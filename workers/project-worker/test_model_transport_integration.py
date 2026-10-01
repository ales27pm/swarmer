from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from pathlib import Path
from typing import Any

import launch_sandboxed
import project_worker as worker
import pytest

_GOAL = "goal_6fc92c9fa71d42b7ab478417486467ae"
_JOB = "job_8fb6db328150431283da3d3ffb526c7d"
_PRIVATE = "PRIVATE_PROMPT_OUTPUT_REASONING_TOKEN"


def payload() -> dict[str, Any]:
    return {
        "objective": "Create a tiny Python app. " + _PRIVATE,
        "conversation": [],
        "files": [],
        "plan": [],
        "checks": [],
        "iteration": 1,
        "base_revision_id": None,
        "base_sha256": None,
    }


def step(*, clarify: bool = False) -> dict[str, Any]:
    return {
        "action": "clarify" if clarify else "continue",
        "message": _PRIVATE,
        "plan": ["Implement application", "Execute tests"],
        "edits": [] if clarify else [{"path": "app.py", "content": "value = 1\n"}],
        "patches": [],
        "deletions": [],
        "requested_checks": [],
        "run_instructions": "python app.py",
        "runtime": "python",
        "focus_paths": [],
    }


def event(content: str, *, done: bool, **metrics: Any) -> bytes:
    return (
        json.dumps(
            {
                "message": {"content": content, "thinking": _PRIVATE},
                "done": done,
                **metrics,
            }
        ).encode()
        + b"\n"
    )


def generator() -> worker.ProjectGenerator:
    result = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1",
        "qwen3-coder:30b",
        context_tokens=64_000,
        prompt_max_bytes=50_000,
    )
    result.receipt_job_id = _JOB
    result.receipt_goal_id = _GOAL
    return result


def install_transport(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes = b"",
    *,
    open_error: BaseException | None = None,
    end_error: BaseException | None = None,
    response_status: int = 200,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    class Response(io.BytesIO):
        status = response_status

        def readline(self, size: int = -1) -> bytes:
            if end_error is not None and self.tell() >= len(raw):
                raise end_error
            return super().readline(size)

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            requests.append(json.loads(request.data))
            if open_error is not None:
                raise open_error
            return Response(raw)

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *_args: Opener())
    return requests


def logged_receipts(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    prefix = "project model transport receipt: "
    return [
        json.loads(record.getMessage().removeprefix(prefix))
        for record in caplog.records
        if record.name == worker.LOGGER.name and record.getMessage().startswith(prefix)
    ]


def test_generate_logs_one_correlated_receipt_with_terminal_counters(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=worker.LOGGER.name)
    serialized = json.dumps(step())
    raw = event(serialized, done=False) + event(
        "",
        done=True,
        done_reason="stop",
        prompt_eval_count=1234,
        eval_count=89,
        load_duration=12,
        prompt_eval_duration=34,
        eval_duration=56,
        total_duration=102,
    )
    requests = install_transport(monkeypatch, raw)
    model = generator()
    assert model.generate(payload())["edits"] == step()["edits"]
    assert len(requests) == 1
    receipts = logged_receipts(caplog)
    assert receipts == [model.last_transport_receipt]
    result = receipts[0]
    assert result["job_id"] == _JOB and result["goal_id"] == _GOAL
    assert result["outcome"] == "success" and result["scope"] == "model_transport"
    assert result["request"]["context_tokens"] == 64_000
    assert result["request"]["output_token_limit"] == requests[0]["options"]["num_predict"]
    assert result["request"]["prompt_bytes"] == sum(
        len(message["content"].encode()) for message in requests[0]["messages"]
    )
    assert result["request"]["schema_bytes"] == len(
        json.dumps(requests[0]["format"], separators=(",", ":")).encode()
    )
    assert result["transport"]["response_bytes"] == len(raw)
    assert result["transport"]["content_bytes"] == len(serialized.encode())
    assert result["transport"]["chunks"] == 2
    assert result["ollama"]["prompt_eval_count"] == 1234
    assert result["ollama"]["eval_count"] == 89
    assert result["ollama"]["total_duration"] == 102
    assert result["ollama"]["duration_unit"] == "ns"
    assert result["client_timing_ms"]["first_content_ms"] is not None
    assert _PRIVATE not in caplog.text and "http://" not in caplog.text


@pytest.mark.parametrize("terminal", [False, True])
def test_generate_incomplete_stream_keeps_only_observed_terminal_stats(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, terminal: bool
) -> None:
    caplog.set_level(logging.INFO, logger=worker.LOGGER.name)
    raw = event('{"action":', done=False)
    if terminal:
        raw += event("", done=True, done_reason="length", eval_count=2000)
    install_transport(monkeypatch, raw)
    model = generator()
    with pytest.raises(worker.ModelStepError):
        model.generate(payload())
    result = model.last_transport_receipt
    assert logged_receipts(caplog) == [result]
    assert result["outcome"] == "incomplete"
    assert result["transport"]["terminal_received"] is terminal
    assert result["ollama"]["eval_count"] == (2000 if terminal else None)
    assert result["ollama"]["done_reason"] == ("length" if terminal else None)


@pytest.mark.parametrize("after_content", [False, True])
def test_generate_timeout_logs_once_without_fabricating_terminal_metrics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, after_content: bool
) -> None:
    caplog.set_level(logging.INFO, logger=worker.LOGGER.name)
    raw = event(_PRIVATE, done=False) if after_content else b""
    requests = install_transport(
        monkeypatch,
        raw,
        open_error=None if after_content else TimeoutError(_PRIVATE),
        end_error=TimeoutError(_PRIVATE) if after_content else None,
    )
    model = generator()
    with pytest.raises(worker.ModelTimeoutError):
        model.generate(payload())
    result = model.last_transport_receipt
    assert len(requests) == 1 and logged_receipts(caplog) == [result]
    assert result["outcome"] == "timeout" and result["failure_category"] == "timeout"
    assert result["transport"]["content_bytes"] == (len(_PRIVATE) if after_content else 0)
    assert (result["client_timing_ms"]["headers_ms"] is not None) is after_content
    assert result["transport"]["terminal_received"] is False
    assert result["ollama"]["eval_count"] is None
    assert result["ollama"]["prompt_eval_duration"] is None
    assert result["ollama"]["done_reason"] is None
    assert _PRIVATE not in caplog.text


@pytest.mark.parametrize("malformed_transport", [False, True])
def test_malformed_json_distinguishes_stream_failure_from_rejected_model_step(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    malformed_transport: bool,
) -> None:
    caplog.set_level(logging.INFO, logger=worker.LOGGER.name)
    raw = (
        b"not stream JSON " + _PRIVATE.encode() + b"\n"
        if malformed_transport
        else event("not project JSON " + _PRIVATE, done=True, done_reason="stop", eval_count=9)
    )
    install_transport(monkeypatch, raw)
    model = generator()
    with pytest.raises(worker.ModelStepError):
        model.generate(payload())
    result = model.last_transport_receipt
    assert logged_receipts(caplog) == [result]
    assert result["outcome"] == ("incomplete" if malformed_transport else "success")
    assert result["scope"] == "model_transport"
    assert result["ollama"]["eval_count"] == (None if malformed_transport else 9)
    assert _PRIVATE not in caplog.text


def test_missing_terminal_stats_and_next_timeout_cannot_reuse_prior_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = generator()
    install_transport(
        monkeypatch, event(json.dumps(step()), done=True, done_reason="stop", eval_count=100)
    )
    model.generate(payload())
    first = model.last_transport_receipt
    install_transport(monkeypatch, event(json.dumps(step()), done=True, done_reason="stop"))
    model.generate(payload())
    second = model.last_transport_receipt
    assert second["ollama"]["eval_count"] is None and model.last_metrics == {}
    install_transport(monkeypatch, open_error=TimeoutError(_PRIVATE))
    with pytest.raises(worker.ModelTimeoutError):
        model.generate(payload())
    third = model.last_transport_receipt
    assert len({item["attempt_id"] for item in (first, second, third)}) == 3
    assert third["ollama"]["eval_count"] is None and model.last_metrics == {}


def test_lost_lease_records_cancellation_and_propagates_without_model_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=worker.LOGGER.name)
    requests = install_transport(monkeypatch)
    model = generator()

    def cancelled() -> None:
        raise worker.protocol.LeaseLost(_PRIVATE)

    with pytest.raises(worker.protocol.LeaseLost):
        model.generate(payload(), cancelled)
    assert not requests
    assert logged_receipts(caplog) == [model.last_transport_receipt]
    assert model.last_transport_receipt["outcome"] == "cancelled"
    assert _PRIVATE not in caplog.text


@pytest.mark.parametrize("mode", ["connection", "http", "returned_status"])
def test_nonreturning_transport_errors_keep_a_safe_failure_category(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    error = (
        ConnectionRefusedError(_PRIVATE)
        if mode == "connection"
        else worker.urllib.error.HTTPError(
            "http://private.invalid/?token=" + _PRIVATE, 400, _PRIVATE, {}, None
        )
        if mode == "http"
        else None
    )
    install_transport(monkeypatch, open_error=error, response_status=400)
    model = generator()
    with pytest.raises(worker.ModelTransportError):
        model.generate(payload())
    result = model.last_transport_receipt
    assert result["outcome"] == "error"
    assert (
        result["failure_category"]
        == {
            "connection": "connection_error",
            "http": "http_error",
            "returned_status": "configuration_error",
        }[mode]
    )
    assert _PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("last_claim", ["none", "invalid", "lost"])
def test_run_once_attributes_from_claim_and_clears_ids_on_every_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, last_claim: str
) -> None:
    caplog.set_level(logging.INFO, logger=worker.LOGGER.name)
    install_transport(
        monkeypatch, event(json.dumps(step(clarify=True)), done=True, done_reason="stop")
    )
    claims = [
        {
            "id": "job_1",
            "goal_run_id": "goal_1",
            "required_skill": "code.build_project",
            "payload": payload(),
            "claim_token": _PRIVATE,
            "lease_id": "lease_1",
            "lease_generation": 1,
        },
        {
            "id": "job_2",
            "goal_run_id": "goal_2",
            "required_skill": "code.build_project",
            "payload": payload(),
            "claim_token": _PRIVATE,
            "lease_id": "lease_2",
            "lease_generation": 1,
        },
    ]
    final = dict(claims[-1], id="job_3", goal_run_id="goal_3")
    if last_claim == "invalid":
        final["payload"] = {}
    claims.append(None if last_claim == "none" else final)
    submitted = []
    current = 0

    def request(origin: str, path: str, token: str, method: str, body: Any, **kwargs: Any) -> Any:
        nonlocal current
        if path.endswith("/claim"):
            value = claims[current]
            current += 1
            return value
        if path.endswith("/result"):
            submitted.append(body)
        return {"status": "ok"}

    class Heartbeat:
        def __init__(self, *_args: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def ensure_active(self) -> None:
            if last_claim == "lost" and current == 3:
                raise worker.protocol.LeaseLost(_PRIVATE)

    class NoRunner:
        def run(self, *_args: Any, **_kwargs: Any) -> Any:
            pytest.fail("clarification must not execute project checks")

    monkeypatch.setattr(worker.protocol, "request", request)
    monkeypatch.setattr(worker.protocol, "LeaseHeartbeat", Heartbeat)
    model = generator()
    for _ in range(3):
        worker.run_once("https://control.example", "agt_1", _PRIVATE, model, NoRunner())
        assert model.receipt_job_id is None and model.receipt_goal_id is None
        assert not worker._JOB_LOCK.locked()
    receipts = logged_receipts(caplog)
    assert [(item["job_id"], item["goal_id"]) for item in receipts] == [
        ("job_1", "goal_1"),
        ("job_2", "goal_2"),
    ]
    assert receipts[0]["attempt_id"] != receipts[1]["attempt_id"]
    assert _PRIVATE not in caplog.text


def test_launcher_pins_transport_module_and_mounts_it_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = "workers/project-worker/model_transport.py"
    assert relative in launch_sandboxed.SOURCES
    release = tmp_path / "release"
    hashes = {}
    for name in launch_sandboxed.SOURCES:
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# qualification source\n")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (release / "release.json").write_text(json.dumps({"worker_sources": hashes}))
    for key in launch_sandboxed.REQUIRED_ENV:
        monkeypatch.setenv(key, "test-value")
    monkeypatch.setenv("MONGARS_PROJECT_RUNTIME_IMAGE", "sha256:" + "1" * 64)
    # This unit test covers immutable sources/binds, not real process privileges.
    # Model an operator-owned scratch even when the test runner itself is root.
    operator_uid = 1000
    monkeypatch.setattr(launch_sandboxed.os, "getuid", lambda: operator_uid)
    scratch = tmp_path / f"swarmer-project-worker-{operator_uid}"
    scratch.mkdir(mode=0o700)
    actual_stat = Path.stat

    def operator_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        metadata = actual_stat(path, follow_symlinks=follow_symlinks)
        if path == scratch:
            fields = list(metadata)
            fields[4] = operator_uid
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(Path, "stat", operator_stat)
    monkeypatch.setenv("MONGARS_PROJECT_SCRATCH_DIR", str(scratch))
    command, _ = launch_sandboxed.sandbox_command(release)
    binds = [command[i + 1 : i + 3] for i, arg in enumerate(command) if arg == "--ro-bind"]
    assert [str(release / relative), "/app/" + relative] in binds
    (release / relative).write_text("# modified\n")
    with pytest.raises(ValueError, match="manifest"):
        launch_sandboxed.sandbox_command(release)
