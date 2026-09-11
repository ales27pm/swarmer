"""Run the trusted proposal worker with only its source and dedicated environment."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

SOURCES = (
    "workers/code-worker/code_worker.py",
    "workers/file-worker/file_worker.py",
)
REQUIRED_ENV = (
    "MONGARS_SERVER_URL",
    "MONGARS_AGENT_ID",
    "MONGARS_AGENT_CREDENTIAL",
    "MONGARS_CODE_MODEL_URL",
    "MONGARS_CODE_MODEL_ID",
)
OPTIONAL_ENV = ("MONGARS_CODE_TIMEOUT_SECONDS", "MONGARS_JOB_HEARTBEAT_SECONDS")
# This path is a new private tmpfs inside bwrap, never the host's /tmp directory.
SANDBOX_TMP = "/tmp"  # nosec B108


def sandbox_command(release: Path) -> tuple[list[str], dict[str, str]]:
    """Verify fixed source paths and construct credential-free bwrap arguments."""
    release = release.resolve(strict=True)
    manifest_path = release / "release.json"
    if manifest_path.resolve(strict=True) != manifest_path:
        raise ValueError("release manifest must not resolve through a symlink")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hashes = manifest.get("worker_sources", {})
    for relative in SOURCES:
        source = release / relative
        if source.resolve(strict=True) != source:
            raise ValueError("worker source must not resolve through a symlink")
        if hashlib.sha256(source.read_bytes()).hexdigest() != hashes.get(relative):
            raise ValueError("worker source does not match the release manifest")
    environment = {
        "PATH": "/usr/bin",
        "HOME": SANDBOX_TMP,
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in (*REQUIRED_ENV, *OPTIONAL_ENV):
        value = os.environ.get(key)
        if key in REQUIRED_ENV and not value:
            raise ValueError("dedicated worker environment is incomplete")
        if value is not None:
            if any(character in value for character in ("\0", "\r", "\n")):
                raise ValueError("dedicated worker environment contains invalid characters")
            environment[key] = value
    command = [
        "/usr/bin/bwrap",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        SANDBOX_TMP,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        str(manifest_path),
        "/app/release.json",
    ]
    for relative in SOURCES:
        command.extend(("--ro-bind", str(release / relative), "/app/" + relative))
    command.extend(
        (
            "--chdir",
            SANDBOX_TMP,
            "/usr/bin/python3.12",
            "-I",
            "-B",
            "/app/workers/code-worker/code_worker.py",
        )
    )
    return command, environment


def main() -> None:
    command, environment = sandbox_command(Path(__file__).resolve().parents[2])
    # The executable is fixed and the arguments are passed directly without a shell.
    os.execve(command[0], command, environment)  # nosec B606


if __name__ == "__main__":
    main()
