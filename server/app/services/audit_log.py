from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite


def audit_event_hash(
    *,
    prev_hash: str | None,
    trace_id: str,
    event_type: str,
    actor_type: str,
    actor_id: str,
    task_id: str | None,
    payload_json: str,
    created_at: str,
) -> str:
    material = "\x1f".join(
        (
            prev_hash or "",
            trace_id,
            event_type,
            actor_type,
            actor_id,
            task_id or "",
            payload_json,
            created_at,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def append_audit_event(
    db: aiosqlite.Connection,
    event_type: str,
    payload: dict[str, Any],
    *,
    actor_type: str = "system",
    actor_id: str = "control-plane",
    task_id: str | None = None,
    trace_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Append one hash-chained event inside the caller's active transaction.

    The caller owns transaction boundaries. Keeping this primitive free of
    implicit commits lets approval creation persist its consent audit record,
    approval, tool call, and task transition atomically.
    """

    now = created_at or datetime.now(UTC).isoformat()
    resolved_trace_id = trace_id or f"trc_{uuid4().hex}"
    payload_json = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    previous = await (
        await db.execute(
            "SELECT hash FROM audit_events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
    ).fetchone()
    prev_hash = str(previous[0]) if previous else None
    event_hash = audit_event_hash(
        prev_hash=prev_hash,
        trace_id=resolved_trace_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        task_id=task_id,
        payload_json=payload_json,
        created_at=now,
    )
    cursor = await db.execute(
        """
        INSERT INTO audit_events(
            trace_id,event_type,actor_type,actor_id,task_id,payload_json,
            prev_hash,hash,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            resolved_trace_id,
            event_type,
            actor_type,
            actor_id,
            task_id,
            payload_json,
            prev_hash,
            event_hash,
            now,
        ),
    )
    event_id = cursor.lastrowid
    if event_id is None:
        raise RuntimeError("SQLite did not return the audit event identifier")
    return {
        "id": event_id,
        "trace_id": resolved_trace_id,
        "event_type": event_type,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "task_id": task_id,
        "payload": payload,
        "prev_hash": prev_hash,
        "hash": event_hash,
        "created_at": now,
    }
