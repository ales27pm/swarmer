"""Bounded, private Chroma renderer. This module never mutates Swarmer state."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import struct
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

IMAGE = "sha256:c55ec3428181057adc0a92a8cf844cb7d3aa029dc2c154bc8baca5fc6c7ede5f"
MODEL_FILES = {
    "diffusion": ("Chroma1-HD-GGUF/Chroma1-HD-Q4_K_M.gguf", 5566533792),
    "t5": ("t5-v1_1-xxl-encoder-gguf/t5-v1_1-xxl-encoder-Q5_K_M.gguf", 3386856640),
    "vae": ("Chroma/ae.safetensors", 335304388),
}
PROGRESS_RE = re.compile(r"^\s*\|[= >]*\|\s*(\d+)\s*/\s*(\d+)\s*-\s*[\d.]+(?:s/it|it/s)")


@dataclass(frozen=True)
class Settings:
    state: Path
    models: Path
    runtime: Path
    db: Path
    ollama: str = "http://127.0.0.1:11434/api/ps"
    deadline: int = 900
    gpu_lock: Path = field(default_factory=lambda: Path.home() / ".local/state/swarmer-gpu/image-generation.lock")

    @classmethod
    def from_env(cls):
        home = Path.home()
        return cls(
            Path(os.environ.get("CHROMA_STUDIO_STATE", home / ".local/state/chroma-studio")),
            Path(os.environ.get("CHROMA_STUDIO_MODELS", home / "swarmer-media-qualification/20261002/chroma-inference/models")),
            Path(os.environ.get("CHROMA_STUDIO_RUNTIME", home / ".local/share/swarmer/chroma-runtime/3f8527a-sm75/bin")),
            Path(os.environ.get("CHROMA_STUDIO_DB", home / ".local/state/swarmer-control-plane/mongars.db")),
            gpu_lock=Path(os.environ.get("CHROMA_STUDIO_GPU_LOCK", home / ".local/state/swarmer-gpu/image-generation.lock")),
        )


class BusyError(RuntimeError):
    def __init__(self, message: str, code: str = "swarmer_busy"):
        super().__init__(message)
        self.code = code


class OllamaProbeError(RuntimeError):
    """Unknown residency must not be treated as a free GPU."""


def acquire_gpu_lock(path: Path) -> int:
    """Acquire the same inode as the media worker; never unlink the lock file."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("GPU lock is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def command(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def read_png(path: Path, width: int, height: int) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise RuntimeError("invalid_image_output")
    with path.open("rb") as source:
        header = source.read(24)
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            raise RuntimeError("invalid_image_output")
        if struct.unpack(">II", header[16:24]) != (width, height):
            raise RuntimeError("unexpected_image_dimensions")
        source.seek(0)
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
    except (OSError, ValueError) as exc:
        raise RuntimeError("invalid_image_output") from exc
    return {"bytes": path.stat().st_size, "sha256": digest}


def parse_progress(text: str, steps: int) -> tuple[str, float | None] | None:
    """Only expose renderer-observed progress, never elapsed-time estimates."""
    lower = text.lower()
    if "decoding 1 latents" in lower or "vae decode graph" in lower:
        return "decoding", None
    if "sampling completed" in lower:
        return "decoding", None
    if re.match(r"^\[INFO\s*\]\s+image\.cpp:\d+\s+-\s+generating image:\s+\d+/\d+\s+-\s+seed\s", text):
        return "sampling", None
    matches = PROGRESS_RE.findall(text)
    for current, total in reversed(matches):
        current, total = int(current), int(total)
        if total == steps and 0 <= current <= total:
            return "sampling", round(current / total, 4)
    if "sampling using" in lower:
        return "encoding", None
    if "encode condition" in lower or "encoding prompts" in lower:
        return "encoding", None
    return None


class DockerRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.scope = hashlib.sha256(str(settings.state.resolve()).encode()).hexdigest()[:16]

    def cleanup_interrupted(self):
        found = command(["docker", "ps", "-aq", "--filter", "label=app=swarmer-chroma-studio", "--filter", f"label=scope={self.scope}"])
        if found.returncode:
            raise RuntimeError("docker_unavailable")
        for container in found.stdout.splitlines():
            if re.fullmatch(r"[a-f0-9]{12,64}", container):
                result = command(["docker", "rm", "-f", container], 20)
                if result.returncode:
                    raise RuntimeError("interrupted_container_cleanup_failed")

    def admission(self):
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.settings.db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as db:
            db.execute("PRAGMA query_only=ON")
            calls = db.execute("SELECT count(*) FROM goal_model_calls WHERE provider_source='ubuntu_local' AND status='started' AND lease_expires_at>?", (now,)).fetchone()[0]
            jobs = db.execute("SELECT count(*) FROM agent_jobs WHERE required_skill IN ('writing.draft','code.generate_python','code.build_project','image.generate') AND status IN ('claimed','running') AND lease_expires_at>?", (now,)).fetchone()[0]
        if calls or jobs:
            raise BusyError("Une tâche ou un appel modèle Swarmer est en cours : le GPU lui est réservé.")
        try:
            with urllib.request.urlopen(self.settings.ollama, timeout=3) as response:
                body = response.read(256001)
            if len(body) > 256000:
                raise ValueError("oversized Ollama status")
            payload = json.loads(body)
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                raise TypeError("unknown Ollama model list")
            # CPU residency alone does not reserve the GPU. Require explicit,
            # valid VRAM accounting for every model, including mixed lists.
            if any(not isinstance(model, dict) or type(model.get("size_vram")) is not int
                   or model["size_vram"] < 0 for model in models):
                raise ValueError("unknown Ollama VRAM usage")
        except Exception as exc:
            raise OllamaProbeError("Le contrôle de la mémoire GPU d’Ollama est indisponible.") from exc
        if any(model["size_vram"] > 0 for model in models):
            raise BusyError("Un modèle Ollama occupe de la mémoire GPU.", "ollama_gpu_reserved")

    def ready(self) -> tuple[bool, str, str]:
        try:
            if not os.access(self.settings.runtime / "sd-cli", os.X_OK):
                return False, "Le moteur Chroma est absent ou non exécutable.", "runtime_unavailable"
            for relative, expected_size in MODEL_FILES.values():
                if not (self.settings.models / relative).is_file():
                    return False, "Un fichier modèle Chroma est absent.", "model_unavailable"
                if (self.settings.models / relative).stat().st_size != expected_size:
                    return False, "Un fichier modèle a changé : validation requise.", "model_invalid"
            self.admission()
        except BusyError as exc:
            return False, str(exc), exc.code
        except OllamaProbeError as exc:
            return False, str(exc), "ollama_unavailable"
        except Exception:  # noqa: BLE001 - Readiness must fail closed on any probe failure.
            return False, "Le contrôle du moteur ou du serveur est indisponible.", "probe_unavailable"
        return True, "Chroma est prêt. Une image à 40 étapes prend environ quatre minutes.", "ready"

    def build_command(self, job: dict, work: Path, name: str) -> list[str]:
        paths = {key: "/models/" + value[0] for key, value in MODEL_FILES.items()}
        cli = ["/runtime/sd-cli", "--diffusion-model", paths["diffusion"], "--t5xxl", paths["t5"], "--vae", paths["vae"],
               "--backend", "diffusion=cuda0,te=cpu,vae=cpu", "--params-backend", "diffusion=cuda0,te=cpu,vae=cpu",
               "--max-vram", "cuda0=6.5", "--diffusion-fa", "--steps", str(job["steps"]), "--cfg-scale", "3",
               "--sampling-method", "euler", "--scheduler", "flux", "--extra-sample-args",
               f"base_shift={math.log(3)},max_shift={math.log(3)}", "-W", "512", "-H", "512", "-b", "1",
               "-s", str(job["seed"]), "-t", "4", "-p", job["prompt"], "-n", job["negative_prompt"], "-o", "/output/image.png", "-v"]
        return ["docker", "run", "-d", "-t", "--name", name, "--label", "app=swarmer-chroma-studio", "--label", f"scope={self.scope}",
                "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", f"{os.getuid()}:{os.getgid()}", "--gpus", "all", "--memory", "12g", "--memory-swap", "12g",
                "--cpus", "6", "--pids-limit", "256", "--init", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
                "--mount", f"type=bind,src={self.settings.runtime.resolve()},dst=/runtime,readonly",
                "--mount", f"type=bind,src={self.settings.models.resolve()},dst=/models,readonly",
                "--mount", f"type=bind,src={work.resolve()},dst=/output", "--env", "HOME=/tmp", "--env", "CUDA_CACHE_PATH=/tmp/cuda-cache",
                "--entrypoint", "/usr/bin/timeout", IMAGE, "--signal=TERM", "--kill-after=10s", str(self.settings.deadline), *cli]

    def run(self, job: dict, work: Path, cancel: threading.Event, update: Callable):
        self.admission()
        if cancel.is_set():
            return {"status": "cancelled", "phase": "cancelled", "error": None}
        name = "chroma-studio-" + job["id"]
        args = self.build_command(job, work, name)
        (work / "command.json").write_text(json.dumps(args, ensure_ascii=False, indent=2))
        started = time.monotonic()
        samples = []
        reason = None
        state = {}
        log_stream = None
        reader = None
        launched = False

        def read_logs():
            # Attach reads the live TTY stream. Docker's json-file log driver
            # buffers CR-only progress until newline, even with TTY enabled.
            pending = bytearray()
            with (work / "runtime.log").open("wb") as sink:
                while True:
                    chunk = os.read(log_stream.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    sink.write(chunk)
                    sink.flush()
                    pending.extend(chunk)
                    while b"\r" in pending or b"\n" in pending:
                        positions = [p for p in (pending.find(b"\r"), pending.find(b"\n")) if p >= 0]
                        end = min(positions)
                        line = bytes(pending[:end]).decode("utf-8", "replace")
                        del pending[:end + 1]
                        progress = parse_progress(line, job["steps"])
                        if progress:
                            update(phase=progress[0], progress=progress[1])
                    if len(pending) > 65536:
                        del pending[:-4096]

        try:
            result = command(args, 30)
            # On a client timeout a container can already exist; finally always attempts own cleanup.
            if result.returncode:
                (work / "launch-error.txt").write_text(result.stderr)
                raise RuntimeError("renderer_launch_failed")
            launched = True
            update(status="running", phase="loading", progress=None)
            log_stream = subprocess.Popen(["docker", "attach", "--no-stdin", "--sig-proxy=false", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            reader = threading.Thread(target=read_logs, daemon=True)
            reader.start()
            while True:
                result = command(["docker", "inspect", "--format", "{{json .State}}", name], 10)
                if result.returncode:
                    raise RuntimeError("renderer_state_unavailable")
                state = json.loads(result.stdout)
                if not state["Running"]:
                    break
                if cancel.is_set():
                    reason = "cancelled"
                elif time.monotonic() - started > self.settings.deadline + 10:
                    reason = "deadline_exceeded"
                else:
                    try:
                        self.admission()
                    except BusyError:
                        reason = "yield_to_swarmer"
                    except Exception:  # noqa: BLE001 - Any admission failure must stop this render.
                        reason = "admission_unavailable"
                if reason:
                    update(phase="cancelling", progress=None)
                    command(["docker", "stop", "--timeout", "5", name], 15)
                    break
                gpu = command(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"], 5)
                samples.append({"elapsed_seconds": round(time.monotonic() - started, 3), "whole_gpu_mib_util_temp": gpu.stdout.strip() if not gpu.returncode else None})
                cancel.wait(2)
            if reason == "cancelled":
                return {"status": "cancelled", "phase": "cancelled", "error": None}
            if reason:
                return {"status": "failed", "phase": "failed", "error": reason}
            if state.get("OOMKilled"):
                raise RuntimeError("renderer_memory_limit")
            if state.get("ExitCode") != 0:
                raise RuntimeError("deadline_exceeded" if state.get("ExitCode") == 124 else "renderer_failed")
            image = read_png(work / "image.png", job["width"], job["height"])
            return {"status": "completed", "phase": "completed", "progress": 1.0, "error": None, "image_sha256": image["sha256"]}
        finally:
            # Exact name generated by this service; never acts on production containers.
            cleanup_failed = False
            complete_log = None
            log_capture_error = None
            try:
                final_state = command(["docker", "inspect", "--format", "{{json .State}}", name], 10)
                if not final_state.returncode:
                    state = json.loads(final_state.stdout)
            except Exception as exc:  # noqa: BLE001 - Cleanup still runs if inspection fails.
                state["final_inspection_error"] = type(exc).__name__
            try:
                logs = command(["docker", "logs", name], 15)
                if not logs.returncode and logs.stdout:
                    complete_log = logs.stdout + logs.stderr
                elif logs.returncode:
                    log_capture_error = "docker_logs_failed"
            except (OSError, subprocess.SubprocessError) as exc:
                log_capture_error = type(exc).__name__
            try:
                removed = command(["docker", "rm", "-f", name], 20)
                if removed.returncode:
                    # A failed launch may never have created a container. Require
                    # positive evidence of absence before releasing the GPU slot.
                    remaining = command(["docker", "ps", "-aq", "--filter", f"name=^/{name}$"], 10)
                    cleanup_failed = bool(remaining.returncode or remaining.stdout.strip())
            except Exception:  # noqa: BLE001 - Uncertain cleanup blocks further GPU admission.
                cleanup_failed = True
            if log_stream is not None:
                try:
                    log_stream.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    log_stream.terminate()
                    try:
                        log_stream.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        log_stream.kill()
                        log_stream.wait(timeout=3)
            if reader is not None:
                reader.join(timeout=3)
            # Include startup lines emitted before attach connected. Keep the live
            # failure stream if the complete log is unavailable or empty.
            if complete_log is not None:
                (work / "runtime.log").write_text(complete_log)
            receipt = {"elapsed_seconds": round(time.monotonic() - started, 3), "state": state, "stop_reason": reason,
                       "launched": launched, "samples": samples, "log_capture_error": log_capture_error,
                       "runtime_commit": "3f8527a46c54ecf4cb4ed6003da8e8982283c73c"}
            (work / "receipt.json").write_text(json.dumps(receipt, indent=2))
            if cleanup_failed:
                raise RuntimeError("renderer_cleanup_failed")
