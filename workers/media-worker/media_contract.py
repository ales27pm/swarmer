"""Bounded, model-independent contracts for locally rendered media."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_MEDIA_BYTES = 8 * 1024 * 1024
SKILLS = {"image.generate", "audio.synthesize"}
IMAGE_PROFILES = {
    "sdxl-lightning-4step": {"backend": "sdxl-lightning", "steps": 4},
    "chroma1-hd-q4": {"backend": "chroma-sd-cpp", "steps": 40},
}
FAILURES = {
    "invalid_arguments",
    "unsupported_steps",
    "resource_busy",
    "model_unavailable",
    "model_integrity_error",
    "duration_limit",
    "runtime_error",
    "wall_timeout",
    "invalid_output",
    "storage_limit",
}


class MediaError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = (
            code if isinstance(code, str) and code in FAILURES else "runtime_error"
        )
        super().__init__(self.code)


def integer(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def text(value: Any, maximum: int) -> bool:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\0" in value
    ):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return True


def validate_payload(skill: str, value: Any) -> dict[str, Any]:
    if skill not in SKILLS or not isinstance(value, dict):
        raise MediaError("invalid_arguments")
    fields = (
        {"prompt", "width", "height", "steps", "seed"}
        if skill == "image.generate"
        else {"text", "language", "voice", "max_duration_seconds"}
    )
    optional = (
        {"context", "model_profile"} if skill == "image.generate" else {"context"}
    )
    if set(value) - fields - optional or fields - set(value):
        raise MediaError("invalid_arguments")
    try:
        if (
            len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
            > 64_000
        ):
            raise MediaError("invalid_arguments")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MediaError("invalid_arguments") from exc
    if "context" in value and not isinstance(value["context"], dict):
        raise MediaError("invalid_arguments")
    if skill == "image.generate":
        model_profile = value.get("model_profile", "sdxl-lightning-4step")
        if not isinstance(model_profile, str) or model_profile not in IMAGE_PROFILES:
            raise MediaError("invalid_arguments")
        valid = (
            text(value["prompt"], 2000)
            and type(value["width"]) is int
            and value["width"] in (512, 768)
            and type(value["height"]) is int
            and value["height"] in (512, 768)
            and type(value["steps"]) is int
            and value["steps"] == IMAGE_PROFILES[model_profile]["steps"]
            and (
                model_profile != "chroma1-hd-q4"
                or (value["width"], value["height"]) == (512, 512)
            )
            and integer(value["seed"], 0, 2_147_483_647)
        )
    else:
        valid = (
            text(value["text"], 1000)
            and value["language"] == "fr-FR"
            and value["voice"] == "ff_siwis"
            and integer(value["max_duration_seconds"], 1, 30)
        )
    if not valid:
        raise MediaError("invalid_arguments")
    # Context stays in the canonical job. It cannot replace text, paths, models or runtime options.
    if "model_profile" in value:
        fields.add("model_profile")
    return {key: value[key] for key in fields}


def phoneme_chunks(
    source: str, phonemize: Callable[[str], str], maximum: int = 500
) -> list[tuple[str, str]]:
    """Split source substrings until every phoneme sequence fits; never slice phonemes."""
    if not text(source, 1000) or not integer(maximum, 1, 510):
        raise MediaError("invalid_arguments")
    pending = [source]
    result: list[tuple[str, str]] = []
    while pending:
        segment = pending.pop()
        phones = phonemize(segment)
        if not isinstance(phones, str):
            raise MediaError("runtime_error")
        if len(phones) <= maximum:
            if not phones and any(character.isalnum() for character in segment):
                raise MediaError("invalid_output")
            result.append((segment, phones))
            continue
        if len(segment) < 2:
            raise MediaError("invalid_output")
        midpoint = len(segment) // 2
        boundaries = [m.end() for m in re.finditer(r"[.!?;:\n\s]+", segment)]
        split = min(
            (p for p in boundaries if 0 < p < len(segment)),
            key=lambda p: abs(p - midpoint),
            default=midpoint,
        )
        pending.extend((segment[split:], segment[:split]))
    if "".join(segment for segment, _ in result) != source or not any(
        p for _, p in result
    ):
        raise MediaError("invalid_output")
    return result


def digest_file(path: Path, check: Callable[[], None] = lambda: None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            check()
            digest.update(block)
    check()
    return digest.hexdigest()


def safe_relative(path: Any) -> bool:
    return (
        isinstance(path, str)
        and len(path) <= 1024
        and all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,239}", part)
            and part not in (".", "..")
            for part in path.split("/")
        )
        and len(path.split("/")) <= 16
    )


def local_path(root: Path, relative: str) -> Path:
    if not safe_relative(relative):
        raise MediaError("model_integrity_error")
    candidate = root
    for part in relative.split("/"):
        candidate /= part
        if candidate.is_symlink():
            raise MediaError("model_integrity_error")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise MediaError("model_integrity_error")
    return candidate


def verify_profile(
    path: Path, skill: str, check: Callable[[], None] = lambda: None
) -> dict[str, Any]:
    """Only administrator-installed manifests are read; no job may select this path."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256_000:
        raise MediaError("model_unavailable")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MediaError("model_unavailable") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "backend",
        "origins",
        "components",
        "files",
        "steps",
    }:
        raise MediaError("model_integrity_error")
    backend = value["backend"]
    allowed_backends = (
        {"kokoro"}
        if skill == "audio.synthesize"
        else {"sdxl-lightning", "chroma-sd-cpp"}
    )
    if (
        skill not in SKILLS
        or value["schema_version"] != "1.0"
        or not isinstance(backend, str)
        or backend not in allowed_backends
    ):
        raise MediaError("model_integrity_error")
    origins = value["origins"]
    if (
        not isinstance(origins, list)
        or not 1 <= len(origins) <= 4
        or any(
            not isinstance(origin, dict)
            or set(origin) != {"repo_id", "revision"}
            or not isinstance(origin["repo_id"], str)
            or not re.fullmatch(r"[\w.-]+/[\w.-]+", origin["repo_id"])
            or not isinstance(origin["revision"], str)
            or not re.fullmatch(r"[a-f0-9]{40}", origin["revision"])
            for origin in origins
        )
    ):
        raise MediaError("model_integrity_error")
    components = value["components"]
    required = {
        "kokoro": {"config", "weights", "voice"},
        "sdxl-lightning": {"base", "unet"},
        "chroma-sd-cpp": {"diffusion", "text_encoder", "vae", "runtime"},
    }[backend]
    if (
        not isinstance(components, dict)
        or set(components) != required
        or any(not safe_relative(p) for p in components.values())
    ):
        raise MediaError("model_integrity_error")
    expected_steps = {"kokoro": None, "sdxl-lightning": 4, "chroma-sd-cpp": 40}[backend]
    if (
        type(value["steps"]) is not type(expected_steps)
        or value["steps"] != expected_steps
    ):
        raise MediaError("model_integrity_error")
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 512:
        raise MediaError("model_integrity_error")
    names: set[str] = set()
    total = 0
    for item in files:
        check()
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size_bytes", "sha256"}
            or not safe_relative(item["path"])
        ):
            raise MediaError("model_integrity_error")
        name = item["path"]
        extensions = {
            ".json",
            ".txt",
            ".safetensors",
            ".pth",
            ".pt",
        }
        is_runtime = backend == "chroma-sd-cpp" and name == components["runtime"]
        if backend == "chroma-sd-cpp":
            extensions = {".gguf", ".safetensors"}
        if name.casefold() in names or (
            not is_runtime and Path(name).suffix.lower() not in extensions
        ):
            raise MediaError("model_integrity_error")
        names.add(name.casefold())
        if (
            not integer(item["size_bytes"], 1, 16 * 1024**3)
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
        ):
            raise MediaError("model_integrity_error")
        total += item["size_bytes"]
        if total > 20 * 1024**3:
            raise MediaError("model_integrity_error")
        artifact = local_path(path.parent, name)
        try:
            info = artifact.stat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size != item["size_bytes"]
                or digest_file(artifact, check) != item["sha256"]
                or (is_runtime and not os.access(artifact, os.X_OK))
            ):
                raise MediaError("model_integrity_error")
        except OSError as exc:
            raise MediaError("model_unavailable") from exc
    paths = {item["path"] for item in files}
    if backend in {"kokoro", "chroma-sd-cpp"}:
        if any(component not in paths for component in components.values()):
            raise MediaError("model_integrity_error")
        if backend == "chroma-sd-cpp" and (
            len(set(components.values())) != 4
            or paths != set(components.values())
            or Path(components["runtime"]).name != "sd-cli"
            or Path(components["diffusion"]).suffix != ".gguf"
            or Path(components["text_encoder"]).suffix != ".gguf"
            or Path(components["vae"]).suffix != ".safetensors"
        ):
            raise MediaError("model_integrity_error")
    else:
        base = local_path(path.parent, components["base"])
        # Reject omitted or extra files: local loaders cannot consume unpinned artifacts.
        if not base.is_dir() or components["unet"] not in paths:
            raise MediaError("model_integrity_error")
        actual = {
            str(p.relative_to(path.parent))
            for p in base.rglob("*")
            if p.is_file() or p.is_symlink()
        }
        if actual != {p for p in paths if p.startswith(components["base"] + "/")}:
            raise MediaError("model_integrity_error")
        if any(not p.endswith((".json", ".txt", ".safetensors")) for p in actual):
            raise MediaError("model_integrity_error")
    return value


def validate_media(path: Path, skill: str, payload: dict[str, Any]) -> dict[str, Any]:
    if (
        path.is_symlink()
        or not path.is_file()
        or not 1 <= path.stat().st_size <= MAX_MEDIA_BYTES
    ):
        raise MediaError("invalid_output")
    metadata: dict[str, Any] = {
        "size_bytes": path.stat().st_size,
        "sha256": digest_file(path),
    }
    try:
        if skill == "image.generate":
            from PIL import Image

            Image.MAX_IMAGE_PIXELS = 768 * 768
            with Image.open(path) as image:
                if (
                    image.format != "PNG"
                    or image.size != (payload["width"], payload["height"])
                    or image.mode not in ("RGB", "RGBA")
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise MediaError("invalid_output")
                image.verify()
            with Image.open(path) as image:
                image.load()
            metadata.update(
                media_type="image/png",
                width=payload["width"],
                height=payload["height"],
                duration_ms=None,
                sample_rate=None,
                channels=None,
            )
        else:
            with wave.open(str(path), "rb") as audio:
                frames = audio.getnframes()
                if (
                    audio.getnchannels() != 1
                    or audio.getsampwidth() != 2
                    or audio.getframerate() != 24_000
                    or audio.getcomptype() != "NONE"
                    or frames <= 0
                ):
                    raise MediaError("invalid_output")
                if frames > payload["max_duration_seconds"] * 24_000:
                    raise MediaError("duration_limit")
                if len(audio.readframes(frames + 1)) != frames * 2:
                    raise MediaError("invalid_output")
            metadata.update(
                media_type="audio/wav",
                width=None,
                height=None,
                duration_ms=math.ceil(frames / 24),
                sample_rate=24_000,
                channels=1,
            )
    except MediaError:
        raise
    except (OSError, ValueError, EOFError, wave.Error) as exc:
        raise MediaError("invalid_output") from exc
    return metadata
