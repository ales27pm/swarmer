import asyncio
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
        self.dedupe_index: dict[tuple[str, str], str] = {}

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
        assert numkeys == 2
        assert len(keys_and_arguments) == 5
        dedupe_index_name = self._text(keys_and_arguments[0])
        stream_name = self._text(keys_and_arguments[1])
        dedupe_key = self._text(keys_and_arguments[2])
        envelope_json = self._text(keys_and_arguments[3])
        binding_digest = self._text(keys_and_arguments[4])
        index_key = (dedupe_index_name, dedupe_key)
        existing = self.dedupe_index.get(index_key)
        if existing is not None:
            stored_digest, broker_id = existing.split("|", 1)
            return [broker_id.encode("utf-8"), b"1" if stored_digest == binding_digest else b"2"]
        broker_id = f"{len(self.streams[stream_name]) + 1}-0"
        envelope = json.loads(envelope_json)
        self.streams[stream_name].append(envelope)
        self.dedupe_index[index_key] = f"{binding_digest}|{broker_id}"
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

    assert recovered["status"] == "connected"
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

    assert (await board.health())["status"] == "connected"


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
