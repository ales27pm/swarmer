from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.services.event_privacy import safe_websocket_event

MAX_NOTIFICATION_BYTES = 1_000_000

NotificationHandler = Callable[[dict[str, Any], str | None], Awaitable[None]]


class WebSocketNotificationService:
    """SQLite-authoritative cross-process WebSocket invalidation log.

    Every control-plane instance owns an independent checkpoint. Delivery is
    intentionally at least once: a crash after a socket send and before the
    checkpoint commit may repeat a metadata-only invalidation. Clients refetch
    authoritative REST state, so replay cannot repeat an authoritative or
    sensitive action.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        instance_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not instance_id or len(instance_id) > 200:
            raise ValueError("WebSocket notification instance id is invalid")
        self.db_path = db_path
        self.instance_id = instance_id
        self._drain_lock = asyncio.Lock()
        self._initialized = False
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise RuntimeError("WebSocket notification clock must be timezone-aware")
        return value.astimezone(UTC)

    async def initialize(self) -> None:
        """Start this boot instance at the current high-water mark.

        Events predating an instance cannot target one of its not-yet-installed
        sockets. A newly connected mobile client reconciles through bootstrap.
        """

        now = self._now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            high_water = await (
                await db.execute("SELECT COALESCE(MAX(id), 0) FROM websocket_notifications")
            ).fetchone()
            if high_water is None:
                raise RuntimeError("WebSocket notification high-water mark is unavailable")
            await db.execute(
                """
                INSERT OR IGNORE INTO websocket_notification_checkpoints(
                    instance_id,last_notification_id,updated_at
                ) VALUES(?,?,?)
                """,
                (self.instance_id, int(high_water[0]), now),
            )
            await db.commit()
        self._initialized = True

    async def publish(
        self,
        event: dict[str, Any],
        *,
        device_id: str | None = None,
        event_id: str | None = None,
    ) -> int:
        safe_event = safe_websocket_event(event)
        encoded = json.dumps(
            safe_event,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > MAX_NOTIFICATION_BYTES:
            raise ValueError("WebSocket notification is too large")
        selected_id = event_id or f"wsn_{uuid4().hex}"
        created_at = self._now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO websocket_notifications(
                    event_id,device_id,event_type,event_json,created_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (selected_id, device_id, safe_event["type"], encoded, created_at),
            )
            if cursor.rowcount == 1:
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return the notification identifier")
                notification_id = int(cursor.lastrowid)
            else:
                row = await (
                    await db.execute(
                        """
                        SELECT id,device_id,event_json FROM websocket_notifications
                        WHERE event_id=?
                        """,
                        (selected_id,),
                    )
                ).fetchone()
                if row is None:
                    raise RuntimeError("deduplicated WebSocket notification disappeared")
                if row[1] != device_id or str(row[2]) != encoded:
                    raise ValueError("WebSocket notification id is bound to another event")
                notification_id = int(row[0])
            await db.commit()
        return notification_id

    async def drain(
        self,
        handler: NotificationHandler,
        *,
        limit: int = 100,
    ) -> int:
        bounded_limit = max(1, min(limit, 500))
        async with self._drain_lock:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                checkpoint = await (
                    await db.execute(
                        """
                        SELECT last_notification_id
                        FROM websocket_notification_checkpoints
                        WHERE instance_id=?
                        """,
                        (self.instance_id,),
                    )
                ).fetchone()
                if checkpoint is None:
                    if not self._initialized:
                        raise RuntimeError("WebSocket notification service is not initialized")
                    await self._recover_checkpoint(db)
                    rows: list[aiosqlite.Row] = []
                    checkpoint_recovered = True
                else:
                    checkpoint_recovered = False
                    rows = list(
                        await (
                            await db.execute(
                                """
                                SELECT id,device_id,event_json
                                FROM websocket_notifications
                                WHERE id>? ORDER BY id ASC LIMIT ?
                                """,
                                (int(checkpoint[0]), bounded_limit),
                            )
                        ).fetchall()
                    )

            if checkpoint_recovered:
                await handler(
                    {"type": "sync.invalidated", "payload": {"refetch_required": True}},
                    None,
                )
                return 0

            delivered = 0
            for row in rows:
                decoded = json.loads(str(row["event_json"]))
                if not isinstance(decoded, dict):
                    raise TypeError("stored WebSocket notification is invalid")
                safe_event = safe_websocket_event(decoded)
                device_id = str(row["device_id"]) if row["device_id"] is not None else None
                await handler(safe_event, device_id)
                await self._advance_checkpoint(int(row["id"]))
                delivered += 1
            return delivered

    async def _advance_checkpoint(self, notification_id: int) -> None:
        now = self._now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE websocket_notification_checkpoints
                SET last_notification_id=?,updated_at=?
                WHERE instance_id=? AND last_notification_id<?
                """,
                (notification_id, now, self.instance_id, notification_id),
            )
            await db.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("WebSocket notification checkpoint did not advance")

    async def cleanup(self, *, stale_instance_seconds: int) -> dict[str, int]:
        """Drop dead checkpoints and rows consumed by every live instance.

        Fresh, non-stopped control-plane instances always retain their unread
        rows. A checkpoint declared stale can be recovered later only through a
        metadata-only global invalidation and authoritative REST reconciliation.
        """

        if stale_instance_seconds < 1:
            raise ValueError("WebSocket notification staleness must be positive")
        if not self._initialized:
            raise RuntimeError("WebSocket notification service is not initialized")
        stale_before = (self._now() - timedelta(seconds=stale_instance_seconds)).isoformat()
        async with self._drain_lock, aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            removed_checkpoints = await db.execute(
                """
                DELETE FROM websocket_notification_checkpoints
                WHERE instance_id<>? AND NOT EXISTS (
                    SELECT 1 FROM control_plane_instances AS instance
                    WHERE instance.instance_id=websocket_notification_checkpoints.instance_id
                      AND instance.stopped_at IS NULL
                      AND instance.heartbeat_at>?
                )
                """,
                (self.instance_id, stale_before),
            )
            removed_notifications = 0
            live_without_checkpoint = await (
                await db.execute(
                    """
                    SELECT COUNT(*) FROM control_plane_instances AS instance
                    WHERE instance.stopped_at IS NULL AND instance.heartbeat_at>?
                      AND NOT EXISTS (
                        SELECT 1 FROM websocket_notification_checkpoints AS checkpoint
                        WHERE checkpoint.instance_id=instance.instance_id
                      )
                    """,
                    (stale_before,),
                )
            ).fetchone()
            own_checkpoint = await (
                await db.execute(
                    """
                    SELECT last_notification_id
                    FROM websocket_notification_checkpoints
                    WHERE instance_id=?
                    """,
                    (self.instance_id,),
                )
            ).fetchone()
            if (
                own_checkpoint is not None
                and live_without_checkpoint is not None
                and int(live_without_checkpoint[0]) == 0
            ):
                minimum_live_checkpoint = await (
                    await db.execute(
                        """
                        SELECT MIN(checkpoint.last_notification_id)
                        FROM websocket_notification_checkpoints AS checkpoint
                        LEFT JOIN control_plane_instances AS instance
                          ON instance.instance_id=checkpoint.instance_id
                        WHERE checkpoint.instance_id=? OR (
                            instance.stopped_at IS NULL AND instance.heartbeat_at>?
                        )
                        """,
                        (self.instance_id, stale_before),
                    )
                ).fetchone()
                if minimum_live_checkpoint is not None and minimum_live_checkpoint[0] is not None:
                    deleted = await db.execute(
                        "DELETE FROM websocket_notifications WHERE id<=?",
                        (int(minimum_live_checkpoint[0]),),
                    )
                    removed_notifications = max(0, int(deleted.rowcount))
            await db.commit()
        return {
            "removed_checkpoints": max(0, int(removed_checkpoints.rowcount)),
            "removed_notifications": removed_notifications,
        }

    async def close(self) -> None:
        """Remove this cleanly stopped boot instance's checkpoint."""

        async with self._drain_lock, aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM websocket_notification_checkpoints WHERE instance_id=?",
                (self.instance_id,),
            )
            await db.commit()
        self._initialized = False

    async def _recover_checkpoint(self, db: aiosqlite.Connection) -> None:
        high_water = await (
            await db.execute("SELECT COALESCE(MAX(id), 0) FROM websocket_notifications")
        ).fetchone()
        if high_water is None:
            raise RuntimeError("WebSocket notification high-water mark is unavailable")
        await db.execute(
            """
            INSERT INTO websocket_notification_checkpoints(
                instance_id,last_notification_id,updated_at
            ) VALUES(?,?,?)
            """,
            (self.instance_id, int(high_water[0]), self._now().isoformat()),
        )
        await db.commit()
