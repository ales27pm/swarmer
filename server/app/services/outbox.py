from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite

if TYPE_CHECKING:
    from app.services.maintenance_lease import MaintenanceLeaseGuard

from app.services.audit_log import append_audit_event
from app.services.event_privacy import assert_safe_shared_payload
from app.services.message_board import (
    EVENT_SCHEMA_VERSION,
    DurableEvent,
    MessageBoard,
    MessageBoardUnavailableError,
)

_CLAIM_EXPIRED_AUDIT_EVENT = "outbox.publication.claim_expired"
_PUBLICATION_ACK_AUDIT_EVENT = "outbox.publication.acknowledged"
_RECOVERY_BATCH_SIZE = 50
_MAX_RECOVERIES_PER_INVOCATION = 500


class OutboxService:
    """Persist board publications beside domain state, then deliver them at least once."""

    def __init__(
        self,
        db_path: Path,
        board: MessageBoard,
        *,
        instance_id: str | None = None,
        publication_lease_seconds: int = 30,
    ) -> None:
        if publication_lease_seconds < 5 or publication_lease_seconds > 900:
            raise ValueError("outbox publication lease must be between 5 and 900 seconds")
        self.db_path = db_path
        self.board = board
        self.instance_id = instance_id or f"outbox_{uuid4().hex}"
        if not self.instance_id or len(self.instance_id) > 200:
            raise ValueError("outbox publisher instance id is invalid")
        self.publication_lease_seconds = publication_lease_seconds

    @staticmethod
    def _utc(value: datetime | None = None) -> datetime:
        current = value or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("outbox timestamps must be timezone-aware")
        return current.astimezone(UTC)

    @staticmethod
    async def enqueue_locked(
        db: aiosqlite.Connection,
        *,
        aggregate_type: str,
        aggregate_id: str,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        dedupe_key: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        message_id: str | None = None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> int:
        if not all((aggregate_type, aggregate_id, topic, event_type, dedupe_key)):
            raise ValueError("outbox identifiers are required")
        assert_safe_shared_payload(payload)
        selected_event_id = event_id or f"evt_{uuid4().hex}"
        selected_created_at = created_at or datetime.now(UTC).isoformat()
        candidate = DurableEvent(
            schema_version=EVENT_SCHEMA_VERSION,
            event_id=selected_event_id,
            dedupe_key=dedupe_key,
            topic=topic,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            task_id=task_id,
            agent_id=agent_id,
            payload=payload,
            created_at=selected_created_at,
        )
        payload_json = json.dumps(
            candidate.payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(payload_json.encode("utf-8")) > 1_000_000:
            raise ValueError("outbox payload is too large")
        cursor = await db.execute(
            """
            INSERT INTO outbox_events(
                event_id,aggregate_type,aggregate_id,topic,event_type,payload_json,
                message_id,task_id,agent_id,created_at,dedupe_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (
                selected_event_id,
                aggregate_type,
                aggregate_id,
                topic,
                event_type,
                payload_json,
                message_id or aggregate_id,
                task_id,
                agent_id,
                selected_created_at,
                dedupe_key,
            ),
        )
        if cursor.rowcount == 1:
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return the outbox identifier")
            return int(cursor.lastrowid)
        row = await (
            await db.execute(
                """SELECT id,event_id,topic,event_type,aggregate_type,aggregate_id,
                          task_id,agent_id,payload_json,created_at,dedupe_key
                FROM outbox_events WHERE dedupe_key=?""",
                (dedupe_key,),
            )
        ).fetchone()
        if row is None:
            raise RuntimeError("deduplicated outbox event disappeared")
        try:
            stored_payload = json.loads(str(row[8]))
        except json.JSONDecodeError as exc:
            raise ValueError("stored outbox dedupe binding is invalid") from exc
        if not isinstance(stored_payload, dict):
            raise TypeError("stored outbox dedupe binding is invalid")
        stored_event = DurableEvent(
            schema_version=EVENT_SCHEMA_VERSION,
            event_id=str(row[1]),
            dedupe_key=str(row[10]),
            topic=str(row[2]),
            event_type=str(row[3]),
            aggregate_type=str(row[4]),
            aggregate_id=str(row[5]),
            task_id=str(row[6]) if row[6] is not None else None,
            agent_id=str(row[7]) if row[7] is not None else None,
            payload=stored_payload,
            created_at=str(row[9]),
        )
        if not hmac.compare_digest(
            stored_event.domain_binding_digest,
            candidate.domain_binding_digest,
        ):
            raise ValueError("outbox dedupe key is bound to a different event")
        return int(row[0])

    async def claim_batch(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 500))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            # Preserve an explicit timestamp for deterministic recovery tests,
            # but never age an implicit publication lease while waiting for
            # SQLite's writer lock.
            claimed_at = self._utc(now)
            claimed_at_text = claimed_at.isoformat()
            lease_expires_at = (
                claimed_at + timedelta(seconds=self.publication_lease_seconds)
            ).isoformat()
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            candidates = list(
                await (
                    await db.execute(
                        """
                        SELECT id,event_id,task_id,publishing_owner,
                               publishing_lease_expires_at,publish_generation
                        FROM outbox_events
                        WHERE published_at IS NULL AND (
                            publishing_owner IS NULL
                            OR publishing_lease_expires_at IS NULL
                            OR publishing_lease_expires_at<=?
                        )
                        ORDER BY id ASC LIMIT ?
                        """,
                        (claimed_at_text, bounded_limit),
                    )
                ).fetchall()
            )
            claimed_ids: list[int] = []
            for candidate in candidates:
                outbox_id = int(candidate["id"])
                generation = int(candidate["publish_generation"])
                cursor = await db.execute(
                    """
                    UPDATE outbox_events
                    SET publishing_owner=?,publishing_started_at=?,
                        publishing_lease_expires_at=?,publish_generation=publish_generation+1
                    WHERE id=? AND published_at IS NULL AND publish_generation=? AND (
                        publishing_owner IS NULL
                        OR publishing_lease_expires_at IS NULL
                        OR publishing_lease_expires_at<=?
                    )
                    """,
                    (
                        self.instance_id,
                        claimed_at_text,
                        lease_expires_at,
                        outbox_id,
                        generation,
                        claimed_at_text,
                    ),
                )
                if cursor.rowcount == 1:
                    claimed_ids.append(outbox_id)
                    if candidate["publishing_owner"] is not None:
                        await self._record_claim_expired_locked(
                            db,
                            outbox_id=outbox_id,
                            event_id=str(candidate["event_id"]),
                            task_id=(
                                str(candidate["task_id"])
                                if candidate["task_id"] is not None
                                else None
                            ),
                            expired_generation=generation,
                            detected_at=claimed_at_text,
                        )
            rows: list[aiosqlite.Row] = []
            for outbox_id in claimed_ids:
                row = await (
                    await db.execute("SELECT * FROM outbox_events WHERE id=?", (outbox_id,))
                ).fetchone()
                if row is None:
                    raise RuntimeError("claimed outbox event disappeared")
                rows.append(row)
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return [dict(row) for row in rows]

    async def publish_claimed(
        self,
        row: Mapping[str, Any],
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, Any]:
        if row.get("publishing_owner") != self.instance_id:
            raise ValueError("outbox event is not owned by this publisher")
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise TypeError("outbox payload is not an object")
        event = DurableEvent(
            schema_version=EVENT_SCHEMA_VERSION,
            event_id=str(row["event_id"]),
            dedupe_key=str(row["dedupe_key"]),
            topic=str(row["topic"]),
            event_type=str(row["event_type"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=str(row["aggregate_id"]),
            task_id=str(row["task_id"]) if row["task_id"] is not None else None,
            agent_id=str(row["agent_id"]) if row["agent_id"] is not None else None,
            payload=payload,
            created_at=str(row["created_at"]),
        )
        if maintenance_guard is not None:
            maintenance_guard.raise_if_lost()
        started_ns = time.monotonic_ns()
        acknowledgement = await self.board.publish(event)
        latency_ms = max(0, (time.monotonic_ns() - started_ns) // 1_000_000)
        if maintenance_guard is not None:
            maintenance_guard.raise_if_lost()
        await self._record_publication_acknowledgement(
            row,
            duplicate=acknowledgement.get("duplicate") is True,
            latency_ms=latency_ms,
            maintenance_guard=maintenance_guard,
        )
        return acknowledgement

    async def drain(
        self,
        *,
        limit: int = 100,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, int]:
        rows = await self.claim_batch(limit=limit, maintenance_guard=maintenance_guard)
        published = 0
        failed = 0
        for index, row in enumerate(rows):
            outbox_id = int(row["id"])
            generation = int(row["publish_generation"])
            try:
                await self.publish_claimed(row, maintenance_guard=maintenance_guard)
                changed = await self.mark_published(
                    outbox_id,
                    owner_instance_id=self.instance_id,
                    publish_generation=generation,
                    maintenance_guard=maintenance_guard,
                )
                if changed:
                    published += 1
            except MessageBoardUnavailableError as exc:
                if maintenance_guard is not None:
                    maintenance_guard.raise_if_lost()
                changed = await self.release_or_fail(
                    outbox_id,
                    owner_instance_id=self.instance_id,
                    publish_generation=generation,
                    error=exc,
                    maintenance_guard=maintenance_guard,
                )
                if changed:
                    failed += 1
                for unattempted in rows[index + 1 :]:
                    await self.release_claim(
                        int(unattempted["id"]),
                        owner_instance_id=self.instance_id,
                        publish_generation=int(unattempted["publish_generation"]),
                        maintenance_guard=maintenance_guard,
                    )
                break
            # A swappable board adapter may expose its own exception hierarchy.
            # Cancellation remains a BaseException, while every publication error
            # must leave this durable row pending for a later drain.
            except Exception as exc:  # noqa: BLE001
                if maintenance_guard is not None:
                    maintenance_guard.raise_if_lost()
                changed = await self.release_or_fail(
                    outbox_id,
                    owner_instance_id=self.instance_id,
                    publish_generation=generation,
                    error=exc,
                    maintenance_guard=maintenance_guard,
                )
                if changed:
                    failed += 1
        return {
            "selected": len(rows),
            "published": published,
            "failed": failed,
            "pending": await self.pending_count(),
        }

    async def release_claim(
        self,
        outbox_id: int,
        *,
        owner_instance_id: str,
        publish_generation: int,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        """Release an unattempted fenced claim without recording a false failure."""

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            cursor = await db.execute(
                """UPDATE outbox_events
                SET publishing_owner=NULL,publishing_started_at=NULL,
                    publishing_lease_expires_at=NULL
                WHERE id=? AND published_at IS NULL AND publishing_owner=?
                  AND publish_generation=?""",
                (outbox_id, owner_instance_id, publish_generation),
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return cursor.rowcount == 1

    async def mark_published(
        self,
        outbox_id: int,
        owner_instance_id: str | None = None,
        publish_generation: int | None = None,
        now: datetime | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        owner = owner_instance_id or self.instance_id
        if publish_generation is None:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            # An implicit acknowledgement time must reflect when this fenced
            # write actually owns SQLite, not when it began waiting behind a
            # different writer. Explicit times remain deterministic for tests.
            published_at = self._utc(now).isoformat()
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            cursor = await db.execute(
                """
                UPDATE outbox_events
                SET published_at=?,last_error=NULL,publishing_owner=NULL,
                    publishing_started_at=NULL,publishing_lease_expires_at=NULL
                WHERE id=? AND published_at IS NULL AND publishing_owner=?
                  AND publish_generation=? AND publishing_lease_expires_at>?
                """,
                (published_at, outbox_id, owner, publish_generation, published_at),
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return cursor.rowcount == 1

    async def release_or_fail(
        self,
        outbox_id: int,
        *,
        owner_instance_id: str,
        publish_generation: int,
        error: BaseException,
        now: datetime | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        self._utc(now)
        safe_error = f"{type(error).__name__}: publication failed"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            cursor = await db.execute(
                """
                UPDATE outbox_events
                SET attempts=attempts+1,last_error=?,publishing_owner=NULL,
                    publishing_started_at=NULL,publishing_lease_expires_at=NULL
                WHERE id=? AND published_at IS NULL AND publishing_owner=?
                  AND publish_generation=?
                """,
                (safe_error, outbox_id, owner_instance_id, publish_generation),
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return cursor.rowcount == 1

    async def mark_failed(self, outbox_id: int, error: BaseException) -> bool:
        """Compatibility helper: fail only a row currently claimed by this publisher."""

        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT publish_generation FROM outbox_events
                    WHERE id=? AND published_at IS NULL AND publishing_owner=?
                    """,
                    (outbox_id, self.instance_id),
                )
            ).fetchone()
        if row is None:
            return False
        return await self.release_or_fail(
            outbox_id,
            owner_instance_id=self.instance_id,
            publish_generation=int(row[0]),
            error=error,
        )

    async def recover_expired_claims(
        self,
        *,
        now: datetime | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> int:
        current = self._utc(now).isoformat()
        recovered = 0
        while recovered < _MAX_RECOVERIES_PER_INVOCATION:
            if maintenance_guard is not None:
                await maintenance_guard.renew_now()
            batch_limit = min(
                _RECOVERY_BATCH_SIZE,
                _MAX_RECOVERIES_PER_INVOCATION - recovered,
            )
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                try:
                    if maintenance_guard is not None:
                        await maintenance_guard.require_current_locked(db)
                    batch_started_ns = time.monotonic_ns()
                    expired = list(
                        await (
                            await db.execute(
                                """
                                SELECT id,event_id,task_id,publish_generation
                                FROM outbox_events
                                WHERE published_at IS NULL AND publishing_owner IS NOT NULL
                                  AND (publishing_lease_expires_at IS NULL
                                       OR publishing_lease_expires_at<=?)
                                ORDER BY id ASC
                                LIMIT ?
                                """,
                                (current, batch_limit),
                            )
                        ).fetchall()
                    )
                    batch_recovered = 0
                    batch_examined = 0
                    for row in expired:
                        if (
                            batch_examined > 0
                            and maintenance_guard is not None
                            and (time.monotonic_ns() - batch_started_ns) / 1_000_000_000
                            >= maintenance_guard.mutation_batch_budget_seconds
                        ):
                            break
                        batch_examined += 1
                        cursor = await db.execute(
                            """
                            UPDATE outbox_events
                            SET publishing_owner=NULL,publishing_started_at=NULL,
                                publishing_lease_expires_at=NULL
                            WHERE id=? AND published_at IS NULL
                              AND publishing_owner IS NOT NULL
                              AND publish_generation=? AND (
                                publishing_lease_expires_at IS NULL
                                OR publishing_lease_expires_at<=?
                              )
                            """,
                            (int(row["id"]), int(row["publish_generation"]), current),
                        )
                        if cursor.rowcount != 1:
                            continue
                        batch_recovered += 1
                        await self._record_claim_expired_locked(
                            db,
                            outbox_id=int(row["id"]),
                            event_id=str(row["event_id"]),
                            task_id=(str(row["task_id"]) if row["task_id"] is not None else None),
                            expired_generation=int(row["publish_generation"]),
                            detected_at=current,
                        )
                    if maintenance_guard is not None:
                        await maintenance_guard.require_current_locked(db)
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
            recovered += batch_recovered
            if not expired or (batch_examined == len(expired) and len(expired) < batch_limit):
                break
            # Give the lease renewal task and ordinary SQLite writers a chance
            # between bounded write transactions.
            await asyncio.sleep(0)
        return recovered

    async def pending_count(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute("SELECT COUNT(*) FROM outbox_events WHERE published_at IS NULL")
            ).fetchone()
        if row is None:
            raise RuntimeError("SQLite did not return the outbox count")
        return int(row[0])

    async def metrics(self) -> dict[str, int]:
        """Return payload-free operational counts for authenticated status views."""

        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT
                      COUNT(*),
                      SUM(CASE WHEN publishing_owner IS NOT NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN attempts > 0 THEN 1 ELSE 0 END)
                    FROM outbox_events
                    WHERE published_at IS NULL
                    """
                )
            ).fetchone()
            operational = await (
                await db.execute(
                    """
                    SELECT claim_expirations,duplicate_publications,
                           publish_latency_ms_count,publish_latency_ms_total,
                           publish_latency_ms_max
                    FROM outbox_operational_metrics WHERE singleton_id=1
                    """
                )
            ).fetchone()
        if row is None:
            raise RuntimeError("SQLite did not return outbox metrics")
        if operational is None:
            raise RuntimeError("SQLite did not return outbox operational metrics")
        return {
            "outbox_pending": int(row[0] or 0),
            "outbox_publishing": int(row[1] or 0),
            "outbox_failed": int(row[2] or 0),
            "outbox_duplicate_publications": int(operational[1]),
            "outbox_claim_expirations": int(operational[0]),
            "outbox_publish_latency_ms_count": int(operational[2]),
            "outbox_publish_latency_ms_total": int(operational[3]),
            "outbox_publish_latency_ms_max": int(operational[4]),
        }

    async def _record_publication_acknowledgement(
        self,
        row: Mapping[str, Any],
        *,
        duplicate: bool,
        latency_ms: int,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> None:
        """Persist payload-free publication telemetry across process restarts."""

        outbox_id = int(row["id"])
        generation = int(row["publish_generation"])
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            persisted = await (
                await db.execute(
                    """
                    SELECT 1 FROM outbox_events
                    WHERE id=? AND event_id=? AND dedupe_key=?
                    """,
                    (outbox_id, str(row["event_id"]), str(row["dedupe_key"])),
                )
            ).fetchone()
            if persisted is None:
                await db.rollback()
                raise RuntimeError("acknowledged outbox event disappeared")
            await append_audit_event(
                db,
                _PUBLICATION_ACK_AUDIT_EVENT,
                {
                    "outbox_id": outbox_id,
                    "event_id": str(row["event_id"]),
                    "publish_generation": generation,
                    "duplicate": duplicate,
                    "latency_ms": latency_ms,
                },
                actor_type="control-plane",
                actor_id=self.instance_id,
                task_id=str(row["task_id"]) if row["task_id"] is not None else None,
                trace_id=f"outbox:{outbox_id}:publication:{generation}",
            )
            await db.execute(
                """
                UPDATE outbox_operational_metrics
                SET duplicate_publications=duplicate_publications+?,
                    publish_latency_ms_count=publish_latency_ms_count+1,
                    publish_latency_ms_total=publish_latency_ms_total+?,
                    publish_latency_ms_max=MAX(publish_latency_ms_max,?),
                    updated_at=?
                WHERE singleton_id=1
                """,
                (
                    1 if duplicate else 0,
                    latency_ms,
                    latency_ms,
                    datetime.now(UTC).isoformat(),
                ),
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()

    async def _record_claim_expired_locked(
        self,
        db: aiosqlite.Connection,
        *,
        outbox_id: int,
        event_id: str,
        task_id: str | None,
        expired_generation: int,
        detected_at: str,
    ) -> None:
        await append_audit_event(
            db,
            _CLAIM_EXPIRED_AUDIT_EVENT,
            {
                "outbox_id": outbox_id,
                "event_id": event_id,
                "expired_generation": expired_generation,
            },
            actor_type="control-plane",
            actor_id=self.instance_id,
            task_id=task_id,
            trace_id=f"outbox:{outbox_id}:claim-expired:{expired_generation}",
            created_at=detected_at,
        )
        await db.execute(
            """
            UPDATE outbox_operational_metrics
            SET claim_expirations=claim_expirations+1,updated_at=?
            WHERE singleton_id=1
            """,
            (detected_at,),
        )
