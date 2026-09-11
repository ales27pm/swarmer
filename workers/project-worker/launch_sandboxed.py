"""Launch trusted project transport with isolated files and operator Docker access."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

SOURCES = (
    "workers/project-worker/project_worker.py",
    "workers/project-worker/project_contract.py",
    "workers/project-worker/runtime.py",
    "workers/project-worker/check_harness.py",
    "workers/project-worker/registry_proxy.py",
    "workers/code-worker/code_worker.py",
    "workers/file-worker/file_worker.py",
)
REQUIRED_ENV = (
    "MONGARS_SERVER_URL",
    "MONGARS_AGENT_ID",
    "MONGARS_AGENT_CREDENTIAL",
    "MONGARS_PROJECT_MODEL_URL",
    "MONGARS_PROJECT_MODEL_ID",
    "MONGARS_PROJECT_RUNTIME_IMAGE",
    "MONGARS_PROJECT_SCRATCH_DIR",
)
OPTIONAL_ENV = (
    "MONGARS_JOB_HEARTBEAT_SECONDS",
    "MONGARS_PROJECT_MODEL_TIMEOUT_SECONDS",
)
# Only this private tmpfs is used as HOME; Docker bind inputs use the host scratch.
SANDBOX_HOME = "/tmp"  # nosec B108
BOOTSTRAP = (
    "import runpy,sys; sys.path.insert(0,'/app/workers/project-worker'); "
    "runpy.run_path('/app/workers/project-worker/project_worker.py',run_name='__main__')"
)


def sandbox_command(release: Path) -> tuple[list[str], dict[str, str]]:
    """Verify sources; expose only dedicated scratch, Docker, and trusted files."""
    if os.getuid() == 0:
        raise ValueError("project worker requires an unprivileged operator account")
    release = release.resolve(strict=True)
    manifest_path = release / "release.json"
    if manifest_path.resolve(strict=True) != manifest_path:
        raise ValueError("release manifest must not resolve through a symlink")
    hashes = json.loads(manifest_path.read_text(encoding="utf-8")).get("worker_sources", {})
    for relative in SOURCES:
        source = release / relative
        if source.resolve(strict=True) != source:
            raise ValueError("worker source must not resolve through a symlink")
        if hashlib.sha256(source.read_bytes()).hexdigest() != hashes.get(relative):
            raise ValueError("worker source does not match the release manifest")
    environment = {
        "PATH": "/usr/bin",
        "HOME": SANDBOX_HOME,
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
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", environment["MONGARS_PROJECT_RUNTIME_IMAGE"]):
        raise ValueError("project runtime requires an immutable local image ID")
    scratch = Path(environment["MONGARS_PROJECT_SCRATCH_DIR"])
    if not scratch.is_absolute() or scratch.resolve(strict=True) != scratch:
        raise ValueError("project scratch must be an absolute directory without symlinks")
    metadata = scratch.stat()
    if (
        scratch.name != f"swarmer-project-worker-{os.getuid()}"
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("project scratch must be a dedicated operator-owned 0700 directory")
    environment["TMPDIR"] = str(scratch)
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
        SANDBOX_HOME,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/var/run/docker.sock",
        "/var/run/docker.sock",
        "--bind",
        str(scratch),
        str(scratch),
        "--ro-bind",
        str(manifest_path),
        "/app/release.json",
    ]
    for relative in SOURCES:
        command.extend(("--ro-bind", str(release / relative), "/app/" + relative))
    command.extend(("--chdir", str(scratch), "/usr/bin/python3.12", "-I", "-B", "-c", BOOTSTRAP))
    return command, environment


def main() -> None:
    command, environment = sandbox_command(Path(__file__).resolve().parents[2])
    # Fixed executable/argv; credentials stay in the minimal environment, never argv.
    os.execve(command[0], command, environment)  # nosec B606


if __name__ == "__main__":
    main()
