"""Opt-in qualification tests for the real Redis Streams transport.

These tests never make Redis authoritative. They exercise only the disposable
notification stream and its application-level dedupe binding. The companion
``scripts/test-redis-integration.sh`` starts an authenticated loopback Redis
when Docker or Podman is available.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
import pytest

from app.services.message_board import (
    DurableEvent,
    MessageBoardDedupeConflict,
    MessageBoardUnavailableError,
    RedisStreamsMessageBoard,
)
from app.services.outbox import OutboxService
from app.services.state_service import StateService

REDIS_URL = os.environ.get("MONGARS_TEST_REDIS_URL")
WRONG_AUTH_URL = os.environ.get("MONGARS_TEST_REDIS_WRONG_AUTH_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not REDIS_URL,
        reason=(
            "real Redis qualification skipped: set MONGARS_TEST_REDIS_URL or run "
            "scripts/test-redis-integration.sh"
        ),
    ),
]


def _event(*, prefix: str, sequence: int = 0) -> DurableEvent:
    return DurableEvent(
        schema_version="1.0",
        event_id=f"evt_{prefix}_{sequence}",
        dedupe_key=f"integration:{prefix}:{sequence}",
        topic="tasks.integration",
        event_type="published",
        aggregate_type="agent_job",
        aggregate_id=f"job_{prefix}_{sequence}",
        task_id=f"task_{prefix}_{sequence}",
        agent_id=None,
        payload={"status": "queued", "sequence": sequence},
        created_at=datetime.now(UTC).isoformat(),
    )


@dataclass(slots=True)
class LiveRedis:
    prefix: str
    board: RedisStreamsMessageBoard
    admin: Any = field(repr=False)

    @property
    def task_stream(self) -> str:
        return f"{self.prefix}:tasks"


@pytest.fixture
async def live_redis() -> AsyncIterator[LiveRedis]:
    assert REDIS_URL is not None
    redis_asyncio = pytest.importorskip("redis.asyncio")
    prefix = f"mongars-qualification-{uuid4().hex}"
    board = RedisStreamsMessageBoard(
        redis_url=REDIS_URL,
        stream_prefix=prefix,
        operation_timeout_seconds=0.5,
        stream_max_length=10_000,
        stream_retention_seconds=3_600,
    )
    admin = redis_asyncio.Redis.from_url(
        REDIS_URL,
        decode_responses=False,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        retry_on_timeout=False,
    )
    assert await admin.ping()
    try:
        yield LiveRedis(prefix=prefix, board=board, admin=admin)
    finally:
        keys = [key async for key in admin.scan_iter(match=f"{prefix}:*")]
        if keys:
            await admin.delete(*keys)
        await board.close()
        await admin.aclose()


@pytest.mark.asyncio
async def test_live_authenticated_redis_and_wrong_password_rejection(live_redis: LiveRedis) -> None:
    health = await live_redis.board.health()
    assert health["status"] == "connected"

    if WRONG_AUTH_URL is None:
        pytest.skip("MONGARS_TEST_REDIS_WRONG_AUTH_URL is not configured")
    wrong_board = RedisStreamsMessageBoard(
        redis_url=WRONG_AUTH_URL,
        stream_prefix=f"wrong-auth-{uuid4().hex}",
        operation_timeout_seconds=0.2,
    )
    try:
        wrong_health = await wrong_board.health()
        assert wrong_health["status"] == "degraded"
        assert wrong_health["last_error_category"] == "authentication"
    finally:
        await wrong_board.close()


@pytest.mark.asyncio
async def test_live_duplicate_binding_survives_adapter_restart(live_redis: LiveRedis) -> None:
    assert REDIS_URL is not None
    durable = _event(prefix=uuid4().hex)
    first = await live_redis.board.publish(durable)

    restarted = RedisStreamsMessageBoard(
        redis_url=REDIS_URL,
        stream_prefix=live_redis.prefix,
        operation_timeout_seconds=0.5,
        stream_retention_seconds=3_600,
    )
    try:
        duplicate = await restarted.publish(durable)
        conflicting = DurableEvent(
            schema_version=durable.schema_version,
            event_id=f"{durable.event_id}_changed",
            dedupe_key=durable.dedupe_key,
            topic=durable.topic,
            event_type="failed",
            aggregate_type=durable.aggregate_type,
            aggregate_id=durable.aggregate_id,
            task_id=durable.task_id,
            agent_id=durable.agent_id,
            payload={"status": "failed"},
            created_at=durable.created_at,
        )
        with pytest.raises(MessageBoardDedupeConflict):
            await restarted.publish(conflicting)
    finally:
        await restarted.close()

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert first["backend_id"] == duplicate["backend_id"]
    assert await live_redis.admin.xlen(live_redis.task_stream) == 1


@pytest.mark.asyncio
async def test_live_dedupe_binding_expires_independently_of_other_traffic(
    live_redis: LiveRedis,
) -> None:
    """One binding's TTL cannot be extended by unrelated publications."""

    durable = _event(prefix=uuid4().hex)
    storage_key = (
        f"{live_redis.prefix}:dedupe:"
        f"{hashlib.sha256(durable.dedupe_key.encode('utf-8')).hexdigest()}"
    )
    await live_redis.admin.set(storage_key, f"sha256:{'f' * 64}|0-target", px=1)
    await asyncio.sleep(0.01)

    acknowledgement = await live_redis.board.publish(durable)

    assert acknowledgement["duplicate"] is False
    assert await live_redis.admin.xlen(live_redis.task_stream) == 1
    stored = await live_redis.admin.get(storage_key)
    assert stored is not None
    assert stored.decode("utf-8").startswith(f"{durable.binding_digest}|")
    refreshed_expiry = await live_redis.admin.pttl(storage_key)
    assert 0 < refreshed_expiry <= 3_600_000


@pytest.mark.asyncio
async def test_live_timeout_then_recovery_preserves_one_logical_event(
    live_redis: LiveRedis,
) -> None:
    """A lost XADD acknowledgement may be retried, but dedupe keeps one stream entry."""

    assert REDIS_URL is not None
    prefix = f"mongars-pause-{uuid4().hex}"
    board = RedisStreamsMessageBoard(
        redis_url=REDIS_URL,
        stream_prefix=prefix,
        operation_timeout_seconds=0.05,
        stream_retention_seconds=3_600,
    )
    durable = _event(prefix=uuid4().hex)
    try:
        # The pause is long enough to exceed the adapter's bounded operation
        # timeout. Redis may execute the queued Lua script later, which models
        # the important "remote commit, local acknowledgement lost" case.
        await live_redis.admin.execute_command("CLIENT", "PAUSE", 350, "ALL")
        with pytest.raises(MessageBoardUnavailableError):
            await board.publish(durable)

        degraded = await board.health()
        assert degraded["status"] == "degraded"
        assert degraded["last_error_category"] == "timeout"

        await asyncio.sleep(0.45)
        recovered = await board.publish(durable)
        health = await board.health()

        assert recovered["event_id"] == durable.event_id
        assert await live_redis.admin.xlen(f"{prefix}:tasks") == 1
        assert health["status"] == "connected"
        assert health["reconnect_count"] >= 1
    finally:
        keys = [key async for key in live_redis.admin.scan_iter(match=f"{prefix}:*")]
        if keys:
            await live_redis.admin.delete(*keys)
        await board.close()


@pytest.mark.asyncio
async def test_live_redis_timeout_leaves_outbox_pending_until_recovery(
    live_redis: LiveRedis,
    tmp_path: Path,
) -> None:
    """Redis acknowledgement loss cannot discard the authoritative outbox row."""

    assert REDIS_URL is not None
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    prefix = f"mongars-outbox-recovery-{uuid4().hex}"
    board = RedisStreamsMessageBoard(
        redis_url=REDIS_URL,
        stream_prefix=prefix,
        operation_timeout_seconds=0.05,
        stream_retention_seconds=3_600,
    )
    outbox = OutboxService(
        database,
        board,
        instance_id="live-redis-qualification",
        publication_lease_seconds=5,
    )
    durable = _event(prefix=uuid4().hex)
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        await OutboxService.enqueue_locked(
            db,
            aggregate_type=durable.aggregate_type,
            aggregate_id=durable.aggregate_id,
            topic=durable.topic,
            event_type=durable.event_type,
            payload=durable.payload,
            task_id=durable.task_id,
            agent_id=durable.agent_id,
            message_id=durable.aggregate_id,
            event_id=durable.event_id,
            dedupe_key=durable.dedupe_key,
            created_at=durable.created_at,
        )
        await db.commit()

    try:
        await live_redis.admin.execute_command("CLIENT", "PAUSE", 350, "ALL")
        first = await outbox.drain()
        assert first["published"] == 0
        assert first["failed"] == 1
        assert first["pending"] == 1
        assert await outbox.pending_count() == 1

        await asyncio.sleep(0.45)
        second = await outbox.drain()
        assert second["published"] == 1
        assert second["failed"] == 0
        assert second["pending"] == 0
        assert await outbox.pending_count() == 0
        assert await live_redis.admin.xlen(f"{prefix}:tasks") == 1
    finally:
        keys = [key async for key in live_redis.admin.scan_iter(match=f"{prefix}:*")]
        if keys:
            await live_redis.admin.delete(*keys)
        await board.close()


@pytest.mark.asyncio
async def test_live_connection_drop_reconnects(live_redis: LiveRedis) -> None:
    durable = _event(prefix=uuid4().hex)
    assert (await live_redis.board.publish(durable))["duplicate"] is False

    # Drop normal clients from a separate admin connection. The adapter must
    # reconnect rather than treating its original socket as durable state.
    await live_redis.admin.execute_command("CLIENT", "KILL", "TYPE", "normal", "SKIPME", "yes")
    replacement = _event(prefix=uuid4().hex)
    acknowledgement = await live_redis.board.publish(replacement)

    assert acknowledgement["duplicate"] is False
    assert await live_redis.admin.xlen(live_redis.task_stream) == 2


@pytest.mark.asyncio
async def test_live_large_backlog_is_trimmed_without_losing_dedupe(live_redis: LiveRedis) -> None:
    assert REDIS_URL is not None
    prefix = f"mongars-bounded-{uuid4().hex}"
    board = RedisStreamsMessageBoard(
        redis_url=REDIS_URL,
        stream_prefix=prefix,
        operation_timeout_seconds=1.0,
        stream_max_length=50,
        stream_retention_seconds=3_600,
    )
    events = [_event(prefix=uuid4().hex, sequence=index) for index in range(1_000)]
    try:
        for durable in events:
            acknowledgement = await board.publish(durable)
            assert acknowledgement["duplicate"] is False

        # Redis approximate MAXLEN trimming is intentionally not an exact cap;
        # it remains bounded to a small stream-node overshoot.
        length = await live_redis.admin.xlen(f"{prefix}:tasks")
        duplicate = await board.publish(events[0])
        assert length <= 150
        assert duplicate["duplicate"] is True
        assert await live_redis.admin.xlen(f"{prefix}:tasks") == length
        stream_ttl_ms = await live_redis.admin.pttl(f"{prefix}:tasks")
        assert 0 < stream_ttl_ms <= 3_600_000
        dedupe_keys = [key async for key in live_redis.admin.scan_iter(match=f"{prefix}:dedupe:*")]
        assert len(dedupe_keys) == len(events)
        for key in dedupe_keys:
            ttl_ms = await live_redis.admin.pttl(key)
            assert 0 < ttl_ms <= 3_600_000
    finally:
        keys = [key async for key in live_redis.admin.scan_iter(match=f"{prefix}:*")]
        if keys:
            await live_redis.admin.delete(*keys)
        await board.close()


@pytest.mark.asyncio
async def test_real_client_reports_unavailable_redis_without_startup_crash() -> None:
    board = RedisStreamsMessageBoard(
        redis_url="redis://127.0.0.1:1/0",
        stream_prefix=f"mongars-unavailable-{uuid4().hex}",
        operation_timeout_seconds=0.05,
    )
    try:
        health = await board.health()
        assert health["status"] == "degraded"
        assert health["last_error_category"] in {"connection", "timeout"}
        with pytest.raises(MessageBoardUnavailableError):
            await board.publish(_event(prefix=uuid4().hex))
    finally:
        await board.close()
