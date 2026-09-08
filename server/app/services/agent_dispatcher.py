from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.services.audit_log import append_audit_event
from app.services.message_board import MessageBoard

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})


class AgentDispatchConflict(RuntimeError):
    pass


class AgentDispatcher:
    def __init__(self, db_path: Path, board: MessageBoard) -> None:
        self.db_path = db_path
        self.board = board

    @staticmethod
    def _credential_hash(credential: str) -> str:
        return f"sha256:{hashlib.sha256(credential.encode('utf-8')).hexdigest()}"

    async def authenticate(self, agent_id: str, credential: str) -> dict[str, Any] | None:
        if not credential:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT id,skills_json,status FROM agents WHERE id=? AND auth_token_hash=?",
                    (agent_id, self._credential_hash(credential)),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "skills": json.loads(row["skills_json"]),
            "status": row["status"],
        }

    async def queue_job(
        self, task_id: str, required_skill: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if len(json.dumps(payload).encode("utf-8")) > 1_000_000:
            raise AgentDispatchConflict("job payload is too large")
        now = datetime.now(UTC).isoformat()
        job_id = f"job_{uuid4().hex}"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            task = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
            ).fetchone()
            if task is None:
                await db.rollback()
                raise AgentDispatchConflict("task not found")
            if str(task["status"]) in TERMINAL_TASK_STATUSES:
                await db.rollback()
                raise AgentDispatchConflict("terminal task cannot receive a job")
            await db.execute(
                """INSERT INTO agent_jobs(
                    id,task_id,required_skill,payload_json,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (job_id, task_id, required_skill, json.dumps(payload), "queued", now, now),
            )
            await db.execute(
                "UPDATE tasks SET status='queued',updated_at=? WHERE id=? AND status IN ('created','planned')",
                (now, task_id),
            )
            await append_audit_event(
                db,
                "agent.job.queued",
                {"job_id": job_id, "required_skill": required_skill},
                actor_type="control-plane",
                actor_id="dispatcher",
                task_id=task_id,
                trace_id=task_id,
                created_at=now,
            )
            await db.commit()
        await self.board.publish(
            "tasks.inbox",
            {"job_id": job_id, "required_skill": required_skill},
            task_id=task_id,
            message_id=job_id,
        )
        record = await self.get_job(job_id)
        if record is None:
            raise RuntimeError("queued job disappeared")
        return record

    async def claim(self, agent_id: str) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        claim_token = secrets.token_urlsafe(24)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            agent = await (
                await db.execute("SELECT skills_json FROM agents WHERE id=?", (agent_id,))
            ).fetchone()
            if agent is None:
                await db.rollback()
                raise AgentDispatchConflict("agent not found")
            skills = json.loads(str(agent["skills_json"]))
            if not skills:
                await db.rollback()
                return None
            placeholders = ",".join("?" for _ in skills)
            row = await (
                await db.execute(
                    f"""SELECT j.* FROM agent_jobs j JOIN tasks t ON t.id=j.task_id
                    WHERE j.status='queued' AND j.required_skill IN ({placeholders})
                      AND t.status NOT IN ('completed','failed','cancelled')
                    ORDER BY t.priority DESC,j.created_at ASC LIMIT 1""",  # nosec B608
                    skills,
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                return None
            cursor = await db.execute(
                """UPDATE agent_jobs SET status='claimed',claimed_by=?,claim_token=?,
                claimed_at=?,heartbeat_at=?,updated_at=? WHERE id=? AND status='queued'""",
                (agent_id, claim_token, now, now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise AgentDispatchConflict("job changed during claim")
            await db.execute(
                "UPDATE tasks SET status='running',updated_at=? WHERE id=? AND status='queued'",
                (now, row["task_id"]),
            )
            await append_audit_event(
                db,
                "agent.job.claimed",
                {"job_id": row["id"], "agent_id": agent_id},
                actor_type="agent",
                actor_id=agent_id,
                task_id=row["task_id"],
                trace_id=row["task_id"],
                created_at=now,
            )
            await db.commit()
        await self.board.claim(str(row["id"]), topic="tasks.inbox", agent_id=agent_id)
        record = await self.get_job(str(row["id"]), include_claim_token=True)
        return record

    async def heartbeat(self, agent_id: str, job_id: str, claim_token: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """UPDATE agent_jobs SET status='running',heartbeat_at=?,updated_at=?
                WHERE id=? AND claimed_by=? AND claim_token=? AND status IN ('claimed','running')""",
                (now, now, job_id, agent_id, claim_token),
            )
            await db.commit()
        if cursor.rowcount != 1:
            raise AgentDispatchConflict("job is not active for this agent")
        await self.board.heartbeat(job_id, topic="agents.heartbeat", agent_id=agent_id)
        record = await self.get_job(job_id)
        if record is None:
            raise RuntimeError("active job disappeared")
        return record

    async def submit_result(
        self,
        agent_id: str,
        job_id: str,
        claim_token: str,
        *,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> tuple[dict[str, Any], bool]:
        if status not in {"completed", "failed"}:
            raise AgentDispatchConflict("result status must be completed or failed")
        if result is not None and len(json.dumps(result).encode("utf-8")) > 1_000_000:
            raise AgentDispatchConflict("job result is too large")
        now = datetime.now(UTC).isoformat()
        public_error = "remote worker reported failure" if status == "failed" else None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,))
            ).fetchone()
            if row is None or row["claimed_by"] != agent_id or row["claim_token"] != claim_token:
                await db.rollback()
                raise AgentDispatchConflict("job is not owned by this agent")
            if row["status"] in TERMINAL_JOB_STATUSES:
                same = (
                    row["status"] == status
                    and (json.loads(row["result_json"]) if row["result_json"] else None) == result
                )
                await db.rollback()
                if not same:
                    raise AgentDispatchConflict("terminal job result differs from recorded result")
                record = self._job_from_row(row)
                return record, False
            if row["status"] not in {"claimed", "running"}:
                await db.rollback()
                raise AgentDispatchConflict("job is not active")
            task = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (row["task_id"],))
            ).fetchone()
            if task is None or task["status"] in TERMINAL_TASK_STATUSES:
                await db.rollback()
                raise AgentDispatchConflict("terminal task cannot be resurrected")
            await db.execute(
                """UPDATE agent_jobs SET status=?,result_json=?,error=?,updated_at=?,completed_at=?
                WHERE id=? AND status IN ('claimed','running')""",
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    public_error,
                    now,
                    now,
                    job_id,
                ),
            )
            task_status = "completed" if status == "completed" else "failed"
            await db.execute(
                """UPDATE tasks SET status=?,updated_at=?,completed_at=?,error_json=?
                WHERE id=? AND status NOT IN ('completed','failed','cancelled')""",
                (
                    task_status,
                    now,
                    now,
                    json.dumps({"message": public_error}) if public_error else None,
                    row["task_id"],
                ),
            )
            await append_audit_event(
                db,
                f"agent.job.{status}",
                {"job_id": job_id, "agent_id": agent_id},
                actor_type="agent",
                actor_id=agent_id,
                task_id=row["task_id"],
                trace_id=row["task_id"],
                created_at=now,
            )
            await db.commit()
        if status == "completed":
            await self.board.ack(job_id, topic="tasks.status", agent_id=agent_id)
        else:
            await self.board.fail(
                job_id,
                topic="tasks.status",
                agent_id=agent_id,
                reason="worker reported failure",
            )
        completed_record = await self.get_job(job_id)
        if completed_record is None:
            raise RuntimeError("completed job disappeared")
        return completed_record, True

    async def list_jobs(self, agent_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM agent_jobs WHERE claimed_by=? ORDER BY updated_at DESC LIMIT ?",
                    (agent_id, max(1, min(limit, 500))),
                )
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    async def get_job(
        self, job_id: str, *, include_claim_token: bool = False
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,))
            ).fetchone()
        if row is None:
            return None
        value = self._job_from_row(row)
        if include_claim_token:
            value["claim_token"] = row["claim_token"]
        return value

    @staticmethod
    def _job_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        raw_result = value.pop("result_json")
        value["result"] = json.loads(raw_result) if raw_result else None
        value.pop("claim_token", None)
        return value
