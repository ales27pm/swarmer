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
        "sandboxed_worker_launcher", Path(__file__).with_name("launch_sandboxed.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release(tmp_path: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Path:
    hashes = {}
    for relative in launcher.SOURCES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_text("# trusted application source\n")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "release.json").write_text(json.dumps({"worker_sources": hashes}))
    for key in launcher.REQUIRED_ENV:
        monkeypatch.setenv(key, "worker-test-value")
    return tmp_path


def test_isolates_fixed_source_and_dedicated_environment(
    release: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("SERVER_SECRET", "PYTHONPATH", "LD_PRELOAD", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(key, "inherited-value-must-not-cross-boundary")
    monkeypatch.setenv("MONGARS_AGENT_CREDENTIAL", "private-worker-test-marker")
    command, environment = launcher.sandbox_command(release)
    assert command[0] == "/usr/bin/bwrap"
    assert "--unshare-pid" in command and "--unshare-user" in command
    assert "--unshare-net" not in command  # The trusted worker needs host loopback services.
    assert command[-4:] == [
        "/usr/bin/python3.12",
        "-I",
        "-B",
        "/app/workers/code-worker/code_worker.py",
    ]
    assert "private-worker-test-marker" not in " ".join(command)
    assert environment["MONGARS_AGENT_CREDENTIAL"] == "private-worker-test-marker"
    assert not {"SERVER_SECRET", "PYTHONPATH", "LD_PRELOAD", "SSH_AUTH_SOCK"} & environment.keys()
    assert set(environment) <= {
        *launcher.REQUIRED_ENV,
        *launcher.OPTIONAL_ENV,
        "PATH",
        "HOME",
        "LANG",
        "PYTHONDONTWRITEBYTECODE",
    }
    binds = [
        command[index + 1 : index + 3] for index, arg in enumerate(command) if arg == "--ro-bind"
    ]
    assert binds == [
        ["/usr", "/usr"],
        [str(release / "release.json"), "/app/release.json"],
        *[[str(release / relative), "/app/" + relative] for relative in launcher.SOURCES],
    ]


def test_rejects_changed_source(release: Path, launcher: ModuleType) -> None:
    (release / launcher.SOURCES[0]).write_text("# source changed after packaging\n")
    with pytest.raises(ValueError, match="manifest"):
        launcher.sandbox_command(release)


@pytest.mark.parametrize("relative", ["release.json", "workers/code-worker/code_worker.py"])
def test_rejects_symlinked_release_file(release: Path, launcher: ModuleType, relative: str) -> None:
    original = release / relative
    replacement = original.with_suffix(".replacement")
    original.rename(replacement)
    original.symlink_to(replacement)
    with pytest.raises(ValueError, match="symlink"):
        launcher.sandbox_command(release)


def test_requires_own_credential(
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
