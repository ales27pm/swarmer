from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "sandboxed_project_launcher", Path(__file__).with_name("launch_sandboxed.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release(tmp_path: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Path:
    hashes = {}
    release = tmp_path / "release"
    for relative in launcher.SOURCES:
        path = release / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# immutable trusted source\n")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    (release / "release.json").write_text(json.dumps({"worker_sources": hashes}))
    for key in launcher.REQUIRED_ENV:
        monkeypatch.setenv(key, "test-only-value")
    monkeypatch.setenv("MONGARS_PROJECT_RUNTIME_IMAGE", "sha256:" + "1" * 64)
    scratch = tmp_path / f"swarmer-project-worker-{launcher.os.getuid()}"
    scratch.mkdir(mode=0o700)
    monkeypatch.setenv("MONGARS_PROJECT_SCRATCH_DIR", str(scratch))
    return release


def test_private_scratch_remains_at_the_host_path_for_docker(
    release: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "SERVER_SECRET",
        "PYTHONPATH",
        "LD_PRELOAD",
        "DOCKER_HOST",
        "SSH_AUTH_SOCK",
    ):
        monkeypatch.setenv(key, "must-not-be-inherited")
    monkeypatch.setenv("MONGARS_AGENT_CREDENTIAL", "private-test-marker")
    command, environment = launcher.sandbox_command(release)
    scratch = environment["MONGARS_PROJECT_SCRATCH_DIR"]
    assert environment["TMPDIR"] == scratch
    assert command[command.index("--bind") + 1 : command.index("--bind") + 3] == [
        scratch,
        scratch,
    ]
    assert "--unshare-user" in command and "--unshare-pid" in command
    assert "--unshare-net" not in command
    assert "private-test-marker" not in " ".join(command)
    assert environment["MONGARS_AGENT_CREDENTIAL"] == "private-test-marker"
    assert set(environment) <= {
        *launcher.REQUIRED_ENV,
        *launcher.OPTIONAL_ENV,
        "PATH",
        "HOME",
        "LANG",
        "PYTHONDONTWRITEBYTECODE",
        "TMPDIR",
    }
    binds = [command[i + 1 : i + 3] for i, arg in enumerate(command) if arg == "--ro-bind"]
    assert binds == [
        ["/usr", "/usr"],
        ["/var/run/docker.sock", "/var/run/docker.sock"],
        [str(release / "release.json"), "/app/release.json"],
        *[[str(release / relative), "/app/" + relative] for relative in launcher.SOURCES],
    ]
    assert command[-5:] == ["/usr/bin/python3.12", "-I", "-B", "-c", launcher.BOOTSTRAP]
    assert "sys.path.insert(0,'/app/workers/project-worker')" in command[-1]


def test_rejects_modified_source(release: Path, launcher: ModuleType) -> None:
    (release / launcher.SOURCES[0]).write_text("changed source")
    with pytest.raises(ValueError, match="manifest"):
        launcher.sandbox_command(release)


def test_passes_only_explicit_project_model_timeout(
    release: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONGARS_PROJECT_MODEL_TIMEOUT_SECONDS", "180")
    monkeypatch.setenv("MONGARS_CODE_TIMEOUT_SECONDS", "999")
    _, environment = launcher.sandbox_command(release)
    assert environment["MONGARS_PROJECT_MODEL_TIMEOUT_SECONDS"] == "180"
    assert "MONGARS_CODE_TIMEOUT_SECONDS" not in environment


@pytest.mark.parametrize("relative", ["release.json", "workers/project-worker/runtime.py"])
def test_rejects_symlinked_source(release: Path, launcher: ModuleType, relative: str) -> None:
    original = release / relative
    replacement = original.with_suffix(".replacement")
    original.rename(replacement)
    original.symlink_to(replacement)
    with pytest.raises(ValueError, match="symlink"):
        launcher.sandbox_command(release)


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_rejects_shared_scratch(release: Path, launcher: ModuleType, mode: int) -> None:
    Path(launcher.os.environ["MONGARS_PROJECT_SCRATCH_DIR"]).chmod(mode)
    with pytest.raises(ValueError, match="0700"):
        launcher.sandbox_command(release)


def test_rejects_symlinked_scratch(
    release: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = Path(launcher.os.environ["MONGARS_PROJECT_SCRATCH_DIR"])
    alias = scratch.with_name("scratch-alias")
    alias.symlink_to(scratch, target_is_directory=True)
    monkeypatch.setenv("MONGARS_PROJECT_SCRATCH_DIR", str(alias))
    with pytest.raises(ValueError, match="without symlinks"):
        launcher.sandbox_command(release)


def test_rejects_scratch_owned_by_another_operator(
    release: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_uid = launcher.os.getuid() + 1
    scratch = Path(launcher.os.environ["MONGARS_PROJECT_SCRATCH_DIR"])
    renamed = scratch.with_name(f"swarmer-project-worker-{other_uid}")
    scratch.rename(renamed)
    monkeypatch.setenv("MONGARS_PROJECT_SCRATCH_DIR", str(renamed))
    monkeypatch.setattr(launcher.os, "getuid", lambda: other_uid)
    with pytest.raises(ValueError, match="operator-owned"):
        launcher.sandbox_command(release)


def test_rejects_root_operator(
    release: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher.os, "getuid", lambda: 0)
    with pytest.raises(ValueError, match="unprivileged"):
        launcher.sandbox_command(release)


@pytest.mark.parametrize("value", ["runtime:latest", "sha256:not-an-image"])
def test_rejects_mutable_image(
    release: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MONGARS_PROJECT_RUNTIME_IMAGE", value)
    with pytest.raises(ValueError, match="immutable"):
        launcher.sandbox_command(release)


def test_requires_separate_credential(
    release: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MONGARS_AGENT_CREDENTIAL")
    with pytest.raises(ValueError, match="incomplete"):
        launcher.sandbox_command(release)


def test_rejects_multiline_environment(
    release: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONGARS_AGENT_CREDENTIAL", "invalid\nvalue")
    with pytest.raises(ValueError, match="invalid characters"):
        launcher.sandbox_command(release)
