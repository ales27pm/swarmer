import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner
from runner import BusyError, DockerRunner, Settings


class QuietLogs:
    stdout = None
    def wait(self, timeout):
        return 0


class NoReader:
    def __init__(self, **kwargs):
        pass
    def start(self):
        pass
    def join(self, timeout):
        pass


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    settings = Settings(tmp_path, tmp_path / "models", tmp_path / "runtime", tmp_path / "db")
    engine = DockerRunner(settings)
    job = {"id": "a" * 32, "prompt": "teapot", "negative_prompt": "", "steps": 40, "seed": 42, "width": 512, "height": 512}
    commands = []
    running = {"value": True, "exit": 0, "rm_failed": False}

    def fake_command(args, timeout=15):
        commands.append(args)
        stdout, error, code = "", "", 0
        if args[:2] == ["docker", "inspect"]:
            stdout = json.dumps({"Running": running["value"], "ExitCode": running["exit"], "OOMKilled": False})
        elif args[:2] == ["docker", "stop"]:
            running["value"] = False
            running["exit"] = 143
        elif args[:2] == ["docker", "rm"]:
            if running["rm_failed"]:
                code = 1
            else:
                running["value"] = False
        elif args[:2] == ["docker", "ps"] and running["rm_failed"]:
            stdout = "a" * 12
        return subprocess.CompletedProcess(args, code, stdout, error)

    monkeypatch.setattr(runner, "command", fake_command)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: QuietLogs())
    monkeypatch.setattr(runner.threading, "Thread", NoReader)
    return engine, job, tmp_path, commands, running


def test_cancel_before_launch_never_starts_container(supervisor, monkeypatch):
    engine, job, work, commands, _ = supervisor
    monkeypatch.setattr(engine, "admission", lambda: None)
    cancel = threading.Event()
    cancel.set()
    assert engine.run(job, work, cancel, lambda **kwargs: None)["status"] == "cancelled"
    assert commands == []


def test_production_appears_after_launch_stops_only_owned_container(supervisor, monkeypatch):
    engine, job, work, commands, _ = supervisor
    checks = []
    def admission():
        checks.append(True)
        if len(checks) > 1:
            raise BusyError("busy")
    monkeypatch.setattr(engine, "admission", admission)
    result = engine.run(job, work, threading.Event(), lambda **kwargs: None)
    assert result["status"] == "failed"
    assert result["error"] == "yield_to_swarmer"
    stops = [command for command in commands if command[:2] == ["docker", "stop"]]
    removes = [command for command in commands if command[:2] == ["docker", "rm"]]
    assert len(stops) == len(removes) == 1
    assert stops[0][-1] == removes[0][-1] == "chroma-studio-" + job["id"]
    receipt = json.loads((work / "receipt.json").read_text())
    assert receipt["state"]["Running"] is False
    assert receipt["state"]["ExitCode"] == 143


def test_admission_failure_during_run_fails_closed(supervisor, monkeypatch):
    engine, job, work, commands, _ = supervisor
    checks = []
    def admission():
        checks.append(True)
        if len(checks) > 1:
            raise OSError("database cannot be read")
    monkeypatch.setattr(engine, "admission", admission)
    result = engine.run(job, work, threading.Event(), lambda **kwargs: None)
    assert result["error"] == "admission_unavailable"
    assert any(command[:2] == ["docker", "stop"] for command in commands)


def test_success_requires_png_then_cleans_owned_container(supervisor, monkeypatch):
    engine, job, work, commands, running = supervisor
    monkeypatch.setattr(engine, "admission", lambda: None)
    running["value"] = False
    Image.new("RGB", (512, 512)).save(work / "image.png")
    result = engine.run(job, work, threading.Event(), lambda **kwargs: None)
    assert result["status"] == "completed"
    assert len(result["image_sha256"]) == 64
    assert any(command[:2] == ["docker", "rm"] for command in commands)


def test_cleanup_failure_cannot_claim_complete(supervisor, monkeypatch):
    engine, job, work, _, running = supervisor
    monkeypatch.setattr(engine, "admission", lambda: None)
    running["value"] = False
    running["rm_failed"] = True
    Image.new("RGB", (512, 512)).save(work / "image.png")
    with pytest.raises(RuntimeError, match="renderer_cleanup_failed"):
        engine.run(job, work, threading.Event(), lambda **kwargs: None)


def test_restart_cleanup_selects_only_service_and_scope(tmp_path, monkeypatch):
    engine = DockerRunner(Settings(tmp_path, tmp_path, tmp_path, tmp_path))
    commands = []
    def fake(args, timeout=15):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "a" * 12 + "\n" if args[:2] == ["docker", "ps"] else "", "")
    monkeypatch.setattr(runner, "command", fake)
    engine.cleanup_interrupted()
    assert "label=app=swarmer-chroma-studio" in commands[0]
    assert f"label=scope={engine.scope}" in commands[0]
    assert commands[1] == ["docker", "rm", "-f", "a" * 12]


def test_uncertain_launch_and_cleanup_failure_is_latched(supervisor, monkeypatch):
    engine, job, work, _, _ = supervisor
    monkeypatch.setattr(engine, "admission", lambda: None)
    def fail_command(args, timeout=15):
        if args[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(args, timeout)
        return subprocess.CompletedProcess(args, 1, "", "daemon unavailable")
    monkeypatch.setattr(runner, "command", fail_command)
    with pytest.raises(RuntimeError, match="renderer_cleanup_failed"):
        engine.run(job, work, threading.Event(), lambda **kwargs: None)


def test_cleanup_command_timeout_is_latched(supervisor, monkeypatch):
    engine, job, work, _, running = supervisor
    monkeypatch.setattr(engine, "admission", lambda: None)
    running["value"] = False
    Image.new("RGB", (512, 512)).save(work / "image.png")
    original = runner.command
    def fail_remove(args, timeout=15):
        if args[:2] == ["docker", "rm"]:
            raise subprocess.TimeoutExpired(args, timeout)
        return original(args, timeout)
    monkeypatch.setattr(runner, "command", fail_remove)
    with pytest.raises(RuntimeError, match="renderer_cleanup_failed"):
        engine.run(job, work, threading.Event(), lambda **kwargs: None)


def test_live_attach_argv_disables_stdin_and_signal_forwarding(supervisor, monkeypatch):
    engine, job, work, _, running = supervisor
    running["value"] = False
    Image.new("RGB", (512, 512)).save(work / "image.png")
    monkeypatch.setattr(engine, "admission", lambda: None)
    calls = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda args, **kwargs: calls.append(args) or QuietLogs())
    result = engine.run(job, work, threading.Event(), lambda **kwargs: None)
    assert result["status"] == "completed"
    assert calls == [["docker", "attach", "--no-stdin", "--sig-proxy=false", "chroma-studio-" + job["id"]]]


@pytest.mark.parametrize("logs_available", [True, False])
def test_final_logs_replace_live_stream_only_when_available(supervisor, monkeypatch, logs_available):
    engine, job, work, commands, running = supervisor
    running["value"] = False
    Image.new("RGB", (512, 512)).save(work / "image.png")
    (work / "runtime.log").write_text("live attachment failure diagnostic")
    monkeypatch.setattr(engine, "admission", lambda: None)
    original = runner.command
    def with_logs(args, timeout=15):
        if args[:2] == ["docker", "logs"]:
            commands.append(args)
            return subprocess.CompletedProcess(args, 0 if logs_available else 1, "complete renderer log" if logs_available else "", "" if logs_available else "unavailable")
        return original(args, timeout)
    monkeypatch.setattr(runner, "command", with_logs)
    assert engine.run(job, work, threading.Event(), lambda **kwargs: None)["status"] == "completed"
    assert (work / "runtime.log").read_text() == ("complete renderer log" if logs_available else "live attachment failure diagnostic")
    log_index = next(i for i, command in enumerate(commands) if command[:2] == ["docker", "logs"])
    rm_index = next(i for i, command in enumerate(commands) if command[:2] == ["docker", "rm"])
    assert log_index < rm_index


def test_real_carriage_return_progress_chunks():
    # Bytes read from the running container via attach during the 40-step QA.
    chunks = [
        b'\r  |=====================================>            | 30/40 - 4.04s/it\x1b[K',
        b'\r  |======================================>           | 31/40 - 4.02s/it\x1b[K',
        b'\r  |========================================>         | 32/40 - 4.08s/it\x1b[K',
    ]
    assert [runner.parse_progress(chunk.decode().strip("\r"), 40) for chunk in chunks] == [
        ("sampling", 0.75), ("sampling", 0.775), ("sampling", 0.8),
    ]


def test_attach_client_shutdown_is_bounded(supervisor, monkeypatch):
    engine, job, work, _, running = supervisor
    running["value"] = False
    Image.new("RGB", (512, 512)).save(work / "image.png")
    monkeypatch.setattr(engine, "admission", lambda: None)
    actions = []
    class SlowAttach(QuietLogs):
        def wait(self, timeout):
            actions.append("wait")
            if actions.count("wait") < 3:
                raise subprocess.TimeoutExpired("docker attach", timeout)
            return 0
        def terminate(self):
            actions.append("terminate")
        def kill(self):
            actions.append("kill")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: SlowAttach())
    assert engine.run(job, work, threading.Event(), lambda **kwargs: None)["status"] == "completed"
    assert actions == ["wait", "terminate", "wait", "kill", "wait"]
