"""Versioned project state grounded in original sources and accepted receipts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.services.context_builder import safe_context_text


class ProjectContextConflict(RuntimeError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class ProjectContextService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def _sources_locked(
        self, db: aiosqlite.Connection, goal_id: str
    ) -> tuple[Any, Any, Any, str]:
        goal = await (
            await db.execute(
                """SELECT g.*, l.project_id FROM goal_runs g
            JOIN goal_project_links l ON l.goal_run_id=g.id WHERE g.id=?""",
                (goal_id,),
            )
        ).fetchone()
        if goal is None:
            raise ProjectContextConflict("project not found")
        messages = await (
            await db.execute(
                """SELECT DISTINCT m.rowid AS seq,m.id,
            m.role,m.content,m.created_at FROM goal_messages m
            JOIN goal_conversation_links c ON c.conversation_id=m.conversation_id
            JOIN goal_project_links p ON p.goal_run_id=c.goal_run_id
            WHERE p.project_id=? ORDER BY seq LIMIT 10001""",
                (goal["project_id"],),
            )
        ).fetchall()
        messages = list(messages)
        if len(messages) > 10000:
            raise ProjectContextConflict("project source limit reached; archive required")
        revision = await (
            await db.execute(
                """SELECT * FROM project_revisions
            WHERE project_id=? ORDER BY revision DESC LIMIT 1""",
                (goal["project_id"],),
            )
        ).fetchone()
        fingerprint = digest(
            {
                "goal_id": goal_id,
                "objective": goal["objective"],
                "conversation_revision": goal["conversation_revision"],
                "sources": [(m["id"], m["role"], digest(m["content"])) for m in messages],
                "revision": [revision["id"], revision["sha256"]] if revision else None,
            }
        )
        return goal, messages, revision, fingerprint

    async def require_fingerprint_locked(
        self, db: aiosqlite.Connection, goal_id: str, fingerprint: str
    ) -> None:
        # Caller holds BEGIN IMMEDIATE, so a revision/message cannot race acceptance.
        if (await self._sources_locked(db, goal_id))[3] != fingerprint:
            raise ProjectContextConflict("project context changed")

    async def refresh(
        self, goal_id: str, *, expected_fingerprint: str | None = None
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            goal, messages, revision, fingerprint = await self._sources_locked(db, goal_id)
            project_id = goal["project_id"]
            if expected_fingerprint is not None and fingerprint != expected_fingerprint:
                raise ProjectContextConflict("project context changed")
            existing = await (
                await db.execute(
                    """SELECT state_json FROM project_context_snapshots
                WHERE project_id=? AND fingerprint=?""",
                    (project_id, fingerprint),
                )
            ).fetchone()
            if existing:
                return json.loads(existing[0])  # type: ignore[no-any-return]
            requirements: list[dict[str, Any]] = []
            by_text: dict[str, dict[str, Any]] = {}
            proposals: list[dict[str, str]] = []
            for message in messages:
                text = safe_context_text(
                    message["content"], max_chars=max(4000, len(message["content"]))
                )
                if message["role"] == "user":
                    key = digest(text)
                    if key in by_text:
                        by_text[key]["source_ids"].append(message["id"])
                    else:
                        item = {
                            "text": text,
                            "source_id": message["id"],
                            "source_ids": [message["id"]],
                            "created_at": message["created_at"],
                        }
                        by_text[key] = item
                        requirements.append(item)
                elif message["role"] == "assistant":
                    proposals.append({"text": text, "source_id": message["id"]})
            snapshot = json.loads(revision["snapshot_json"]) if revision else {}
            version_row = await (
                await db.execute(
                    """SELECT COALESCE(MAX(version),0)+1
                FROM project_context_snapshots WHERE project_id=?""",
                    (project_id,),
                )
            ).fetchone()
            if version_row is None:
                raise ProjectContextConflict("project context version unavailable")
            state = {
                "schema_version": "1.0",
                "project_id": project_id,
                "goal_id": goal_id,
                "version": int(version_row[0]),
                "fingerprint": fingerprint,
                "conversation_revision": goal["conversation_revision"],
                "base_revision_id": revision["id"] if revision else None,
                "objective": safe_context_text(goal["objective"], max_chars=4000),
                "requirements": requirements,
                "proposals": proposals[-8:],
                "accepted_changes": {"revision_id": revision["id"], "sha256": revision["sha256"]}
                if revision
                else None,
                "verified_results": [
                    {"source_id": revision["id"], **check} for check in snapshot.get("checks", [])
                ]
                if revision
                else [],
                "pending_work": snapshot.get("plan", []),
                "source_count": len(messages),
                "method": "source_backed_extract",
                "created_at": datetime.now(UTC).isoformat(),
            }
            await db.execute(
                """INSERT INTO project_context_snapshots
                (project_id,version,goal_run_id,fingerprint,state_json,created_at)
                VALUES(?,?,?,?,?,?)""",
                (
                    project_id,
                    state["version"],
                    goal_id,
                    fingerprint,
                    json.dumps(state, ensure_ascii=False),
                    state["created_at"],
                ),
            )
            await db.commit()
            return state

    async def source(self, goal_id: str, source_id: str) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """SELECT DISTINCT m.id,m.role,m.content,m.created_at
                FROM goal_messages m JOIN goal_conversation_links c ON c.conversation_id=m.conversation_id
                JOIN goal_project_links p ON p.goal_run_id=c.goal_run_id
                JOIN goal_project_links target ON target.project_id=p.project_id
                WHERE target.goal_run_id=? AND m.id=?""",
                    (goal_id, source_id),
                )
            ).fetchone()
            if row is None:
                raise ProjectContextConflict("source not found in project")
            return {
                **dict(row),
                "content": safe_context_text(
                    row["content"], max_chars=max(4000, len(row["content"]))
                ),
            }

    @staticmethod
    def prompt_state(state: dict[str, Any]) -> dict[str, Any]:
        # Chronological quotations are requirements, never grants of tool authority.
        # Never silently remove this block when a generation exceeds its budget.
        return {
            "version": state["version"],
            "fingerprint": state["fingerprint"],
            "requirements": [
                {"text": item["text"], "source_id": item["source_id"]}
                for item in state["requirements"]
            ],
            "base_revision_id": state["base_revision_id"],
        }
