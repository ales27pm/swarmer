from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.services.message_board import MessageBoard


class OutboxService:
    """Persist board publications beside domain state, then deliver them at least once."""

    def __init__(self, db_path: Path, board: MessageBoard) -> None:
        self.db_path = db_path
        self.board = board

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
        created_at: str | None = None,
    ) -> int:
        if not all((aggregate_type, aggregate_id, topic, event_type, dedupe_key)):
            raise ValueError("outbox identifiers are required")
        try:
            payload_json = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("outbox payload must be canonical JSON") from exc
        if len(payload_json.encode("utf-8")) > 1_000_000:
            raise ValueError("outbox payload is too large")
        cursor = await db.execute(
            """
            INSERT INTO outbox_events(
                aggregate_type,aggregate_id,topic,event_type,payload_json,
                message_id,task_id,agent_id,created_at,dedupe_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (
                aggregate_type,
                aggregate_id,
                topic,
                event_type,
                payload_json,
                message_id or aggregate_id,
                task_id,
                agent_id,
                created_at or datetime.now(UTC).isoformat(),
                dedupe_key,
            ),
        )
        if cursor.rowcount == 1:
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return the outbox identifier")
            return int(cursor.lastrowid)
        row = await (
            await db.execute("SELECT id FROM outbox_events WHERE dedupe_key=?", (dedupe_key,))
        ).fetchone()
        if row is None:
            raise RuntimeError("deduplicated outbox event disappeared")
        return int(row[0])

    async def drain(self, *, limit: int = 100) -> dict[str, int]:
        bounded_limit = max(1, min(limit, 500))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = list(
                await (
                    await db.execute(
                        """
                        SELECT * FROM outbox_events
                        WHERE published_at IS NULL
                        ORDER BY id ASC LIMIT ?
                        """,
                        (bounded_limit,),
                    )
                ).fetchall()
            )

        published = 0
        failed = 0
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
                if not isinstance(payload, dict):
                    raise TypeError("outbox payload is not an object")
                await self.board.record(
                    str(row["event_type"]),
                    str(row["message_id"]),
                    topic=str(row["topic"]),
                    task_id=str(row["task_id"]) if row["task_id"] is not None else None,
                    agent_id=str(row["agent_id"]) if row["agent_id"] is not None else None,
                    payload=payload,
                    dedupe_key=str(row["dedupe_key"]),
                )
                await self.mark_published(int(row["id"]))
                published += 1
            # A swappable board adapter may expose its own exception hierarchy.
            # Cancellation remains a BaseException, while every publication error
            # must leave this durable row pending for a later drain.
            except Exception as exc:  # noqa: BLE001
                await self.mark_failed(int(row["id"]), exc)
                failed += 1
        return {
            "selected": len(rows),
            "published": published,
            "failed": failed,
            "pending": await self.pending_count(),
        }

    async def mark_published(self, outbox_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE outbox_events SET published_at=?,last_error=NULL
                WHERE id=? AND published_at IS NULL
                """,
                (datetime.now(UTC).isoformat(), outbox_id),
            )
            await db.commit()

    async def mark_failed(self, outbox_id: int, error: BaseException) -> None:
        safe_error = f"{type(error).__name__}: publication failed"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE outbox_events SET attempts=attempts+1,last_error=?
                WHERE id=? AND published_at IS NULL
                """,
                (safe_error, outbox_id),
            )
            await db.commit()

    async def pending_count(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute("SELECT COUNT(*) FROM outbox_events WHERE published_at IS NULL")
            ).fetchone()
        if row is None:
            raise RuntimeError("SQLite did not return the outbox count")
        return int(row[0])
