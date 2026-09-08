from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.models import FeedbackCorrection
from app.services.audit_log import append_audit_event

SECRET_PATTERN = re.compile(r"(?i)(bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+|token\s*[:=]\s*\S+)")
PATH_PATTERN = re.compile(r"/(?:Users|home|root|private|etc)/[^\s\"']+")


def redact_dataset_text(value: str | None) -> str | None:
    if value is None:
        return None
    return PATH_PATTERN.sub("<protected-path>", SECRET_PATTERN.sub("<redacted-secret>", value))


class FeedbackDatasetService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def add_correction(self, request: FeedbackCorrection, *, actor_id: str) -> dict[str, Any]:
        correction_id = f"cor_{uuid4().hex}"
        example_id = f"eval_{uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        behavior = redact_dataset_text(request.corrected_behavior) or ""
        notes = redact_dataset_text(request.notes)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            task = await (
                await db.execute("SELECT id FROM tasks WHERE id=?", (request.task_id,))
            ).fetchone()
            if task is None:
                await db.rollback()
                raise ValueError("task not found")
            await db.execute(
                "INSERT INTO corrections(id,task_id,corrected_behavior,notes,created_at) VALUES(?,?,?,?,?)",
                (correction_id, request.task_id, behavior, notes, now),
            )
            await db.execute(
                "INSERT INTO eval_examples(id,task_id,payload_json,created_at) VALUES(?,?,?,?)",
                (
                    example_id,
                    request.task_id,
                    json.dumps({"correction_id": correction_id, "reviewed": True}),
                    now,
                ),
            )
            await append_audit_event(
                db,
                "feedback.corrected",
                {"correction_id": correction_id},
                actor_type="device",
                actor_id=actor_id,
                task_id=request.task_id,
                trace_id=request.task_id,
                created_at=now,
            )
            await db.commit()
        return {
            "id": correction_id,
            "task_id": request.task_id,
            "corrected_behavior": behavior,
            "notes": notes,
            "created_at": now,
        }

    async def export_jsonl(self) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """SELECT t.id,t.input,t.status,t.error_json,c.corrected_behavior,c.notes,
                    (SELECT payload_json FROM audit_events WHERE task_id=t.id AND event_type='orchestrator.proposed' ORDER BY id DESC LIMIT 1) proposal,
                    (SELECT json_object('type',type,'label',label,'score',score,'notes',notes)
                     FROM feedback_events WHERE task_id=t.id ORDER BY created_at DESC LIMIT 1) feedback
                    FROM tasks t JOIN corrections c ON c.task_id=t.id ORDER BY c.created_at"""
                )
            ).fetchall()
        lines: list[str] = []
        for row in rows:
            feedback = json.loads(row["feedback"]) if row["feedback"] else {}
            proposal = json.loads(row["proposal"]) if row["proposal"] else None
            lines.append(
                json.dumps(
                    {
                        "task_id": row["id"],
                        "task_input": redact_dataset_text(row["input"]),
                        "planner_proposal": proposal,
                        "tool_outcome": {
                            "status": row["status"],
                            "error": redact_dataset_text(row["error_json"]),
                        },
                        "user_feedback": feedback,
                        "corrected_behavior": row["corrected_behavior"],
                        "correction_notes": row["notes"],
                    },
                    sort_keys=True,
                )
            )
        return "\n".join(lines) + ("\n" if lines else "")
