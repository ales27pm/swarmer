import asyncio
import hashlib
import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.services.message_board import (
    DurableEvent,
    MessageBoardDedupeConflict,
    MessageBoardUnavailableError,
    RedisStreamsMessageBoard,
)


class FakeAsyncRedis:
    """Minimal async Redis double for the adapter's atomic dedupe script."""

    def __init__(self) -> None:
        self.available = True
        self.closed = False
        self.streams: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.dedupe_index: dict[str, str] = {}
        self.dedupe_expires_at: dict[str, int] = {}
        self.key_expires_at: dict[str, int] = {}
        self.eval_calls = 0
        self.server_now_ms = int(datetime.now(UTC).timestamp() * 1000)

    @staticmethod
    def _text(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    async def ping(self) -> bool:
        if not self.available:
            raise ConnectionError("redis unavailable")
        return True

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_arguments: object,
    ) -> list[bytes]:
        del script
        if not self.available:
            raise ConnectionError("redis unavailable")
        self.eval_calls += 1
        assert numkeys == 2
        assert len(keys_and_arguments) == 6
        dedupe_storage_name = self._text(keys_and_arguments[0])
        stream_name = self._text(keys_and_arguments[1])
        envelope_json = self._text(keys_and_arguments[2])
        binding_digest = self._text(keys_and_arguments[3])
        max_length = int(self._text(keys_and_arguments[4]))
        now_ms = self.server_now_ms
        retention_ms = int(self._text(keys_and_arguments[5]))
        requested_expiry = self.dedupe_expires_at.get(dedupe_storage_name)
        if requested_expiry is not None and requested_expiry <= now_ms:
            self.dedupe_expires_at.pop(dedupe_storage_name)
            self.dedupe_index.pop(dedupe_storage_name, None)
        existing = self.dedupe_index.get(dedupe_storage_name)
        if existing is not None:
            stored_digest, broker_id = existing.split("|", 1)
            return [broker_id.encode("utf-8"), b"1" if stored_digest == binding_digest else b"2"]
        broker_id = f"{len(self.streams[stream_name]) + 1}-0"
        envelope = json.loads(envelope_json)
        self.streams[stream_name].append(envelope)
        if len(self.streams[stream_name]) > max_length:
            self.streams[stream_name] = self.streams[stream_name][-max_length:]
        self.dedupe_index[dedupe_storage_name] = f"{binding_digest}|{broker_id}"
        self.dedupe_expires_at[dedupe_storage_name] = now_ms + retention_ms
        for key in (dedupe_storage_name, stream_name):
            self.key_expires_at[key] = now_ms + retention_ms
        return [broker_id.encode("utf-8"), b"0"]

    async def aclose(self) -> None:
        self.closed = True

    async def close(self) -> None:
        self.closed = True


def event(
    topic: str,
    *,
    event_id: str,
    dedupe_key: str,
) -> DurableEvent:
    return DurableEvent(
        schema_version="1.0",
        event_id=event_id,
        dedupe_key=dedupe_key,
        topic=topic,
        event_type="published",
        aggregate_type="agent_job",
        aggregate_id=f"job_{event_id}",
        task_id=f"task_{event_id}",
        agent_id=None,
        payload={"status": "queued"},
        created_at=datetime(2026, 9, 8, 16, 0, tzinfo=UTC).isoformat(),
    )


def redis_board(client: FakeAsyncRedis) -> RedisStreamsMessageBoard:
    return RedisStreamsMessageBoard(
        redis_url="rediss://unused.invalid/0",
        stream_prefix="mongars",
        redis_client=client,
        stream_max_length=10_000,
        stream_retention_seconds=86_400,
    )


@pytest.mark.asyncio
async def test_redis_routes_durable_events_to_bounded_stream_set() -> None:
    client = FakeAsyncRedis()
    board = redis_board(client)
    cases = {
        "tasks.inbox": "mongars:tasks",
        "agents.status": "mongars:agents",
        "iphone.capability.requested": "mongars:iphone",
        "telemetry.updated": "mongars:system",
    }

    for index, topic in enumerate(cases):
        durable = event(topic, event_id=f"evt_{index}", dedupe_key=f"dedupe-{index}")
        acknowledgement = await board.publish(durable)
        assert acknowledgement["event_id"] == durable.event_id
        assert acknowledgement["duplicate"] is False

    assert set(client.streams) == set(cases.values())
    for topic, stream_name in cases.items():
        [envelope] = client.streams[stream_name]
        assert envelope["topic"] == topic
        assert envelope["schema_version"] == "1.0"
        assert envelope["dedupe_key"]
        assert envelope["event_id"]


@pytest.mark.asyncio
async def test_redis_duplicate_application_event_is_acknowledged_without_second_xadd() -> None:
    client = FakeAsyncRedis()
    board = redis_board(client)
    durable = event("tasks.inbox", event_id="evt_same", dedupe_key="stable-domain-key")

    first = await board.publish(durable)
    duplicate = await board.publish(durable)

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert first["event_id"] == duplicate["event_id"] == durable.event_id
    assert len(client.streams["mongars:tasks"]) == 1


@pytest.mark.asyncio
async def test_redis_rejects_dedupe_key_rebound_to_different_envelope() -> None:
    client = FakeAsyncRedis()
    board = redis_board(client)
    original = event("tasks.inbox", event_id="evt_original", dedupe_key="stable-key")
    conflicting = DurableEvent(
        schema_version=original.schema_version,
        event_id="evt_conflicting",
        dedupe_key=original.dedupe_key,
        topic=original.topic,
        event_type="failed",
        aggregate_type=original.aggregate_type,
        aggregate_id=original.aggregate_id,
        task_id=original.task_id,
        agent_id=original.agent_id,
        payload={"status": "failed"},
        created_at=original.created_at,
    )

    await board.publish(original)
    with pytest.raises(MessageBoardDedupeConflict):
        await board.publish(conflicting)

    assert len(client.streams["mongars:tasks"]) == 1


@pytest.mark.asyncio
async def test_redis_health_transitions_degraded_then_recovers_without_leaking_url() -> None:
    client = FakeAsyncRedis()
    board = redis_board(client)
    before = await board.health()
    assert before["backend"] == "redis"
    assert before["status"] == "connected"

    client.available = False
    with pytest.raises(MessageBoardUnavailableError):
        await board.publish(event("system.status", event_id="evt_down", dedupe_key="system:down"))
    degraded = await board.health()
    assert degraded["status"] == "degraded"
    assert "redis_url" not in degraded
    assert "unused.invalid" not in json.dumps(degraded)

    client.available = True
    recovered = await board.health()
    acknowledgement = await board.publish(
        event("system.status", event_id="evt_up", dedupe_key="system:up")
    )
    after_publish = await board.health()

    assert recovered["status"] == "degraded"
    assert acknowledgement["duplicate"] is False
    assert after_publish["last_successful_publication"] is not None


@pytest.mark.asyncio
async def test_redis_publication_timeout_is_bounded_and_reports_degraded() -> None:
    class HangingRedis(FakeAsyncRedis):
        async def eval(
            self,
            script: str,
            numkeys: int,
            *keys_and_arguments: object,
        ) -> list[bytes]:
            del script, numkeys, keys_and_arguments
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    client = HangingRedis()
    board = RedisStreamsMessageBoard(
        redis_url="rediss://unused.invalid/0",
        stream_prefix="mongars",
        redis_client=client,
        operation_timeout_seconds=0.01,
    )

    with pytest.raises(MessageBoardUnavailableError):
        await asyncio.wait_for(
            board.publish(event("system.status", event_id="evt_timeout", dedupe_key="timeout")),
            timeout=0.2,
        )

    health = await board.health()
    assert health["status"] == "degraded"
    assert health["last_error_category"] == "timeout"


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://cache.example.invalid/0",
        "http://127.0.0.1:6379/0",
        "redis://127.0.0.1:invalid/0",
        "rediss://cache.example.invalid/0?ssl_cert_reqs=none",
        "rediss://cache.example.invalid/0?decode_responses=true",
    ],
)
def test_redis_transport_rejects_plaintext_remote_or_invalid_urls(redis_url: str) -> None:
    with pytest.raises(ValueError):
        RedisStreamsMessageBoard(redis_url=redis_url, redis_client=FakeAsyncRedis())


def test_redis_transport_allows_loopback_plaintext_and_remote_tls() -> None:
    local = RedisStreamsMessageBoard(
        redis_url="redis://127.0.0.1:6379/0",
        redis_client=FakeAsyncRedis(),
    )
    remote = RedisStreamsMessageBoard(
        redis_url="rediss://user:password@cache.example.invalid:6380/0",
        redis_client=FakeAsyncRedis(),
    )

    assert local.stream_prefix == "mongars"
    assert remote.stream_prefix == "mongars"


@pytest.mark.asyncio
async def test_redis_close_closes_injected_async_client() -> None:
    client = FakeAsyncRedis()
    board = redis_board(client)

    await board.close()

    assert client.closed is True


@pytest.mark.asyncio
async def test_redis_stream_retention_is_bounded_without_affecting_dedupe() -> None:
    client = FakeAsyncRedis()
    board = RedisStreamsMessageBoard(
        redis_url="rediss://unused.invalid/0",
        stream_prefix="bounded",
        redis_client=client,
        stream_max_length=3,
        stream_retention_seconds=3_600,
    )

    published = [
        event("tasks.inbox", event_id=f"evt_{index}", dedupe_key=f"key-{index}")
        for index in range(10)
    ]
    for durable in published:
        await board.publish(durable)

    assert len(client.streams["bounded:tasks"]) == 3
    duplicate = await board.publish(published[-1])
    assert duplicate["duplicate"] is True
    assert len(client.streams["bounded:tasks"]) == 3
    assert client.key_expires_at["bounded:tasks"] == client.server_now_ms + 3_600_000
    dedupe_keys = [key for key in client.key_expires_at if key.startswith("bounded:dedupe:")]
    assert len(dedupe_keys) == 10
    assert all(
        client.key_expires_at[key] == client.server_now_ms + 3_600_000 for key in dedupe_keys
    )


@pytest.mark.asyncio
async def test_redis_dedupe_bindings_expire_independently_under_large_backlog() -> None:
    client = FakeAsyncRedis()
    board = redis_board(client)
    durable = event("tasks.inbox", event_id="evt_target", dedupe_key="target-key")
    target = board._dedupe_storage_key(durable.dedupe_key)
    for index in range(600):
        filler = board._dedupe_storage_key(f"expired-filler-{index:04d}")
        client.dedupe_index[filler] = f"sha256:{index:064x}|0-{index}"
        client.dedupe_expires_at[filler] = 0
        client.key_expires_at[filler] = 0
    client.dedupe_index[target] = f"sha256:{'f' * 64}|0-target"
    client.dedupe_expires_at[target] = 1

    acknowledgement = await board.publish(durable)

    assert acknowledgement["duplicate"] is False
    assert len(client.streams["mongars:tasks"]) == 1
    assert client.dedupe_index[target].startswith(f"{durable.binding_digest}|")
    assert (
        sum(expiry > 0 for key, expiry in client.key_expires_at.items() if ":dedupe:" in key) == 1
    )


@pytest.mark.asyncio
async def test_redis_retention_uses_broker_clock_not_publisher_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FarFutureDateTime:
        @classmethod
        def now(cls, timezone: object) -> datetime:
            del timezone
            return datetime(2126, 1, 1, tzinfo=UTC)

    client = FakeAsyncRedis()
    client.server_now_ms = int(datetime(2026, 9, 8, tzinfo=UTC).timestamp() * 1000)
    board = redis_board(client)
    durable = event("tasks.inbox", event_id="evt_clock", dedupe_key="clock-key")
    monkeypatch.setattr("app.services.message_board.datetime", FarFutureDateTime)

    acknowledgement = await board.publish(durable)

    assert acknowledgement["duplicate"] is False
    assert len(client.streams["mongars:tasks"]) == 1
    storage_key = "mongars:dedupe:" + hashlib.sha256(b"clock-key").hexdigest()
    expiry = client.dedupe_expires_at[storage_key]
    assert expiry == client.server_now_ms + board.stream_retention_seconds * 1000


def test_redis_retention_settings_are_bounded() -> None:
    client = FakeAsyncRedis()
    with pytest.raises(ValueError, match="stream max length"):
        RedisStreamsMessageBoard(
            redis_url="rediss://unused.invalid/0",
            redis_client=client,
            stream_max_length=0,
        )
    with pytest.raises(ValueError, match="stream retention"):
        RedisStreamsMessageBoard(
            redis_url="rediss://unused.invalid/0",
            redis_client=client,
            stream_retention_seconds=30,
        )


REDIS_INTEGRATION_URL = os.environ.get("MONGARS_TEST_REDIS_URL")


@pytest.mark.integration
@pytest.mark.skipif(
    not REDIS_INTEGRATION_URL,
    reason="MONGARS_TEST_REDIS_URL is not configured",
)
@pytest.mark.asyncio
async def test_redis_streams_integration_smoke() -> None:
    assert REDIS_INTEGRATION_URL is not None
    board = RedisStreamsMessageBoard(
        redis_url=REDIS_INTEGRATION_URL,
        stream_prefix=f"mongars-test-{uuid4().hex}",
    )
    try:
        health = await board.health()
        durable = event(
            "system.integration",
            event_id=f"evt_{uuid4().hex}",
            dedupe_key=f"integration:{uuid4().hex}",
        )
        acknowledgement = await board.publish(durable)
    finally:
        await board.close()

    assert health["status"] == "connected"
    assert acknowledgement["event_id"] == durable.event_id
