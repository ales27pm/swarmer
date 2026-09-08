from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.services.event_privacy import assert_safe_shared_payload
from app.services.message_board import (
    EVENT_SCHEMA_VERSION,
    DurableEvent,
    MessageBoard,
    MessageBoardUnavailableError,
)


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
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 500))
        claimed_at = self._utc(now)
        claimed_at_text = claimed_at.isoformat()
        lease_expires_at = (
            claimed_at + timedelta(seconds=self.publication_lease_seconds)
        ).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            candidates = list(
                await (
                    await db.execute(
                        """
                        SELECT id,publish_generation FROM outbox_events
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
            rows: list[aiosqlite.Row] = []
            for outbox_id in claimed_ids:
                row = await (
                    await db.execute("SELECT * FROM outbox_events WHERE id=?", (outbox_id,))
                ).fetchone()
                if row is None:
                    raise RuntimeError("claimed outbox event disappeared")
                rows.append(row)
            await db.commit()
        return [dict(row) for row in rows]

    async def publish_claimed(self, row: Mapping[str, Any]) -> dict[str, Any]:
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
        return await self.board.publish(event)

    async def drain(self, *, limit: int = 100) -> dict[str, int]:
        rows = await self.claim_batch(limit=limit)
        published = 0
        failed = 0
        for index, row in enumerate(rows):
            outbox_id = int(row["id"])
            generation = int(row["publish_generation"])
            try:
                await self.publish_claimed(row)
                changed = await self.mark_published(
                    outbox_id,
                    owner_instance_id=self.instance_id,
                    publish_generation=generation,
                )
                if changed:
                    published += 1
            except MessageBoardUnavailableError as exc:
                changed = await self.release_or_fail(
                    outbox_id,
                    owner_instance_id=self.instance_id,
                    publish_generation=generation,
                    error=exc,
                )
                if changed:
                    failed += 1
                for unattempted in rows[index + 1 :]:
                    await self.release_claim(
                        int(unattempted["id"]),
                        owner_instance_id=self.instance_id,
                        publish_generation=int(unattempted["publish_generation"]),
                    )
                break
            # A swappable board adapter may expose its own exception hierarchy.
            # Cancellation remains a BaseException, while every publication error
            # must leave this durable row pending for a later drain.
            except Exception as exc:  # noqa: BLE001
                changed = await self.release_or_fail(
                    outbox_id,
                    owner_instance_id=self.instance_id,
                    publish_generation=generation,
                    error=exc,
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
    ) -> bool:
        """Release an unattempted fenced claim without recording a false failure."""

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """UPDATE outbox_events
                SET publishing_owner=NULL,publishing_started_at=NULL,
                    publishing_lease_expires_at=NULL
                WHERE id=? AND published_at IS NULL AND publishing_owner=?
                  AND publish_generation=?""",
                (outbox_id, owner_instance_id, publish_generation),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def mark_published(
        self,
        outbox_id: int,
        owner_instance_id: str | None = None,
        publish_generation: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        owner = owner_instance_id or self.instance_id
        if publish_generation is None:
            return False
        published_at = self._utc(now).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
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
    ) -> bool:
        self._utc(now)
        safe_error = f"{type(error).__name__}: publication failed"
        async with aiosqlite.connect(self.db_path) as db:
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

    async def recover_expired_claims(self, *, now: datetime | None = None) -> int:
        current = self._utc(now).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE outbox_events
                SET publishing_owner=NULL,publishing_started_at=NULL,
                    publishing_lease_expires_at=NULL
                WHERE published_at IS NULL AND publishing_owner IS NOT NULL
                  AND (publishing_lease_expires_at IS NULL OR publishing_lease_expires_at<=?)
                """,
                (current,),
            )
            await db.commit()
        return cursor.rowcount

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
                      SUM(CASE WHEN published_at IS NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN published_at IS NULL AND publishing_owner IS NOT NULL
                               THEN 1 ELSE 0 END),
                      SUM(CASE WHEN published_at IS NULL AND attempts > 0 THEN 1 ELSE 0 END)
                    FROM outbox_events
                    """
                )
            ).fetchone()
        if row is None:
            raise RuntimeError("SQLite did not return outbox metrics")
        return {
            "outbox_pending": int(row[0] or 0),
            "outbox_publishing": int(row[1] or 0),
            "outbox_failed": int(row[2] or 0),
        }
