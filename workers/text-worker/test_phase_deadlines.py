from __future__ import annotations

import threading
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from test_text_worker import FakeConnection, draft, event, payload, stream
from test_text_worker import worker as worker  # noqa: PLC0414


def timed_generator(
    worker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[bytes],
    **settings: float,
) -> tuple[Any, FakeConnection, list[float]]:
    generator = worker.TextGenerator("http://127.0.0.1", "local:writer", **settings)
    connection = FakeConnection(chunks)
    now = [0.0]
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(generator, "_connection", lambda: connection)
    return generator, connection, now


@pytest.mark.parametrize(
    "setting",
    [
        "connect_timeout_seconds",
        "first_content_timeout_seconds",
        "idle_timeout_seconds",
    ],
)
@pytest.mark.parametrize(
    "value", [0, 0.5, 601, True, False, float("nan"), float("inf"), "30", None]
)
def test_phase_settings_are_finite_operator_bounds(
    worker: ModuleType, setting: str, value: object
) -> None:
    with pytest.raises(ValueError, match="timeout"):
        worker.TextGenerator("http://127.0.0.1", "local:writer", **{setting: value})


def test_phase_default_and_maximum_settings_keep_absolute_cap(
    worker: ModuleType,
) -> None:
    generator = worker.TextGenerator("http://127.0.0.1", "local:writer")
    assert (
        generator.timeout_seconds,
        generator.connect_timeout_seconds,
        generator.first_content_timeout_seconds,
        generator.idle_timeout_seconds,
    ) == (120, 10, 120, 30)
    generator = worker.TextGenerator(
        "http://127.0.0.1",
        "local:writer",
        timeout_seconds=600,
        connect_timeout_seconds=600,
        first_content_timeout_seconds=600,
        idle_timeout_seconds=600,
    )
    assert generator._connection().timeout == 600


def test_connection_budget_expires_before_post(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator, connection, now = timed_generator(
        worker, monkeypatch, stream(), timeout_seconds=120, connect_timeout_seconds=10
    )

    def connect() -> None:
        connection.calls.append(("connect",))
        now[0] = 11

    monkeypatch.setattr(connection, "connect", connect)
    with pytest.raises(worker.GenerationError) as failed:
        generator.generate(payload(), ensure_active=lambda: None)
    assert failed.value.reason_code == "connection_timeout"
    assert connection.calls == [("connect",)] and connection.initial_sock.closed.is_set()


def test_first_content_budget_includes_headers_and_ignores_empty_events(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator, connection, now = timed_generator(
        worker,
        monkeypatch,
        [event(""), event("")] + stream(),
        timeout_seconds=120,
        first_content_timeout_seconds=5,
    )
    connection.response.on_read = lambda: now.__setitem__(0, now[0] + 3)
    with pytest.raises(worker.GenerationError) as failed:
        generator.generate(payload(), ensure_active=lambda: None)
    assert failed.value.reason_code == "first_content_timeout"
    assert len([x for x in connection.calls if x[0] == "POST"]) == 1
    assert connection.initial_sock.closed.is_set()


def test_progress_can_outlive_first_content_budget(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator, connection, now = timed_generator(
        worker,
        monkeypatch,
        stream(),
        timeout_seconds=30,
        first_content_timeout_seconds=5,
        idle_timeout_seconds=10,
    )
    ticks = iter((4, 8, 12, 13))
    connection.response.on_read = lambda: now.__setitem__(0, next(ticks))
    assert generator.generate(payload(), ensure_active=lambda: None) == draft()
    assert now[0] == 13 and now[0] > generator.first_content_timeout_seconds
    assert len([x for x in connection.calls if x[0] == "POST"]) == 1


def test_empty_or_thinking_only_events_do_not_reset_idle(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    thinking = b'{"message":{"role":"assistant","content":"","thinking":"private reasoning"},"done":false}\n'
    generator, connection, now = timed_generator(
        worker,
        monkeypatch,
        [event("{"), thinking, event("")] + stream(),
        timeout_seconds=120,
        idle_timeout_seconds=5,
    )
    ticks = iter((1, 4, 7))
    connection.response.on_read = lambda: now.__setitem__(0, next(ticks))
    with pytest.raises(worker.GenerationError) as failed:
        generator.generate(payload(), ensure_active=lambda: None)
    assert failed.value.reason_code == "idle_timeout"
    assert connection.initial_sock.closed.is_set()


@pytest.mark.parametrize("total,step", [(10, 4), (600, 201)])
def test_slow_continuous_progress_still_hits_absolute_cap(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, total: int, step: int
) -> None:
    generator, connection, now = timed_generator(
        worker,
        monkeypatch,
        stream(),
        timeout_seconds=total,
        first_content_timeout_seconds=600,
        idle_timeout_seconds=600,
    )
    connection.response.on_read = lambda: now.__setitem__(0, now[0] + step)
    with pytest.raises(worker.GenerationError) as failed:
        generator.generate(payload(), ensure_active=lambda: None)
    assert failed.value.reason_code == "wall_timeout"
    assert len([x for x in connection.calls if x[0] == "POST"]) == 1
    assert max(connection.initial_sock.timeouts) <= total


@pytest.mark.parametrize(
    "stage,reason",
    [
        ("connect", "connection_timeout"),
        ("headers", "first_content_timeout"),
        ("body", "idle_timeout"),
    ],
)
def test_native_socket_timeout_preserves_phase_without_retry(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, stage: str, reason: str
) -> None:
    generator, connection, _ = timed_generator(worker, monkeypatch, stream())
    reads = 0

    def expired(*args: Any) -> Any:
        raise TimeoutError("private socket endpoint")

    if stage == "body":

        def on_read() -> None:
            nonlocal reads
            reads += 1
            if reads == 2:
                expired()

        connection.response.on_read = on_read
    else:
        monkeypatch.setattr(connection, "connect" if stage == "connect" else "getresponse", expired)
    with pytest.raises(worker.GenerationError) as failed:
        generator.generate(payload(), ensure_active=lambda: None)
    assert failed.value.reason_code == reason
    assert "private" not in str(failed.value)
    assert len([x for x in connection.calls if x[0] == "POST"]) == (0 if stage == "connect" else 1)
    assert connection.initial_sock.closed.is_set()


@pytest.mark.parametrize("stage", ["connect", "headers", "body"])
def test_lease_cancellation_interrupts_each_phase_without_retry(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    generator = worker.TextGenerator("http://127.0.0.1", "local:writer", timeout_seconds=300)
    connection = FakeConnection(stream())
    stalled = threading.Event()

    def block(*args: Any) -> Any:
        stalled.set()
        assert connection.initial_sock.closed.wait(1)
        raise OSError("private blocked transport")

    monkeypatch.setattr(generator, "_connection", lambda: connection)
    monkeypatch.setattr(
        connection if stage != "body" else connection.response,
        {"connect": "connect", "headers": "getresponse", "body": "read1"}[stage],
        block,
    )

    def ensure_active() -> None:
        if stalled.is_set():
            raise worker.protocol.LeaseLost("cancelled")

    with pytest.raises(worker.protocol.LeaseLost):
        generator.generate(payload(), ensure_active=ensure_active)
    assert len([x for x in connection.calls if x[0] == "POST"]) == (0 if stage == "connect" else 1)
    assert connection.initial_sock.closed.is_set() and not worker._MODEL_LOCK.locked()


def test_startup_reads_separate_operator_phase_settings(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["text_worker.py", "--once"])
    for key, value in {
        "MONGARS_TEXT_MODEL_ID": "local:writer",
        "MONGARS_SERVER_URL": "http://127.0.0.1",
        "MONGARS_AGENT_ID": "agent",
        "MONGARS_AGENT_CREDENTIAL": "private",
        "MONGARS_TEXT_TIMEOUT_SECONDS": "300",
        "MONGARS_TEXT_CONNECT_TIMEOUT_SECONDS": "7",
        "MONGARS_TEXT_FIRST_CONTENT_TIMEOUT_SECONDS": "90",
        "MONGARS_TEXT_IDLE_TIMEOUT_SECONDS": "20",
    }.items():
        monkeypatch.setenv(key, value)
    observed = []

    def run(base: str, agent: str, credential: str, generator: Any, **kwargs: Any) -> bool:
        observed.append(
            (
                generator.timeout_seconds,
                generator.connect_timeout_seconds,
                generator.first_content_timeout_seconds,
                generator.idle_timeout_seconds,
            )
        )
        return False

    monkeypatch.setattr(worker, "run_once", run)
    worker.main()
    assert observed == [(300, 7, 90, 20)]
