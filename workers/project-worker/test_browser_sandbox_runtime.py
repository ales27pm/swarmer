from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import project_worker as worker
import pytest
import runtime

IMAGE = "sha256:" + "a" * 64


def operator(monkeypatch):
    monkeypatch.setattr(runtime.os, "getuid", lambda: 1000)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")


def reviewed_copy(tmp_path, monkeypatch):
    data = Path(runtime.__file__).with_name("browser-seccomp-v1.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == runtime.BROWSER_SECCOMP_SHA256
    path = tmp_path / "browser-seccomp-v1.json"
    path.write_bytes(data)
    monkeypatch.setattr(runtime, "BROWSER_SECCOMP_PATH", path)
    return path, data


@pytest.mark.parametrize("value,expected", [(None, False), ("0", False), ("1", True)])
def test_startup_browser_activation_is_explicit(value, expected):
    assert worker.browser_sandbox_enabled(value) is expected


@pytest.mark.parametrize("value", ["", "true", "yes", " 1", "1 ", "2"])
def test_bad_operator_activation_is_rejected(value):
    with pytest.raises(ValueError, match="must be 0 or 1"):
        worker.browser_sandbox_enabled(value)


@pytest.mark.parametrize("defect", ["missing", "symlink", "tampered", "directory", "oversized"])
def test_untrusted_profile_fails_before_any_container(tmp_path, monkeypatch, defect):
    operator(monkeypatch)
    path = tmp_path / "profile.json"
    if defect == "symlink":
        original = tmp_path / "original.json"
        original.write_text("{}")
        path.symlink_to(original)
    elif defect == "directory":
        path.mkdir()
    elif defect == "oversized":
        path.write_bytes(b"x" * 64_001)
    elif defect == "tampered":
        path.write_text("{}")
    monkeypatch.setattr(runtime, "BROWSER_SECCOMP_PATH", path)
    monkeypatch.setattr(
        runtime.DockerRunner, "_process", lambda *args: pytest.fail("must not launch")
    )
    with pytest.raises(ValueError, match="seccomp profile"):
        runtime.DockerRunner(IMAGE, browser_sandbox=True)


def test_default_runner_does_not_require_or_apply_browser_profile(tmp_path, monkeypatch):
    operator(monkeypatch)
    monkeypatch.setattr(runtime, "BROWSER_SECCOMP_PATH", tmp_path / "missing")
    runner = runtime.DockerRunner(IMAGE)
    assert runner._browser_seccomp is None
    command = runner._base("ordinary-check", "none")
    assert command[command.index("--pids-limit") + 1] == "64"
    assert not any(arg.startswith("seccomp=") for arg in command)


@pytest.mark.parametrize("value", [1, "1", None])
def test_constructor_requires_boolean_not_truthy_configuration(monkeypatch, value):
    operator(monkeypatch)
    with pytest.raises(ValueError, match="explicit boolean"):
        runtime.DockerRunner(IMAGE, browser_sandbox=value)


def test_profile_is_only_applied_to_optin_tests_with_private_pinned_snapshot(tmp_path, monkeypatch):
    operator(monkeypatch)
    original, reviewed = reviewed_copy(tmp_path, monkeypatch)
    runner = runtime.DockerRunner(IMAGE, browser_sandbox=True)
    original.write_text("modified after verified startup")
    observed = []
    profile_copies = []

    def process(command, directory, timeout, ensure_active):
        observed.append(command)
        assert "--cap-add" not in command and "--privileged" not in command
        assert not any(value in {"--no-sandbox", "seccomp=unconfined"} for value in command)
        if IMAGE in command:
            assert command[command.index("--memory") + 1] == "1g"
            assert command[command.index("--cpus") + 1] == "2"
            assert "no-new-privileges" in command
            mode = command[-1]
            selected = [arg for arg in command if arg.startswith("seccomp=")]
            if mode in {"python_test", "node_test"}:
                assert command[command.index("--network") + 1] == "none"
                assert command[command.index("--pids-limit") + 1] == "256"
                assert len(selected) == 1
                private = Path(selected[0].split("=", 1)[1])
                assert private.parent == directory and private != original
                assert private.read_bytes() == reviewed
                assert stat.S_IMODE(private.stat().st_mode) == 0o600
                assert not any(
                    str(private) in arg and arg.startswith("type=bind") for arg in command
                )
                profile_copies.append(private)
            else:
                assert command[command.index("--pids-limit") + 1] == "64"
                assert selected == []
            if mode in {"python_test", "python_build", "node_test", "node_build"}:
                count = 1 if mode.endswith("_test") else 0
                return (
                    0,
                    runtime.RECEIPT_PREFIX
                    + json.dumps({"exit_code": 0, "tests_executed": count, "test_failures": 0}),
                    1,
                )
        return 0, "", 1

    monkeypatch.setattr(runner, "_process", process)
    result = runner.run(
        [
            {"path": "requirements.txt", "content": "Flask==3.1.3\n"},
            {"path": "package.json", "content": '{"dependencies":{"express":"4.21.2"}}'},
            {"path": "app.py", "content": "VALUE = 1\n"},
        ],
        "python_node",
        [],
        lambda: None,
    )
    assert result["tests_executed"] == 2 and result["build_passed"]
    assert len(profile_copies) == 2 and all(not p.exists() for p in profile_copies)
    assert any("/manifests/requirements.txt" in command for command in observed)
    assert any("npm" in command for command in observed)
    # Runtime inspection is also deliberately left on the original policy.
    runner.probe_profile()
    assert observed[-2][-1] == "runtime_profile"
    assert not any(arg.startswith("seccomp=") for arg in observed[-2])
