import json
from pathlib import Path

import aiosqlite

from app.models import TaskRecord


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    input TEXT NOT NULL,
    mode TEXT NOT NULL,
    source TEXT NOT NULL,
    conversation_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS pairing_codes (
    code TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class StateService:
    """The only service allowed to persist authoritative application state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def create_task(self, task: TaskRecord) -> TaskRecord:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO tasks (id,input,mode,source,conversation_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (task.id, task.input, task.mode, task.source, task.conversation_id, task.status.value, task.created_at.isoformat(), task.updated_at.isoformat()),
            )
            await db.execute("INSERT INTO audit_events (event_type, payload_json) VALUES (?, ?)", ("task.created", json.dumps({"task_id": task.id, "source": task.source})))
            await db.commit()
        return task

    async def list_tasks(self, limit: int = 100) -> list[TaskRecord]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))).fetchall()
        return [TaskRecord.model_validate(dict(row)) for row in rows]

    async def bootstrap(self) -> dict:
        tasks = [t.model_dump(mode="json") for t in await self.list_tasks(500)]
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            approvals = [dict(r) for r in await (await db.execute("SELECT * FROM approvals ORDER BY created_at DESC LIMIT 500")).fetchall()]
            cursor_row = await (await db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_events")).fetchone()
        return {"tasks": tasks, "approvals": approvals, "cursor": str(cursor_row[0])}
