from __future__ import annotations

import threading
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from test_text_worker import FakeClient, FakeConnection, draft, payload, stream
from test_text_worker import worker as worker  # noqa: PLC0414


@pytest.mark.parametrize("seconds", [1, 120, 121, 300, 600])
def test_writer_accepts_operator_timeout_without_legacy_code_worker_cap(
    worker: ModuleType, seconds: float
) -> None:
    generator = worker.TextGenerator(
        "http://localhost:11434/v1", "installed:writer", timeout_seconds=seconds
    )
    assert generator.timeout_seconds == seconds
    assert generator._connection().timeout == min(10, seconds)
    assert generator.url == "http://127.0.0.1:11434/api/chat"
    assert generator.model == "installed:writer"


def test_writer_default_timeout_stays_120_seconds(worker: ModuleType) -> None:
    generator = worker.TextGenerator("http://127.0.0.1:11434", "installed:writer")
    assert generator.timeout_seconds == 120


@pytest.mark.parametrize(
    "seconds",
    [
        -1,
        0,
        0.5,
        601,
        True,
        False,
        float("nan"),
        float("inf"),
        -float("inf"),
        "300",
        None,
    ],
)
def test_writer_rejects_invalid_or_unbounded_timeout(worker: ModuleType, seconds: object) -> None:
    with pytest.raises(ValueError, match="timeout"):
        worker.TextGenerator("http://127.0.0.1:11434", "installed:writer", timeout_seconds=seconds)


@pytest.mark.parametrize("configured,expected", [(None, 120), ("300", 300), ("600", 600)])
def test_startup_reads_operator_timeout_with_unchanged_default(
    worker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    expected: int,
) -> None:
    monkeypatch.setattr("sys.argv", ["text_worker.py", "--once"])
    monkeypatch.setenv("MONGARS_TEXT_MODEL_ID", "installed:writer")
    monkeypatch.setenv("MONGARS_SERVER_URL", "http://127.0.0.1:8710")
    monkeypatch.setenv("MONGARS_AGENT_ID", "agent")
    monkeypatch.setenv("MONGARS_AGENT_CREDENTIAL", "credential")
    if configured is None:
        monkeypatch.delenv("MONGARS_TEXT_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MONGARS_TEXT_TIMEOUT_SECONDS", configured)
    observed = []

    def run(base: str, agent: str, credential: str, generator: Any, **kwargs: Any) -> bool:
        observed.append(generator.timeout_seconds)
        return False

    monkeypatch.setattr(worker, "run_once", run)
    worker.main()
    assert observed == [expected]


@pytest.mark.parametrize("configured", ["0", "601", "NaN", "inf", "true", ""])
def test_invalid_timeout_environment_fails_before_claiming(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    monkeypatch.setattr("sys.argv", ["text_worker.py", "--once"])
    monkeypatch.setenv("MONGARS_TEXT_MODEL_ID", "installed:writer")
    monkeypatch.setenv("MONGARS_TEXT_TIMEOUT_SECONDS", configured)
    monkeypatch.setattr(worker, "run_once", lambda *a, **k: pytest.fail("must not claim a job"))
    with pytest.raises(ValueError):
        worker.main()


@pytest.mark.parametrize("lease_lost", [False, True])
def test_extended_timeout_keeps_heartbeats_and_cancellation_without_retry(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, lease_lost: bool
) -> None:
    renewed = threading.Event()

    class RenewingClient(FakeClient):
        def heartbeat_job(self, job_id: str, lease: Any) -> None:
            super().heartbeat_job(job_id, lease)
            if self.renewals >= 3:
                renewed.set()
                if lease_lost:
                    raise worker.protocol.LeaseLost("cancelled")

    client = RenewingClient()
    monkeypatch.setattr(worker.protocol, "ControlPlaneClient", lambda *args: client)
    generator = worker.TextGenerator(
        "http://127.0.0.1:11434", "installed:writer", timeout_seconds=300
    )
    connection = FakeConnection(stream())
    connection.header_stall = lease_lost
    original_getresponse = connection.getresponse

    def wait_for_renewal() -> Any:
        assert renewed.wait(1), "background heartbeats must continue during generation"
        return original_getresponse()

    monkeypatch.setattr(connection, "getresponse", wait_for_renewal)
    monkeypatch.setattr(generator, "_connection", lambda: connection)

    assert worker.run_once(
        "http://127.0.0.1",
        "agent",
        "credential",
        generator,
        heartbeat_interval_seconds=0.01,
    )
    assert client.renewals >= 3
    assert client.submitted == ([] if lease_lost else [{"status": "completed", "result": draft()}])
    assert len([call for call in connection.calls if call[0] == "POST"]) == 1
    assert connection.initial_sock.closed.is_set()
    assert not worker._MODEL_LOCK.locked()


def test_job_cannot_extend_operator_timeout(worker: ModuleType) -> None:
    with pytest.raises(worker.GenerationError):
        worker.validate_payload({**payload(), "timeout_seconds": 600})


def test_extended_timeout_still_enforces_absolute_wall_budget(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    monkeypatch.setattr(worker.protocol, "ControlPlaneClient", lambda *args: client)
    generator = worker.TextGenerator(
        "http://127.0.0.1:11434", "installed:writer", timeout_seconds=300
    )
    connection = FakeConnection(stream())
    now = [0.0]

    def advance_past_deadline() -> None:
        now[0] = 301

    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: now[0]))
    connection.response.on_read = advance_past_deadline
    monkeypatch.setattr(generator, "_connection", lambda: connection)
    assert worker.run_once("http://127.0.0.1", "agent", "credential", generator)
    assert client.submitted == [{"status": "failed", "error": "wall_timeout"}]
    assert len([call for call in connection.calls if call[0] == "POST"]) == 1
    assert connection.initial_sock.closed.is_set()
    assert not worker._MODEL_LOCK.locked()
