"""One cancellable child per render; no process outside that child group is stopped."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from media_contract import IMAGE_PROFILES, MediaError, digest_file, verify_profile


def sandbox_command(command: list[str], profile: Path, directory: Path) -> list[str]:
    """Expose only trusted runtime inputs and scratch; native code has no network."""
    if sys.platform != "linux" or not Path("/usr/bin/bwrap").is_file():
        raise MediaError("runtime_error")
    result = [
        "/usr/bin/bwrap",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--die-with-parent",
        "--cap-drop",
        "ALL",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    roots = {
        Path("/usr"),
        Path(sys.base_prefix).resolve(),
        Path(sys.prefix).absolute(),
        Path(__file__).resolve().parent,
        profile.parent.resolve(),
    }
    # uv may point a venv interpreter through its stable version alias rather
    # than sys.base_prefix. Expose that exact interpreter root, not the home.
    executable = Path(sys.executable)
    if executable.is_symlink():
        target = executable.readlink()
        if target.is_absolute():
            roots.add(target.parent.parent)
    for path in sorted(roots, key=str):
        if not any(other != path and other in path.parents for other in roots):
            result.extend(("--ro-bind", str(path), str(path)))
    for name in ("/lib", "/lib64", "/bin"):
        path = Path(name)
        if path.is_symlink():
            result.extend(("--symlink", os.readlink(path), name))
        elif path.exists():
            result.extend(("--ro-bind", name, name))
    if Path("/etc/ld.so.cache").is_file():
        result.extend(("--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"))
    for device in sorted(Path("/dev").glob("nvidia*")):
        result.extend(("--dev-bind", str(device), str(device)))
    result.extend(("--bind", str(directory), str(directory), "--chdir", str(directory)))
    return [*result, *command]


def resident_gpu_models() -> bool:
    """Fail closed on an unknown Ollama residency. Never unload somebody else's model."""
    request = urllib.request.Request("http://127.0.0.1:11434/api/ps")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    try:
        with urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        ).open(request, timeout=3) as response:
            body = response.read(256_001)
        if len(body) > 256_000:
            return True
        data = json.loads(body)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return True
        return any(
            not isinstance(model, dict)
            or type(model.get("size_vram")) is not int
            or model["size_vram"] < 0
            or model["size_vram"] > 0
            for model in models
        )
    except (OSError, ValueError):
        return True


def studio_container_running() -> bool:
    """Also fence a detached Studio container left alive after its owner crashed."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/docker",
                "ps",
                "--filter",
                "label=app=swarmer-chroma-studio",
                "--format",
                "{{.ID}}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return result.returncode != 0 or bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return True


def group_rss_bytes(group: int) -> int | None:
    """Linux RSS watchdog, in addition to deployment cgroup limits; not GPU memory."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    total = 0
    for directory in proc.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == group:
                total += int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            continue
    return total


class MediaRenderer:
    def __init__(
        self,
        profile: Path,
        skill: str,
        *,
        timeout_seconds: float | None = None,
        max_rss_bytes: int | None = None,
        gpu_enabled: bool = False,
        residency: Callable[[], bool] = resident_gpu_models,
        sandbox_enabled: bool = False,
        gpu_lock_path: Path | None = None,
    ) -> None:
        if timeout_seconds is None:
            timeout_seconds = 600 if skill == "image.generate" else 180
        if max_rss_bytes is None:
            max_rss_bytes = (12 if skill == "image.generate" else 4) * 1024**3
        if (
            skill not in {"audio.synthesize", "image.generate"}
            or not 1 <= timeout_seconds <= 600
            or not 256 * 1024**2 <= max_rss_bytes <= 16 * 1024**3
        ):
            raise ValueError("invalid operator limits")
        self.profile = profile.absolute()
        self.skill = skill
        self.timeout = timeout_seconds
        self.max_rss = max_rss_bytes
        self.gpu_enabled = gpu_enabled
        self.residency = residency
        self.sandbox_enabled = sandbox_enabled
        self.gpu_lock_path = gpu_lock_path
        self._gpu_lock_fd: int | None = None

    def acquire_slot(self) -> bool:
        """Reserve the operator's shared image slot before claiming any work."""
        if self.skill != "image.generate" or self.gpu_lock_path is None:
            return True
        if self._gpu_lock_fd is not None:
            return True
        descriptor = None
        try:
            path = self.gpu_lock_path
            if (
                not path.is_absolute()
                or path.parent.resolve(strict=True) != path.parent
            ):
                return False
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                return False
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._gpu_lock_fd = descriptor
            descriptor = None
            return True
        except OSError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def release_slot(self) -> None:
        if self._gpu_lock_fd is not None:
            os.close(self._gpu_lock_fd)
            self._gpu_lock_fd = None

    def available(self) -> bool:
        # Avoid consuming a claimed attempt while an unrelated model occupies the GPU.
        # The server separately fences active GPU jobs; generate rechecks residency.
        return self.skill != "image.generate" or (
            self.gpu_enabled
            and not self.residency()
            and (self.gpu_lock_path is None or not studio_container_running())
        )

    @property
    def unavailable_status(self) -> str:
        # Temporary residency must not hide a configured capability from planning.
        return (
            "busy"
            if self.skill == "image.generate" and not self.gpu_enabled
            else "online"
        )

    def generate(
        self,
        payload: dict[str, Any],
        directory: Path,
        ensure_active: Callable[[], None],
    ) -> Path:
        deadline = time.monotonic() + self.timeout

        def check() -> None:
            ensure_active()
            if time.monotonic() >= deadline:
                raise MediaError("wall_timeout")

        check()
        profile = verify_profile(self.profile, self.skill, check)
        if self.skill == "image.generate":
            selected = payload.get("model_profile", "sdxl-lightning-4step")
            if (
                not isinstance(selected, str)
                or selected not in IMAGE_PROFILES
                or IMAGE_PROFILES[selected]["backend"] != profile["backend"]
            ):
                raise MediaError("model_unavailable")
            if payload["steps"] != profile["steps"]:
                raise MediaError("unsupported_steps")
            if selected == "chroma1-hd-q4" and (
                payload["width"],
                payload["height"],
            ) != (512, 512):
                raise MediaError("invalid_arguments")
            if not self.available():
                raise MediaError("resource_busy")
        request = directory / "request.json"
        request.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        request.chmod(0o600)
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "LD_LIBRARY_PATH")
            if key in os.environ
        }
        environment.update(
            HOME=str(directory),
            TMPDIR=str(directory),
            HF_HOME=str(directory / "hf"),
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
            DO_NOT_TRACK="1",
            PYTHONDONTWRITEBYTECODE="1",
            TOKENIZERS_PARALLELISM="false",
            CUDA_CACHE_PATH=str(directory / "cuda-cache"),
            OMP_NUM_THREADS="2",
            MKL_NUM_THREADS="2",
            OPENBLAS_NUM_THREADS="2",
        )
        if self.skill == "audio.synthesize":
            environment["CUDA_VISIBLE_DEVICES"] = ""
        command = [
            sys.executable,
            "-B",
            str(Path(__file__).with_name("render.py")),
            "--profile",
            str(self.profile),
            "--profile-sha256",
            digest_file(self.profile),
            "--skill",
            self.skill,
            "--directory",
            str(directory),
        ]
        if self.sandbox_enabled:
            command = sandbox_command(command, self.profile, directory)
        process = subprocess.Popen(
            command,
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            while process.poll() is None:
                check()
                rss = group_rss_bytes(process.pid)
                if rss is not None and rss > self.max_rss:
                    raise MediaError("resource_busy")
                time.sleep(0.05)
            check()
            receipt = directory / "render-result.json"
            if (
                not receipt.is_file()
                or receipt.is_symlink()
                or receipt.stat().st_size > 2048
            ):
                raise MediaError("runtime_error")
            try:
                result = json.loads(receipt.read_text())
            except (OSError, ValueError) as exc:
                raise MediaError("runtime_error") from exc
            if not isinstance(result, dict) or set(result) != {"outcome", "error"}:
                raise MediaError("runtime_error")
            if (
                process.returncode != 0
                or result["outcome"] != "completed"
                or result["error"] is not None
            ):
                raise MediaError(result.get("error"))
            return directory / (
                "output.png" if self.skill == "image.generate" else "output.wav"
            )
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            # The Python wrapper may exit first while a native descendant
            # ignores TERM. Fence the whole group, even when wait() succeeded.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
