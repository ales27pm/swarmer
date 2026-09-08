from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import aiosqlite

from app.services.message_board import DurableEvent

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_MAX_ENVELOPE_BYTES = 1_000_000


class ConsumerDeliveryConflict(RuntimeError):
    """Raised when a duplicate durable identity carries different content."""


class ConsumerLeaseLost(RuntimeError):
    """Raised when a handler finishes after its fenced delivery lease was lost."""


@dataclass(frozen=True, slots=True)
class ConsumerCheckpoint:
    consumer_group: str
    last_event_id: str
    last_source_cursor: str | None
    completed_count: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class ConsumerDelivery:
    consumer_group: str
    consumer_id: str
    event: DurableEvent
    source_cursor: str | None
    attempt: int
    claim_generation: int
    claim_expires_at: str

    @property
    def idempotency_key(self) -> str:
        """Stable handler key; consumers must reuse it for downstream effects."""

        return f"{self.consumer_group}:{self.event.event_id}"


@dataclass(frozen=True, slots=True)
class ConsumerHandlerContext:
    consumer_group: str
    consumer_id: str
    event_id: str
    dedupe_key: str
    idempotency_key: str
    attempt: int


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    event_id: str
    inserted: bool


@dataclass(frozen=True, slots=True)
class ConsumerRunResult:
    claimed: int
    succeeded: int
    retrying: int
    dead_lettered: int
    lease_lost: int


class MessageHandler(Protocol):
    async def __call__(
        self,
        event: DurableEvent,
        context: ConsumerHandlerContext,
    ) -> None: ...


class ConsumerCheckpointStore:
    """SQLite delivery ledger for trusted control-plane consumers.

    Broker delivery is at-least-once. This ledger provides application-level
    idempotence, bounded retries, and fenced concurrent consumption without
    making the broker authoritative.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        claim_lease_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if claim_lease_seconds < 1 or claim_lease_seconds > 900:
            raise ValueError("consumer claim lease must be between 1 and 900 seconds")
        self.db_path = db_path
        self.claim_lease_seconds = claim_lease_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        """Create additive, restart-safe consumer tables."""

        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_consumer_deliveries (
                    consumer_group TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    source_cursor TEXT,
                    envelope_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','processing','completed','dead_letter')),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
                    claim_owner TEXT,
                    claim_generation INTEGER NOT NULL DEFAULT 0
                        CHECK(claim_generation >= 0),
                    claim_started_at TEXT,
                    claim_expires_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    dead_lettered_at TEXT,
                    PRIMARY KEY(consumer_group, event_id),
                    UNIQUE(consumer_group, dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS idx_message_consumer_claimable
                    ON message_consumer_deliveries(
                        consumer_group,status,claim_expires_at,created_at,event_id
                    );
                CREATE TABLE IF NOT EXISTS message_consumer_checkpoints (
                    consumer_group TEXT PRIMARY KEY,
                    last_event_id TEXT NOT NULL,
                    last_source_cursor TEXT,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            await db.commit()

    async def enqueue(
        self,
        consumer_group: str,
        event: DurableEvent,
        *,
        source_cursor: str | None = None,
        max_attempts: int = 3,
    ) -> EnqueueResult:
        self._validate_identity(consumer_group, label="consumer group")
        if max_attempts < 1 or max_attempts > 100:
            raise ValueError("consumer max attempts must be between 1 and 100")
        if source_cursor is not None and (not source_cursor or len(source_cursor) > 300):
            raise ValueError("consumer source cursor is invalid")
        envelope_json = self._encode_event(event)
        created_at = self._now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                INSERT INTO message_consumer_deliveries(
                    consumer_group,event_id,dedupe_key,source_cursor,envelope_json,
                    status,attempt_count,max_attempts,claim_generation,created_at
                ) VALUES(?,?,?,?,?,'pending',0,?,0,?)
                ON CONFLICT DO NOTHING
                """,
                (
                    consumer_group,
                    event.event_id,
                    event.dedupe_key,
                    source_cursor,
                    envelope_json,
                    max_attempts,
                    created_at,
                ),
            )
            inserted = cursor.rowcount == 1
            row = await (
                await db.execute(
                    """
                    SELECT event_id,envelope_json,source_cursor,max_attempts
                    FROM message_consumer_deliveries
                    WHERE consumer_group=? AND dedupe_key=?
                    """,
                    (consumer_group, event.dedupe_key),
                )
            ).fetchone()
            if row is None:
                conflicting_event = await (
                    await db.execute(
                        """
                        SELECT dedupe_key FROM message_consumer_deliveries
                        WHERE consumer_group=? AND event_id=?
                        """,
                        (consumer_group, event.event_id),
                    )
                ).fetchone()
                if conflicting_event is not None:
                    await db.rollback()
                    raise ConsumerDeliveryConflict(
                        "consumer event ID was reused with a different dedupe key"
                    )
                raise RuntimeError("consumer delivery disappeared")
            if not inserted and (
                str(row["event_id"]) != event.event_id
                or str(row["envelope_json"]) != envelope_json
                or row["source_cursor"] != source_cursor
                or int(row["max_attempts"]) != max_attempts
            ):
                await db.rollback()
                raise ConsumerDeliveryConflict(
                    "consumer dedupe key was reused for a different durable delivery"
                )
            await db.commit()
        return EnqueueResult(event_id=event.event_id, inserted=inserted)

    async def claim_batch(
        self,
        consumer_group: str,
        consumer_id: str,
        *,
        limit: int = 1,
        now: datetime | None = None,
    ) -> list[ConsumerDelivery]:
        self._validate_identity(consumer_group, label="consumer group")
        self._validate_identity(consumer_id, label="consumer identity")
        bounded_limit = max(1, min(limit, 100))
        current = self._utc(now)
        now_text = current.isoformat()
        expires_at = (current + timedelta(seconds=self.claim_lease_seconds)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await self._dead_letter_exhausted_locked(db, consumer_group, now_text)
            candidates = await (
                await db.execute(
                    """
                    SELECT event_id,claim_generation
                    FROM message_consumer_deliveries
                    WHERE consumer_group=? AND attempt_count < max_attempts AND (
                        status='pending'
                        OR (status='processing' AND claim_expires_at<=?)
                    )
                    ORDER BY created_at ASC,event_id ASC
                    LIMIT ?
                    """,
                    (consumer_group, now_text, bounded_limit),
                )
            ).fetchall()
            claimed_ids: list[str] = []
            for candidate in candidates:
                event_id = str(candidate["event_id"])
                generation = int(candidate["claim_generation"])
                cursor = await db.execute(
                    """
                    UPDATE message_consumer_deliveries
                    SET status='processing',attempt_count=attempt_count+1,
                        claim_owner=?,claim_generation=claim_generation+1,
                        claim_started_at=?,claim_expires_at=?,last_error=NULL
                    WHERE consumer_group=? AND event_id=? AND claim_generation=?
                      AND attempt_count < max_attempts AND (
                        status='pending'
                        OR (status='processing' AND claim_expires_at<=?)
                      )
                    """,
                    (
                        consumer_id,
                        now_text,
                        expires_at,
                        consumer_group,
                        event_id,
                        generation,
                        now_text,
                    ),
                )
                if cursor.rowcount == 1:
                    claimed_ids.append(event_id)
            rows: list[aiosqlite.Row] = []
            for event_id in claimed_ids:
                row = await (
                    await db.execute(
                        """
                        SELECT * FROM message_consumer_deliveries
                        WHERE consumer_group=? AND event_id=?
                        """,
                        (consumer_group, event_id),
                    )
                ).fetchone()
                if row is None:
                    raise RuntimeError("claimed consumer delivery disappeared")
                rows.append(row)
            await db.commit()
        return [self._delivery_from_row(row) for row in rows]

    async def acknowledge(
        self,
        delivery: ConsumerDelivery,
        *,
        now: datetime | None = None,
    ) -> bool:
        current = self._utc(now).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE message_consumer_deliveries
                SET status='completed',completed_at=?,last_error=NULL,
                    claim_owner=NULL,claim_started_at=NULL,claim_expires_at=NULL
                WHERE consumer_group=? AND event_id=? AND status='processing'
                  AND claim_owner=? AND claim_generation=? AND claim_expires_at>?
                """,
                (
                    current,
                    delivery.consumer_group,
                    delivery.event.event_id,
                    delivery.consumer_id,
                    delivery.claim_generation,
                    current,
                ),
            )
            if cursor.rowcount == 1:
                await db.execute(
                    """
                    INSERT INTO message_consumer_checkpoints(
                        consumer_group,last_event_id,last_source_cursor,completed_count,updated_at
                    ) VALUES(?,?,?,?,?)
                    ON CONFLICT(consumer_group) DO UPDATE SET
                        last_event_id=excluded.last_event_id,
                        last_source_cursor=excluded.last_source_cursor,
                        completed_count=message_consumer_checkpoints.completed_count+1,
                        updated_at=excluded.updated_at
                    """,
                    (
                        delivery.consumer_group,
                        delivery.event.event_id,
                        delivery.source_cursor,
                        1,
                        current,
                    ),
                )
            await db.commit()
        return cursor.rowcount == 1

    async def fail(
        self,
        delivery: ConsumerDelivery,
        error: BaseException,
        *,
        now: datetime | None = None,
    ) -> Literal["retry", "dead_letter"] | None:
        current = self._utc(now).isoformat()
        safe_error = f"{type(error).__name__}: handler failed"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    """
                    SELECT attempt_count,max_attempts FROM message_consumer_deliveries
                    WHERE consumer_group=? AND event_id=? AND status='processing'
                      AND claim_owner=? AND claim_generation=? AND claim_expires_at>?
                    """,
                    (
                        delivery.consumer_group,
                        delivery.event.event_id,
                        delivery.consumer_id,
                        delivery.claim_generation,
                        current,
                    ),
                )
            ).fetchone()
            if row is None:
                await db.commit()
                return None
            exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
            status = "dead_letter" if exhausted else "pending"
            cursor = await db.execute(
                """
                UPDATE message_consumer_deliveries
                SET status=?,last_error=?,claim_owner=NULL,claim_started_at=NULL,
                    claim_expires_at=NULL,dead_lettered_at=?
                WHERE consumer_group=? AND event_id=? AND status='processing'
                  AND claim_owner=? AND claim_generation=?
                """,
                (
                    status,
                    safe_error,
                    current if exhausted else None,
                    delivery.consumer_group,
                    delivery.event.event_id,
                    delivery.consumer_id,
                    delivery.claim_generation,
                ),
            )
            await db.commit()
        if cursor.rowcount != 1:
            return None
        return "dead_letter" if exhausted else "retry"

    async def recover_expired_claims(
        self,
        consumer_group: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        self._validate_identity(consumer_group, label="consumer group")
        current = self._utc(now).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            dead = await self._dead_letter_exhausted_locked(db, consumer_group, current)
            retry = await db.execute(
                """
                UPDATE message_consumer_deliveries
                SET status='pending',claim_owner=NULL,claim_started_at=NULL,
                    claim_expires_at=NULL,last_error='LeaseExpired: handler did not acknowledge'
                WHERE consumer_group=? AND status='processing' AND claim_expires_at<=?
                  AND attempt_count < max_attempts
                """,
                (consumer_group, current),
            )
            await db.commit()
        return {"retrying": retry.rowcount, "dead_lettered": dead}

    async def get_checkpoint(self, consumer_group: str) -> ConsumerCheckpoint | None:
        self._validate_identity(consumer_group, label="consumer group")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT consumer_group,last_event_id,last_source_cursor,
                           completed_count,updated_at
                    FROM message_consumer_checkpoints WHERE consumer_group=?
                    """,
                    (consumer_group,),
                )
            ).fetchone()
        if row is None:
            return None
        return ConsumerCheckpoint(
            consumer_group=str(row["consumer_group"]),
            last_event_id=str(row["last_event_id"]),
            last_source_cursor=(
                str(row["last_source_cursor"]) if row["last_source_cursor"] is not None else None
            ),
            completed_count=int(row["completed_count"]),
            updated_at=str(row["updated_at"]),
        )

    async def status_counts(self, consumer_group: str) -> dict[str, int]:
        self._validate_identity(consumer_group, label="consumer group")
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT status,COUNT(*) FROM message_consumer_deliveries
                    WHERE consumer_group=? GROUP BY status
                    """,
                    (consumer_group,),
                )
            ).fetchall()
        counts = {"pending": 0, "processing": 0, "completed": 0, "dead_letter": 0}
        for status, count in rows:
            counts[str(status)] = int(count)
        return counts

    async def list_dead_letters(
        self,
        consumer_group: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return metadata only; event payloads stay out of diagnostics."""

        self._validate_identity(consumer_group, label="consumer group")
        bounded_limit = max(1, min(limit, 500))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT event_id,dedupe_key,attempt_count,max_attempts,
                           last_error,dead_lettered_at
                    FROM message_consumer_deliveries
                    WHERE consumer_group=? AND status='dead_letter'
                    ORDER BY dead_lettered_at,event_id LIMIT ?
                    """,
                    (consumer_group, bounded_limit),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def _dead_letter_exhausted_locked(
        self,
        db: aiosqlite.Connection,
        consumer_group: str,
        now: str,
    ) -> int:
        cursor = await db.execute(
            """
            UPDATE message_consumer_deliveries
            SET status='dead_letter',claim_owner=NULL,claim_started_at=NULL,
                claim_expires_at=NULL,
                last_error='LeaseExpired: handler did not acknowledge',
                dead_lettered_at=?
            WHERE consumer_group=? AND status='processing' AND claim_expires_at<=?
              AND attempt_count >= max_attempts
            """,
            (now, consumer_group, now),
        )
        return cursor.rowcount

    def _delivery_from_row(self, row: aiosqlite.Row) -> ConsumerDelivery:
        raw = json.loads(str(row["envelope_json"]))
        if not isinstance(raw, dict):
            raise TypeError("persisted consumer envelope is invalid")
        event = DurableEvent(
            schema_version=str(raw["schema_version"]),
            event_id=str(raw["event_id"]),
            dedupe_key=str(raw["dedupe_key"]),
            topic=str(raw["topic"]),
            event_type=str(raw["event_type"]),
            aggregate_type=str(raw["aggregate_type"]),
            aggregate_id=str(raw["aggregate_id"]),
            task_id=str(raw["task_id"]) if raw.get("task_id") is not None else None,
            agent_id=str(raw["agent_id"]) if raw.get("agent_id") is not None else None,
            payload=self._object_payload(raw.get("payload")),
            created_at=str(raw["created_at"]),
        )
        claim_owner = row["claim_owner"]
        claim_expires_at = row["claim_expires_at"]
        if claim_owner is None or claim_expires_at is None:
            raise RuntimeError("persisted consumer claim is incomplete")
        return ConsumerDelivery(
            consumer_group=str(row["consumer_group"]),
            consumer_id=str(claim_owner),
            event=event,
            source_cursor=str(row["source_cursor"]) if row["source_cursor"] is not None else None,
            attempt=int(row["attempt_count"]),
            claim_generation=int(row["claim_generation"]),
            claim_expires_at=str(claim_expires_at),
        )

    @staticmethod
    def _object_payload(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError("persisted consumer payload is not an object")
        return {str(key): item for key, item in value.items()}

    @staticmethod
    def _encode_event(event: DurableEvent) -> str:
        encoded = json.dumps(
            event.as_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > _MAX_ENVELOPE_BYTES:
            raise ValueError("consumer event envelope is too large")
        return encoded

    @staticmethod
    def _validate_identity(value: str, *, label: str) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError(f"{label} has an invalid format")

    def _now(self) -> datetime:
        return self._utc(None)

    def _utc(self, value: datetime | None) -> datetime:
        current = value or self.clock()
        if current.tzinfo is None:
            raise RuntimeError("consumer clock must be timezone-aware")
        return current.astimezone(UTC)


class MessageConsumer:
    """Trusted control-plane handler runner backed by a persistent delivery ledger."""

    def __init__(
        self,
        store: ConsumerCheckpointStore,
        *,
        consumer_group: str,
        consumer_id: str,
        handler: MessageHandler,
        max_attempts: int = 3,
    ) -> None:
        ConsumerCheckpointStore._validate_identity(consumer_group, label="consumer group")
        ConsumerCheckpointStore._validate_identity(consumer_id, label="consumer identity")
        if max_attempts < 1 or max_attempts > 100:
            raise ValueError("consumer max attempts must be between 1 and 100")
        self.store = store
        self.consumer_group = consumer_group
        self.consumer_id = consumer_id
        self.handler = handler
        self.max_attempts = max_attempts

    async def initialize(self) -> None:
        await self.store.initialize()

    async def submit(
        self,
        event: DurableEvent,
        *,
        source_cursor: str | None = None,
    ) -> EnqueueResult:
        return await self.store.enqueue(
            self.consumer_group,
            event,
            source_cursor=source_cursor,
            max_attempts=self.max_attempts,
        )

    async def process_once(self, *, limit: int = 1) -> ConsumerRunResult:
        deliveries = await self.store.claim_batch(
            self.consumer_group,
            self.consumer_id,
            limit=limit,
        )
        succeeded = 0
        retrying = 0
        dead_lettered = 0
        lease_lost = 0
        for delivery in deliveries:
            context = ConsumerHandlerContext(
                consumer_group=delivery.consumer_group,
                consumer_id=delivery.consumer_id,
                event_id=delivery.event.event_id,
                dedupe_key=delivery.event.dedupe_key,
                idempotency_key=delivery.idempotency_key,
                attempt=delivery.attempt,
            )
            try:
                await self.handler(delivery.event, context)
            except Exception as exc:  # noqa: BLE001 - handler failures become bounded retries
                disposition = await self.store.fail(delivery, exc)
                if disposition == "retry":
                    retrying += 1
                elif disposition == "dead_letter":
                    dead_lettered += 1
                else:
                    lease_lost += 1
            else:
                if await self.store.acknowledge(delivery):
                    succeeded += 1
                else:
                    lease_lost += 1
        return ConsumerRunResult(
            claimed=len(deliveries),
            succeeded=succeeded,
            retrying=retrying,
            dead_lettered=dead_lettered,
            lease_lost=lease_lost,
        )

    async def require_process_once(self, *, limit: int = 1) -> ConsumerRunResult:
        result = await self.process_once(limit=limit)
        if result.lease_lost:
            raise ConsumerLeaseLost("consumer handler lost its delivery lease")
        return result
