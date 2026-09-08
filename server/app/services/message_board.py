from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import aiosqlite


class MessageBoard(Protocol):
    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def record(
        self,
        event_type: str,
        message_id: str,
        *,
        topic: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def list_events(
        self, *, topic: str | None = None, after_id: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    async def claim(self, message_id: str, *, topic: str, agent_id: str) -> dict[str, Any]: ...

    async def ack(self, message_id: str, *, topic: str, agent_id: str) -> dict[str, Any]: ...

    async def fail(
        self, message_id: str, *, topic: str, agent_id: str, reason: str
    ) -> dict[str, Any]: ...

    async def heartbeat(self, message_id: str, *, topic: str, agent_id: str) -> dict[str, Any]: ...


class MessageBoardService:
    """SQLite-backed durable event board implementing the swappable board interface."""

    EVENT_TYPES = frozenset({"published", "claimed", "acked", "failed", "heartbeat"})

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.record(
            "published",
            message_id or f"mb_{uuid4().hex}",
            topic=topic,
            task_id=task_id,
            agent_id=agent_id,
            payload=payload,
        )

    async def record(
        self,
        event_type: str,
        message_id: str,
        *,
        topic: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if event_type not in self.EVENT_TYPES:
            raise ValueError("unsupported message board event")
        if not topic or len(topic) > 200 or not message_id or len(message_id) > 200:
            raise ValueError("invalid message board identifier")
        created_at = datetime.now(UTC).isoformat()
        body = payload or {}
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO message_board_events(
                    topic,event_type,message_id,agent_id,task_id,payload_json,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (topic, event_type, message_id, agent_id, task_id, encoded, created_at),
            )
            await db.commit()
            event_id = cursor.lastrowid
        return {
            "id": event_id,
            "topic": topic,
            "event_type": event_type,
            "message_id": message_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "payload": body,
            "created_at": created_at,
        }

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
