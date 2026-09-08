from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, overload
from urllib.parse import urlsplit
from uuid import uuid4

import aiosqlite

from app.services.event_privacy import assert_safe_shared_payload

EVENT_SCHEMA_VERSION = "1.0"
MAX_EVENT_BYTES = 1_000_000


def _canonical_json(value: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("message board payload must be canonical JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
        raise ValueError("message board payload is too large")
    return encoded


def durable_event_binding_digest(
    *,
    schema_version: str,
    event_id: str,
    created_at: str,
    topic: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    task_id: str | None,
    agent_id: str | None,
    payload: dict[str, Any],
) -> str:
    """Bind a dedupe key to the complete immutable durable envelope."""

    encoded = _canonical_json(
        {
            "schema_version": schema_version,
            "event_id": event_id,
            "created_at": created_at,
            "topic": topic,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "payload": payload,
        }
    )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def domain_event_binding_digest(
    *,
    schema_version: str,
    topic: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    task_id: str | None,
    agent_id: str | None,
    payload: dict[str, Any],
) -> str:
    """Bind an outbox idempotency key while excluding generated retry metadata."""

    encoded = _canonical_json(
        {
            "schema_version": schema_version,
            "topic": topic,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "payload": payload,
        }
    )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class DurableEvent:
    """Backend-neutral, application-identified durable event envelope."""

    schema_version: str
    event_id: str
    dedupe_key: str
    topic: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    created_at: str
    task_id: str | None = None
    agent_id: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError("unsupported durable event schema version")
        required = {
            "event_id": self.event_id,
            "dedupe_key": self.dedupe_key,
            "topic": self.topic,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
        }
        for label, value in required.items():
            if not value or len(value) > 300:
                raise ValueError(f"durable event {label} is invalid")
        if not isinstance(self.payload, dict):
            raise TypeError("durable event payload must be an object")
        assert_safe_shared_payload(self.payload)
        encoded_payload = _canonical_json(self.payload)
        try:
            created = datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise ValueError("durable event timestamp is invalid") from exc
        if created.tzinfo is None:
            raise ValueError("durable event timestamp must be timezone-aware")
        canonical_payload = json.loads(encoded_payload)
        if not isinstance(canonical_payload, dict):  # pragma: no cover - dict input is preserved
            raise TypeError("durable event payload must be an object")
        object.__setattr__(self, "payload", canonical_payload)

    @property
    def binding_digest(self) -> str:
        return durable_event_binding_digest(
            schema_version=self.schema_version,
            event_id=self.event_id,
            created_at=self.created_at,
            topic=self.topic,
            event_type=self.event_type,
            aggregate_type=self.aggregate_type,
            aggregate_id=self.aggregate_id,
            task_id=self.task_id,
            agent_id=self.agent_id,
            payload=self.payload,
        )

    @property
    def domain_binding_digest(self) -> str:
        return domain_event_binding_digest(
            schema_version=self.schema_version,
            topic=self.topic,
            event_type=self.event_type,
            aggregate_type=self.aggregate_type,
            aggregate_id=self.aggregate_id,
            task_id=self.task_id,
            agent_id=self.agent_id,
            payload=self.payload,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "dedupe_key": self.dedupe_key,
            "topic": self.topic,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


class MessageBoardUnavailableError(RuntimeError):
    """A transport failure that is safe for the transactional outbox to retry."""


class MessageBoardDedupeConflict(ValueError):
    """A dedupe key was reused for different immutable domain content."""


class MessageBoard(Protocol):
    async def publish(self, event: DurableEvent) -> dict[str, Any]: ...

    async def health(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class SQLiteMessageBoard:
    """SQLite notification transport; authoritative domain state remains elsewhere."""

    EVENT_TYPES = frozenset(
        {
            "published",
            "claimed",
            "acked",
            "failed",
            "heartbeat",
            "cancelled",
            "lease_expired",
            "dead_lettered",
            "capability_requested",
            "capability_authorized",
            "capability_consumed",
            "capability_result",
        }
    )

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.last_successful_publication: str | None = None

    @overload
    async def publish(self, event: DurableEvent) -> dict[str, Any]: ...

    @overload
    async def publish(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        message_id: str | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]: ...

    async def publish(
        self,
        event: DurableEvent | str,
        payload: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        message_id: str | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(event, DurableEvent):
            if payload is not None:
                raise TypeError("a durable event does not accept a second payload")
            return await self._publish_event(event)
        if payload is None:
            raise TypeError("legacy publication requires a payload")
        return await self.record(
            "published",
            message_id or f"mb_{uuid4().hex}",
            topic=event,
            task_id=task_id,
            agent_id=agent_id,
            payload=payload,
            dedupe_key=dedupe_key,
        )

    async def _publish_event(self, event: DurableEvent) -> dict[str, Any]:
        assert_safe_shared_payload(event.payload)
        encoded = _canonical_json(event.payload)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO message_board_events(
                    schema_version,event_id,topic,event_type,message_id,
                    aggregate_type,aggregate_id,agent_id,task_id,payload_json,
                    created_at,dedupe_key
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (
                    event.schema_version,
                    event.event_id,
                    event.topic,
                    event.event_type,
                    event.aggregate_id,
                    event.aggregate_type,
                    event.aggregate_id,
                    event.agent_id,
                    event.task_id,
                    encoded,
                    event.created_at,
                    event.dedupe_key,
                ),
            )
            duplicate = cursor.rowcount != 1
            if duplicate:
                row = await (
                    await db.execute(
                        """SELECT id,event_id,created_at,schema_version,topic,event_type,
                                  aggregate_type,aggregate_id,task_id,agent_id,payload_json
                        FROM message_board_events
                        WHERE dedupe_key=?""",
                        (event.dedupe_key,),
                    )
                ).fetchone()
                if row is None:
                    raise RuntimeError("deduplicated message board event disappeared")
                backend_id, stored_event_id, created_at = int(row[0]), str(row[1]), str(row[2])
                try:
                    stored_payload = json.loads(str(row[10]))
                except json.JSONDecodeError as exc:
                    raise MessageBoardDedupeConflict("stored dedupe binding is invalid") from exc
                if not isinstance(stored_payload, dict):
                    raise MessageBoardDedupeConflict("stored dedupe binding is invalid")
                stored_digest = durable_event_binding_digest(
                    schema_version=str(row[3]),
                    event_id=str(row[1]),
                    created_at=str(row[2]),
                    topic=str(row[4]),
                    event_type=str(row[5]),
                    aggregate_type=str(row[6]),
                    aggregate_id=str(row[7]),
                    task_id=str(row[8]) if row[8] is not None else None,
                    agent_id=str(row[9]) if row[9] is not None else None,
                    payload=stored_payload,
                )
                if not hmac.compare_digest(stored_digest, event.binding_digest):
                    raise MessageBoardDedupeConflict(
                        "message board dedupe key is bound to a different event"
                    )
            else:
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return the board event identifier")
                backend_id = int(cursor.lastrowid)
                stored_event_id = event.event_id
                created_at = event.created_at
            await db.commit()
        self.last_successful_publication = datetime.now(UTC).isoformat()
        return {
            "backend": "sqlite",
            "backend_id": str(backend_id),
            "id": backend_id,
            "event_id": stored_event_id,
            "dedupe_key": event.dedupe_key,
            "duplicate": duplicate,
            "topic": event.topic,
            "event_type": event.event_type,
            "message_id": event.aggregate_id,
            "agent_id": event.agent_id,
            "task_id": event.task_id,
            "payload": dict(event.payload),
            "created_at": created_at,
        }

    async def record(
        self,
        event_type: str,
        message_id: str,
        *,
        topic: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        if event_type not in self.EVENT_TYPES:
            raise ValueError("unsupported message board event")
        if not topic or len(topic) > 200 or not message_id or len(message_id) > 200:
            raise ValueError("invalid message board identifier")
        created_at = datetime.now(UTC).isoformat()
        durable = DurableEvent(
            schema_version=EVENT_SCHEMA_VERSION,
            event_id=f"evt_{uuid4().hex}",
            dedupe_key=dedupe_key or f"legacy:{uuid4().hex}",
            topic=topic,
            event_type=event_type,
            aggregate_type="message",
            aggregate_id=message_id,
            task_id=task_id,
            agent_id=agent_id,
            payload=payload or {},
            created_at=created_at,
        )
        return await self._publish_event(durable)

    async def claim(self, message_id: str, *, topic: str, agent_id: str) -> dict[str, Any]:
        return await self.record("claimed", message_id, topic=topic, agent_id=agent_id)

    async def ack(self, message_id: str, *, topic: str, agent_id: str) -> dict[str, Any]:
        return await self.record("acked", message_id, topic=topic, agent_id=agent_id)

    async def fail(
        self, message_id: str, *, topic: str, agent_id: str, reason: str
    ) -> dict[str, Any]:
        return await self.record(
            "failed", message_id, topic=topic, agent_id=agent_id, payload={"reason": reason}
        )

    async def heartbeat(self, message_id: str, *, topic: str, agent_id: str) -> dict[str, Any]:
        return await self.record("heartbeat", message_id, topic=topic, agent_id=agent_id)

    async def list_events(
        self, *, topic: str | None = None, after_id: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 500))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if topic is None:
                rows = await (
                    await db.execute(
                        "SELECT * FROM message_board_events WHERE id>? ORDER BY id LIMIT ?",
                        (after_id, bounded_limit),
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        """SELECT * FROM message_board_events
                        WHERE id>? AND topic=? ORDER BY id LIMIT ?""",
                        (after_id, topic, bounded_limit),
                    )
                ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            events.append(value)
        return events

    async def health(self) -> dict[str, Any]:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await (await db.execute("SELECT 1")).fetchone()
        except aiosqlite.Error:
            return {
                "backend": "sqlite",
                "status": "degraded",
                "last_successful_publication": self.last_successful_publication,
            }
        return {
            "backend": "sqlite",
            "status": "connected",
            "last_successful_publication": self.last_successful_publication,
        }

    async def close(self) -> None:
        return None


class AsyncRedisClient(Protocol):
    async def ping(self) -> object: ...

    async def eval(self, script: str, numkeys: int, *values: object) -> object: ...

    async def aclose(self) -> None: ...


def _validate_redis_transport_url(redis_url: str) -> bool:
    try:
        parsed = urlsplit(redis_url)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Redis URL is invalid") from exc
    if (
        parsed.scheme not in {"redis", "rediss"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Redis URL is invalid")
    hostname = parsed.hostname.casefold().strip("[]")
    loopback = hostname == "localhost" or hostname.endswith(".localhost")
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme == "redis" and not loopback:
        raise ValueError("TLS is required for Redis outside loopback")
    return parsed.scheme == "rediss"


class RedisStreamsMessageBoard:
    """Optional Redis Streams notification adapter with application-level deduplication."""

    _PUBLISH_SCRIPT = """
local existing = redis.call('HGET', KEYS[1], ARGV[1])
if existing then
  local separator = string.find(existing, '|', 1, true)
  if not separator then
    return {existing, '2'}
  end
  local stored_digest = string.sub(existing, 1, separator - 1)
  local existing_id = string.sub(existing, separator + 1)
  if stored_digest ~= ARGV[3] then
    return {existing_id, '2'}
  end
  return {existing_id, '1'}
end
local broker_id = redis.call('XADD', KEYS[2], '*', 'envelope', ARGV[2])
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3] .. '|' .. broker_id)
return {broker_id, '0'}
"""

    def __init__(
        self,
        *,
        redis_url: str,
        stream_prefix: str = "mongars",
        redis_client: AsyncRedisClient | None = None,
        operation_timeout_seconds: float = 2.0,
    ) -> None:
        tls_required = _validate_redis_transport_url(redis_url)
        normalized_prefix = stream_prefix.strip(": ")
        if not normalized_prefix or len(normalized_prefix) > 100:
            raise ValueError("Redis stream prefix is invalid")
        if not 0.001 <= operation_timeout_seconds <= 30:
            raise ValueError("Redis operation timeout is invalid")
        self.stream_prefix = normalized_prefix
        self.operation_timeout_seconds = operation_timeout_seconds
        self._last_successful_publication: str | None = None
        self._degraded = False
        if redis_client is None:
            try:
                from redis.asyncio import Redis
            except ImportError as exc:  # pragma: no cover - depends on optional package
                raise RuntimeError(
                    "Redis message board requires the optional 'redis' dependency"
                ) from exc
            if tls_required:
                self._client = Redis.from_url(
                    redis_url,
                    decode_responses=False,
                    socket_connect_timeout=operation_timeout_seconds,
                    socket_timeout=operation_timeout_seconds,
                    retry_on_timeout=False,
                    ssl_cert_reqs="required",
                )
            else:
                self._client = Redis.from_url(
                    redis_url,
                    decode_responses=False,
                    socket_connect_timeout=operation_timeout_seconds,
                    socket_timeout=operation_timeout_seconds,
                    retry_on_timeout=False,
                )
        else:
            self._client = redis_client

    def _stream_name(self, topic: str) -> str:
        head = topic.split(".", 1)[0]
        category = head if head in {"tasks", "agents", "iphone"} else "system"
        return f"{self.stream_prefix}:{category}"

    @staticmethod
    def _decoded(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    async def publish(self, event: DurableEvent) -> dict[str, Any]:
        assert_safe_shared_payload(event.payload)
        envelope = _canonical_json(event.as_dict())
        stream = self._stream_name(event.topic)
        try:
            async with asyncio.timeout(self.operation_timeout_seconds):
                raw = await self._client.eval(
                    self._PUBLISH_SCRIPT,
                    2,
                    f"{self.stream_prefix}:dedupe",
                    stream,
                    event.dedupe_key,
                    envelope,
                    event.binding_digest,
                )
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise RuntimeError("Redis returned an invalid publication acknowledgement")
            broker_id = self._decoded(raw[0])
            outcome = self._decoded(raw[1])
        except Exception as exc:
            self._degraded = True
            raise MessageBoardUnavailableError("message board publication unavailable") from exc
        if outcome == "2":
            self._degraded = False
            raise MessageBoardDedupeConflict("Redis dedupe key is bound to a different event")
        if outcome not in {"0", "1"}:
            self._degraded = True
            raise MessageBoardUnavailableError(
                "message board publication returned an invalid dedupe outcome"
            )
        duplicate = outcome == "1"
        self._degraded = False
        self._last_successful_publication = datetime.now(UTC).isoformat()
        return {
            "backend": "redis",
            "backend_id": broker_id,
            "broker_id": broker_id,
            "event_id": event.event_id,
            "dedupe_key": event.dedupe_key,
            "duplicate": duplicate,
            "stream": stream,
        }

    async def health(self) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.operation_timeout_seconds):
                await self._client.ping()
        except Exception:  # noqa: BLE001 - health never exposes adapter details
            self._degraded = True
        else:
            self._degraded = False
        return {
            "backend": "redis",
            "status": "degraded" if self._degraded else "connected",
            "last_successful_publication": self._last_successful_publication,
        }

    async def close(self) -> None:
        await self._client.aclose()


# Compatibility alias for the established SQLite-default API.
MessageBoardService = SQLiteMessageBoard
