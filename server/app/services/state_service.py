from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.models import (
    AgentCreate,
    FeedbackCreate,
    MemoryCreate,
    MemorySearch,
    MemoryUpdate,
    TaskCreate,
    TaskMode,
    TaskRecord,
)
from app.services.approval_binding import (
    PUBLIC_PROCESS_ERROR,
    ApprovalBindingError,
    canonical_action_digest,
    public_tool_call,
)
from app.services.audit_log import append_audit_event

SCHEMA_VERSION = 6
PUBLIC_ERROR_AUDIT_EVENTS = frozenset({"tool.failed", "tool.execution_rejected"})

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"planned", "failed"}),
    "planned": frozenset({"planned", "failed"}),
    "waiting_permission": frozenset(),
    "queued": frozenset(),
    "running": frozenset(),
    "blocked": frozenset(),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    input TEXT NOT NULL,
    mode TEXT NOT NULL,
    source TEXT NOT NULL,
    conversation_id TEXT,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    error_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    request_audit_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decision_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_id TEXT,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_task_id ON tool_calls(task_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_approval_id ON tool_calls(approval_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_calls_unique_approval
    ON tool_calls(approval_id) WHERE approval_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS pairing_codes (
    code TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    attempts_remaining INTEGER NOT NULL DEFAULT 10,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    last_pairing_id TEXT
);
CREATE TABLE IF NOT EXISTS pairing_candidates (
    pairing_id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    device_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    finalized_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pairing_candidates_device
    ON pairing_candidates(device_id);
CREATE TABLE IF NOT EXISTS websocket_tickets (
    ticket_hash TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    task_id TEXT,
    role TEXT NOT NULL,
    agent_id TEXT,
    content TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, created_at);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    model_id TEXT,
    status TEXT NOT NULL,
    skills_json TEXT NOT NULL,
    auth_token_hash TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    confidence REAL NOT NULL DEFAULT 1.0,
    pinned INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_scope
    ON memory_items(scope, kind, pinned, updated_at);
CREATE TABLE IF NOT EXISTS feedback_events (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    agent_id TEXT,
    type TEXT NOT NULL,
    label TEXT,
    score REAL,
    notes TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_task ON feedback_events(task_id, created_at);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    event_type TEXT NOT NULL,
    actor_type TEXT,
    actor_id TEXT,
    task_id TEXT,
    payload_json TEXT NOT NULL,
    prev_hash TEXT,
    hash TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class StateConflict(RuntimeError):
    pass


class StateService:
    """Authoritative domain state and additive migrations for the local database."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def initialize(self) -> None:
        parent_existed = self.db_path.parent.exists()
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not parent_existed:
            self.db_path.parent.chmod(0o700)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.db_path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError("state database must be a safe local file") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("state database must be a regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

        interrupted: list[tuple[str, str, str]] = []
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.execute("BEGIN IMMEDIATE")
            version_row = await (await db.execute("PRAGMA user_version")).fetchone()
            if version_row is None:
                raise RuntimeError("SQLite did not return a schema version")
            version = int(version_row[0])
            await self._migrate_legacy_schema(db)
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_request_audit
                ON approvals(request_audit_id) WHERE request_audit_id IS NOT NULL
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_events(trace_id, created_at)"
            )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_last_pairing
                ON devices(last_pairing_id) WHERE last_pairing_id IS NOT NULL
                """
            )
            if version < SCHEMA_VERSION:
                await db.execute("DELETE FROM pairing_codes")
                await db.execute("DELETE FROM pairing_candidates")
                await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            interrupted = [
                (str(row[0]), str(row[1]), "running")
                for row in await (
                    await db.execute("SELECT id,task_id FROM tool_calls WHERE status='running'")
                ).fetchall()
            ]
            if interrupted:
                now = datetime.now(UTC).isoformat()
                message = "control plane restarted during execution; outcome uncertain; not retried"
                await db.execute(
                    """
                    UPDATE tool_calls SET status='failed',error=?,updated_at=?
                    WHERE status='running'
                    """,
                    (message, now),
                )
                await db.execute(
                    """
                    UPDATE tasks SET status='failed',updated_at=?,completed_at=?,error_json=?
                    WHERE status='running'
                    """,
                    (now, now, json.dumps({"message": message})),
                )
            queued_before_claim = [
                (str(row[0]), str(row[1]), str(row[2]))
                for row in await (
                    await db.execute(
                        """
                        SELECT c.id,c.task_id,
                            CASE
                                WHEN a.status='approved' THEN 'approved_before_claim'
                                ELSE 'queued_before_claim'
                            END
                        FROM tool_calls AS c
                        JOIN tasks AS t ON t.id=c.task_id
                        LEFT JOIN approvals AS a ON a.id=c.approval_id
                        WHERE c.status='queued' AND t.status='queued'
                        """
                    )
                ).fetchall()
            ]
            if queued_before_claim:
                now = datetime.now(UTC).isoformat()
                for tool_call_id, task_id, stage in queued_before_claim:
                    if stage == "approved_before_claim":
                        message = (
                            "control plane restarted after approval but before execution claim; "
                            "execution not started; not retried"
                        )
                    else:
                        message = (
                            "control plane restarted after a tool call was queued but before "
                            "execution claim; execution not started; not retried"
                        )
                    await db.execute(
                        """
                        UPDATE tool_calls SET status='failed',error=?,updated_at=?
                        WHERE id=? AND task_id=? AND status='queued'
                        """,
                        (message, now, tool_call_id, task_id),
                    )
                    await db.execute(
                        """
                        UPDATE tasks SET status='failed',updated_at=?,completed_at=?,error_json=?
                        WHERE id=? AND status='queued'
                        """,
                        (now, now, json.dumps({"message": message}), task_id),
                    )
                interrupted.extend(queued_before_claim)
            for tool_call_id, task_id, stage in interrupted:
                running = stage == "running"
                await append_audit_event(
                    db,
                    "execution.interrupted" if running else "execution.not_started",
                    {
                        "tool_call_id": tool_call_id,
                        "stage": stage,
                        "outcome": "uncertain" if running else "not_started",
                        "retry": False,
                    },
                    task_id=task_id,
                    trace_id=task_id,
                )
            await db.commit()
        for suffix in ("", "-wal", "-shm"):
            database_file = Path(f"{self.db_path}{suffix}")
            if database_file.exists():
                database_file.chmod(0o600)

    async def _migrate_legacy_schema(self, db: aiosqlite.Connection) -> None:
        additions = {
            "tasks": {
                "title": "TEXT NOT NULL DEFAULT ''",
                "priority": "INTEGER NOT NULL DEFAULT 0",
                "completed_at": "TEXT",
                "error_json": "TEXT",
            },
            "approvals": {
                "tool_call_id": "TEXT",
                "action_digest": "TEXT",
                "request_audit_id": "INTEGER",
                "expires_at": "TEXT",
                "decision_json": "TEXT",
            },
            "pairing_codes": {
                "attempts_remaining": "INTEGER NOT NULL DEFAULT 10",
                "created_at": "TEXT",
            },
            "devices": {"last_seen_at": "TEXT", "last_pairing_id": "TEXT"},
            "agents": {"auth_token_hash": "TEXT"},  # nosec B105 - SQLite column type
            "audit_events": {
                "trace_id": "TEXT",
                "actor_type": "TEXT",
                "actor_id": "TEXT",
                "task_id": "TEXT",
                "prev_hash": "TEXT",
                "hash": "TEXT",
            },
        }
        for table, columns in additions.items():
            rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
            existing = {str(row[1]) for row in rows}
            for column, declaration in columns.items():
                if column not in existing:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        await db.execute(
            "UPDATE tasks SET title=substr(input, 1, 80) WHERE title='' OR title IS NULL"
        )
        await db.execute(
            "UPDATE approvals SET expires_at=created_at WHERE expires_at IS NULL OR expires_at=''"
        )
        approval_rows = await (
            await db.execute(
                """
                SELECT a.id,a.action,a.tool_call_id,a.action_digest,
                       c.id,c.tool_name,c.arguments_json
                FROM approvals AS a
                LEFT JOIN tool_calls AS c ON c.approval_id=a.id
                """
            )
        ).fetchall()
        for (
            approval_id,
            approval_action,
            stored_call_id,
            stored_digest,
            linked_call_id,
            linked_tool_name,
            arguments_json,
        ) in approval_rows:
            if stored_call_id and stored_digest:
                continue
            binding_call_id = str(stored_call_id or linked_call_id or f"unbound:{approval_id}")
            binding_tool_name = str(linked_tool_name or approval_action or "unbound")
            try:
                arguments = json.loads(str(arguments_json)) if arguments_json is not None else {}
                if not isinstance(arguments, dict):
                    raise ApprovalBindingError("legacy tool arguments are not an object")
                digest = canonical_action_digest(
                    tool_call_id=binding_call_id,
                    tool_name=binding_tool_name,
                    arguments=arguments,
                )
            except (ApprovalBindingError, json.JSONDecodeError):
                binding_call_id = f"unbound:{approval_id}"
                digest = canonical_action_digest(
                    tool_call_id=binding_call_id,
                    tool_name=str(approval_action or "unbound"),
                    arguments={},
                )
            await db.execute(
                """
                UPDATE approvals SET tool_call_id=?,action_digest=?
                WHERE id=? AND (tool_call_id IS NULL OR action_digest IS NULL)
                """,
                (binding_call_id, digest, approval_id),
            )
        legacy_pending = await (
            await db.execute(
                """
                SELECT id,task_id,tool_call_id FROM approvals
                WHERE status='pending' AND request_audit_id IS NULL
                """
            )
        ).fetchall()
        if legacy_pending:
            now = datetime.now(UTC).isoformat()
            for approval_id, task_id, tool_call_id in legacy_pending:
                await db.execute(
                    """
                    UPDATE approvals SET status='cancelled',decided_at=?
                    WHERE id=? AND status='pending' AND request_audit_id IS NULL
                    """,
                    (now, approval_id),
                )
                await db.execute(
                    """
                    UPDATE tool_calls SET status='cancelled',updated_at=?
                    WHERE approval_id=? AND status='waiting_permission'
                    """,
                    (now, approval_id),
                )
                await db.execute(
                    """
                    UPDATE tasks SET status='blocked',updated_at=?
                    WHERE id=? AND status='waiting_permission'
                    """,
                    (now, task_id),
                )
                await append_audit_event(
                    db,
                    "approval.invalidated.migration",
                    {
                        "approval_id": str(approval_id),
                        "tool_call_id": str(tool_call_id),
                        "reason": "legacy approval has no authenticated consent context",
                    },
                    task_id=str(task_id),
                    trace_id=str(task_id),
                    created_at=now,
                )
        device_rows = await (await db.execute("SELECT id,token FROM devices")).fetchall()
        for device_id, token in device_rows:
            stored = str(token)
            if not stored.startswith("sha256:"):
                digest = hashlib.sha256(stored.encode("utf-8")).hexdigest()
                await db.execute(
                    "UPDATE devices SET token=? WHERE id=?", (f"sha256:{digest}", device_id)
                )

    async def create_task(self, task: TaskRecord) -> TaskRecord:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._insert_task(db, task)
            await append_audit_event(
                db,
                "task.created",
                {"task_id": task.id, "source": task.source, "mode": task.mode.value},
                actor_type="device",
                actor_id=task.source,
                task_id=task.id,
                trace_id=task.id,
            )
            await db.commit()
        return task

    @staticmethod
    async def _insert_task(db: aiosqlite.Connection, task: TaskRecord) -> None:
        await db.execute(
            """
            INSERT INTO tasks(
                id,title,input,mode,source,conversation_id,status,priority,
                created_at,updated_at,completed_at,error_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task.id,
                task.title,
                task.input,
                task.mode.value,
                task.source,
                task.conversation_id,
                task.status.value,
                task.priority,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
                None,
                None,
            ),
        )

    async def get_task(self, task_id: str) -> TaskRecord | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))).fetchone()
        return self._task_from_row(row) if row else None

    async def list_tasks(self, limit: int = 100, status: str | None = None) -> list[TaskRecord]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if status:
                rows = await (
                    await db.execute(
                        "SELECT * FROM tasks WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                        (status, limit),
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
                    )
                ).fetchall()
        return [self._task_from_row(row) for row in rows]

    @staticmethod
    def _task_from_row(row: aiosqlite.Row) -> TaskRecord:
        value = dict(row)
        raw_error = value.get("error_json")
        value["error_json"] = json.loads(raw_error) if raw_error else None
        return TaskRecord.model_validate(value)

    async def update_task_status(
        self, task_id: str, status: str, *, error: str | None = None
    ) -> TaskRecord | None:
        now = datetime.now(UTC).isoformat()
        terminal = status in {"completed", "failed", "cancelled"}
        error_json = json.dumps({"message": error}) if error else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
            ).fetchone()
            if row is None:
                await db.rollback()
                return None
            current = str(row[0])
            if status not in TASK_TRANSITIONS.get(current, frozenset()):
                await db.rollback()
                raise StateConflict(f"task cannot transition from {current} to {status}")
            cursor = await db.execute(
                """
                UPDATE tasks SET status=?, updated_at=?, completed_at=?, error_json=?
                WHERE id=? AND status=?
                """,
                (status, now, now if terminal else None, error_json, task_id, current),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise StateConflict("task status changed during transition")
            await db.commit()
        return await self.get_task(task_id)

    async def cancel_task(self, task_id: str, *, actor_id: str) -> TaskRecord | None:
        now = datetime.now(UTC).isoformat()
        cancellable = ("created", "planned", "waiting_permission", "queued", "blocked")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
            ).fetchone()
            if not row:
                await db.rollback()
                return None
            if row[0] not in cancellable:
                await db.rollback()
                raise StateConflict(f"task cannot be cancelled from status {row[0]}")
            cursor = await db.execute(
                """
                UPDATE tasks SET status='cancelled', updated_at=?, completed_at=?
                WHERE id=? AND status IN (?,?,?,?,?)
                """,
                (now, now, task_id, *cancellable),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise StateConflict("task status changed while cancellation was requested")
            await db.execute(
                "UPDATE approvals SET status='cancelled', decided_at=? WHERE task_id=? AND status='pending'",
                (now, task_id),
            )
            await db.execute(
                """
                UPDATE tool_calls SET status='cancelled', updated_at=?
                WHERE task_id=? AND status IN ('proposed','waiting_permission','queued')
                """,
                (now, task_id),
            )
            await append_audit_event(
                db,
                "task.cancelled",
                {},
                actor_type="device",
                actor_id=actor_id,
                task_id=task_id,
                trace_id=task_id,
                created_at=now,
            )
            await db.commit()
        return await self.get_task(task_id)

    async def create_chat_task(
        self, content: str, conversation_id: str | None, mode: str, actor_id: str
    ) -> tuple[str, TaskRecord]:
        now = datetime.now(UTC).isoformat()
        conversation_id = conversation_id or f"cnv_{uuid4().hex}"
        request = TaskCreate(
            input=content,
            mode=TaskMode(mode),
            conversation_id=conversation_id,
        )
        task = TaskRecord.new(request, source=actor_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            conversation = await (
                await db.execute("SELECT id FROM conversations WHERE id=?", (conversation_id,))
            ).fetchone()
            if not conversation:
                await db.execute(
                    "INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                    (conversation_id, content.strip()[:80], now, now),
                )
            else:
                await db.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
                )
            await db.execute(
                """
                INSERT INTO messages(id,conversation_id,task_id,role,agent_id,content,metadata_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (f"msg_{uuid4().hex}", conversation_id, task.id, "user", None, content, None, now),
            )
            await self._insert_task(db, task)
            await append_audit_event(
                db,
                "task.created",
                {"source": "chat", "mode": mode},
                actor_type="device",
                actor_id=actor_id,
                task_id=task.id,
                trace_id=task.id,
                created_at=now,
            )
            await db.commit()
        return conversation_id, task

    async def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT c.*,
                      (SELECT content FROM messages m WHERE m.conversation_id=c.id
                       ORDER BY m.created_at DESC LIMIT 1) AS last_message
                    FROM conversations c ORDER BY c.updated_at DESC LIMIT ?
                    """,
                    (limit,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_messages(self, conversation_id: str, limit: int = 500) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC LIMIT ?",
                    (conversation_id, limit),
                )
            ).fetchall()
        return [self._decode_json_fields(dict(row), ("metadata_json",)) for row in rows]

    async def list_messages_for_task(self, task_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM messages WHERE task_id=? ORDER BY created_at ASC", (task_id,)
                )
            ).fetchall()
        return [self._decode_json_fields(dict(row), ("metadata_json",)) for row in rows]

    async def append_task_message(
        self,
        task_id: str,
        role: str,
        content: str,
        *,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        task = await self.get_task(task_id)
        if not task or not task.conversation_id:
            return None
        now = datetime.now(UTC).isoformat()
        record = {
            "id": f"msg_{uuid4().hex}",
            "conversation_id": task.conversation_id,
            "task_id": task_id,
            "role": role,
            "agent_id": agent_id,
            "content": content,
            "metadata": metadata,
            "created_at": now,
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO messages(id,conversation_id,task_id,role,agent_id,content,metadata_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    record["id"],
                    task.conversation_id,
                    task_id,
                    role,
                    agent_id,
                    content,
                    json.dumps(metadata) if metadata is not None else None,
                    now,
                ),
            )
            await db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (now, task.conversation_id)
            )
            await db.commit()
        return record

    async def list_memory(self, limit: int = 200) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM memory_items ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                    (limit,),
                )
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    async def create_memory(self, request: MemoryCreate, actor_id: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        memory_id = f"mem_{uuid4().hex}"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO memory_items(
                    id,scope,kind,content,summary,sensitivity,confidence,pinned,
                    metadata_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    memory_id,
                    request.scope,
                    request.kind,
                    request.content,
                    request.summary,
                    request.sensitivity,
                    request.confidence,
                    int(request.pinned),
                    None,
                    now,
                    now,
                ),
            )
            await append_audit_event(
                db,
                "memory.remembered",
                {"memory_id": memory_id, "scope": request.scope},
                actor_type="device",
                actor_id=actor_id,
                created_at=now,
            )
            await db.commit()
        record = await self.get_memory(memory_id)
        if record is None:
            raise RuntimeError("memory disappeared after creation")
        return record

    async def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM memory_items WHERE id=?", (memory_id,))
            ).fetchone()
        return self._memory_from_row(row) if row else None

    @staticmethod
    def _memory_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        value = StateService._decode_json_fields(dict(row), ("metadata_json",))
        value["pinned"] = bool(value["pinned"])
        return value

    async def search_memory(self, request: MemorySearch) -> list[dict[str, Any]]:
        terms = tuple({term.casefold() for term in request.query.split() if term.strip()})
        items = await self.list_memory(500)
        scored: list[dict[str, Any]] = []
        for item in items:
            if request.scope and item["scope"] != request.scope:
                continue
            if request.kind and item["kind"] != request.kind:
                continue
            haystack = f"{item['content']} {item.get('summary') or ''}".casefold()
            matches = sum(term in haystack for term in terms)
            if matches:
                scored.append({**item, "score": matches / len(terms), "search_kind": "lexical"})
        return sorted(scored, key=lambda item: (-item["score"], not item["pinned"]))[:50]

    async def update_memory(
        self, memory_id: str, request: MemoryUpdate, actor_id: str
    ) -> dict[str, Any] | None:
        existing = await self.get_memory(memory_id)
        if not existing:
            return None
        now = datetime.now(UTC).isoformat()
        content = request.content if request.content is not None else existing["content"]
        summary = request.summary if request.summary is not None else existing["summary"]
        pinned = request.pinned if request.pinned is not None else existing["pinned"]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE memory_items SET content=?, summary=?, pinned=?, updated_at=? WHERE id=?",
                (content, summary, int(pinned), now, memory_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await append_audit_event(
                db,
                "memory.updated",
                {"memory_id": memory_id},
                actor_type="device",
                actor_id=actor_id,
                created_at=now,
            )
            await db.commit()
        return await self.get_memory(memory_id)

    async def delete_memory(self, memory_id: str, actor_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("DELETE FROM memory_items WHERE id=?", (memory_id,))
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await append_audit_event(
                db,
                "memory.deleted",
                {"memory_id": memory_id},
                actor_type="device",
                actor_id=actor_id,
                created_at=now,
            )
            await db.commit()
        return True

    async def list_agents(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute("SELECT * FROM agents ORDER BY created_at ASC")
            ).fetchall()
        return [self._agent_from_row(row) for row in rows]

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM agents WHERE id=?", (agent_id,))
            ).fetchone()
        return self._agent_from_row(row) if row else None

    @staticmethod
    def _agent_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        value = dict(row)
        value["skills"] = json.loads(value.pop("skills_json"))
        value.pop("auth_token_hash", None)
        return value

    async def register_agent(self, request: AgentCreate, actor_id: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        agent_id = f"agt_{uuid4().hex}"
        credential = secrets.token_urlsafe(32)
        credential_hash = f"sha256:{hashlib.sha256(credential.encode('utf-8')).hexdigest()}"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO agents(
                    id,name,version,endpoint,model_id,status,skills_json,
                    auth_token_hash,last_heartbeat_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    agent_id,
                    request.name,
                    request.version,
                    str(request.endpoint),
                    request.model_id,
                    "unverified",
                    json.dumps(request.skills),
                    credential_hash,
                    None,
                    now,
                    now,
                ),
            )
            await append_audit_event(
                db,
                "agent.registered",
                {"agent_id": agent_id},
                actor_type="device",
                actor_id=actor_id,
                created_at=now,
            )
            await db.commit()
        record = await self.get_agent(agent_id)
        if record is None:
            raise RuntimeError("agent disappeared after registration")
        return {**record, "credential": credential}

    async def heartbeat_agent(
        self, agent_id: str, status: str, credential: str
    ) -> dict[str, Any] | None:
        if not credential:
            return None
        now = datetime.now(UTC).isoformat()
        credential_hash = f"sha256:{hashlib.sha256(credential.encode('utf-8')).hexdigest()}"
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE agents SET status=?, last_heartbeat_at=?, updated_at=?
                WHERE id=? AND auth_token_hash=?
                """,
                (status, now, now, agent_id, credential_hash),
            )
            await db.commit()
        return await self.get_agent(agent_id) if cursor.rowcount == 1 else None

    async def create_feedback(self, request: FeedbackCreate, actor_id: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        feedback_id = f"fbk_{uuid4().hex}"
        record = {
            "id": feedback_id,
            **request.model_dump(),
            "payload": {},
            "created_at": now,
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO feedback_events(
                    id,task_id,agent_id,type,label,score,notes,payload_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    feedback_id,
                    request.task_id,
                    request.agent_id,
                    request.type,
                    request.label,
                    request.score,
                    request.notes,
                    "{}",
                    now,
                ),
            )
            await append_audit_event(
                db,
                "feedback.created",
                {"feedback_id": feedback_id, "score": request.score},
                actor_type="device",
                actor_id=actor_id,
                task_id=request.task_id,
                trace_id=request.task_id,
                created_at=now,
            )
            await db.commit()
        return record

    async def append_audit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor_type: str = "system",
        actor_id: str = "control-plane",
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            event = await append_audit_event(
                db,
                event_type,
                payload,
                actor_type=actor_type,
                actor_id=actor_id,
                task_id=task_id,
                trace_id=trace_id,
            )
            await db.commit()
        return event

    async def list_audit(
        self, limit: int = 50, after_id: int | None = None
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if after_id is None:
                rows = await (
                    await db.execute(
                        "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        "SELECT * FROM audit_events WHERE id>? ORDER BY id ASC LIMIT ?",
                        (after_id, limit),
                    )
                ).fetchall()
            events = [self._audit_from_row(row) for row in rows]
            tool_call_ids = list(
                dict.fromkeys(
                    tool_call_id
                    for event in events
                    if event.get("event_type") in PUBLIC_ERROR_AUDIT_EVENTS
                    for payload in [event.get("payload")]
                    if isinstance(payload, dict)
                    for tool_call_id in [payload.get("tool_call_id")]
                    if isinstance(tool_call_id, str) and tool_call_id
                )
            )
            process_tool_call_ids: set[str] = set()
            if tool_call_ids:
                placeholders = ",".join("?" for _ in tool_call_ids)
                process_rows = await (
                    await db.execute(
                        f"""
                        SELECT id FROM tool_calls
                        WHERE tool_name='process.run' AND id IN ({placeholders})
                        """,  # nosec B608
                        tool_call_ids,
                    )
                ).fetchall()
                process_tool_call_ids = {str(row[0]) for row in process_rows}
        return [self._public_audit_event(event, process_tool_call_ids) for event in events]

    @staticmethod
    def _audit_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    @staticmethod
    def _public_audit_event(
        event: dict[str, Any], process_tool_call_ids: set[str]
    ) -> dict[str, Any]:
        projected = dict(event)
        raw_payload = projected.get("payload")
        if not isinstance(raw_payload, dict):
            return projected
        payload = dict(raw_payload)
        tool_call_id = payload.get("tool_call_id")
        if (
            projected.get("event_type") in PUBLIC_ERROR_AUDIT_EVENTS
            and isinstance(tool_call_id, str)
            and tool_call_id in process_tool_call_ids
            and "error" in payload
        ):
            payload["error"] = PUBLIC_PROCESS_ERROR
        projected["payload"] = payload
        return projected

    async def bootstrap(self) -> dict[str, Any]:
        # Bootstrap is itself an authoritative read. Materialize lazy approval
        # expiry first so its task, approval, tool-call, and count snapshots agree.
        from app.services.approval_gateway import ApprovalGateway

        approval_gateway = ApprovalGateway(self.db_path)
        approvals = await approval_gateway.list_all(500)
        tasks = [task.model_dump(mode="json") for task in await self.list_tasks(500)]
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            calls = [
                dict(row)
                for row in await (
                    await db.execute("SELECT * FROM tool_calls ORDER BY created_at DESC LIMIT 500")
                ).fetchall()
            ]
            for call in calls:
                call["arguments"] = json.loads(call.pop("arguments_json"))
                raw_result = call.pop("result_json")
                call["result"] = json.loads(raw_result) if raw_result else None
            calls = [public_tool_call(call) for call in calls]
            cursor_row = await (
                await db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_events")
            ).fetchone()
            if cursor_row is None:
                raise RuntimeError("SQLite did not return the audit cursor")
            counts: dict[str, int] = {}
            count_queries = {
                "tasks": "SELECT COUNT(*) FROM tasks",
                "messages": "SELECT COUNT(*) FROM messages",
                "agents": "SELECT COUNT(*) FROM agents",
                "memory_items": "SELECT COUNT(*) FROM memory_items",
                "audit_events": "SELECT COUNT(*) FROM audit_events",
                "approvals_pending": "SELECT COUNT(*) FROM approvals WHERE status='pending'",
            }
            for name, query in count_queries.items():
                count_row = await (await db.execute(query)).fetchone()
                if count_row is None:
                    raise RuntimeError(f"SQLite did not return the {name} count")
                counts[name] = int(count_row[0])
        return {
            "server_time": datetime.now(UTC).isoformat(),
            "tasks": tasks,
            "approvals": approvals,
            "tool_calls": calls,
            "conversations": await self.list_conversations(50),
            "agents": await self.list_agents(),
            "pinned_memory": [item for item in await self.list_memory(200) if item["pinned"]],
            "counts": counts,
            "cursor": str(cursor_row[0]),
        }

    @staticmethod
    def _decode_json_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        for field in fields:
            raw = value.pop(field, None)
            value[field.removesuffix("_json")] = json.loads(raw) if raw else None
        return value
