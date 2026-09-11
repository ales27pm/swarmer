from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.models import TaskCreate, TaskMode, TaskRecord
from app.services.approval_gateway import ApprovalGateway
from app.services.audit_log import append_audit_event
from app.services.code_proposal import validate_code_proposal_result
from app.services.execution_engine import (
    AuthenticatedRequester,
    ExecutionConflict,
    ExecutionEngine,
)
from app.services.maintenance_lease import MaintenanceLeaseGuard
from app.services.state_service import StateService

CODE_PROPOSAL_SKILL = "code.generate_python"
_TERMINAL_GOALS = frozenset({"completed", "failed", "cancelled", "budget_exhausted"})
_PROPOSAL_SUMMARY = "Python file proposal is ready for review. No file has been written."


class GoalCodeApplicationConflict(RuntimeError):
    """A proposal no longer matches its authoritative goal or reviewed artifact."""


class GoalCodeApplicationService:
    """Bridge inert worker proposals to device-requested, one-shot local writes."""

    def __init__(self, db_path: Path, execution_engine: ExecutionEngine) -> None:
        self.db_path = db_path
        self.execution_engine = execution_engine

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    async def capture_result(
        self,
        goal_run_id: str,
        node_id: str,
        job_id: str,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> bool:
        """Read the durable job, then store its proposal and wait for user review."""

        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            row = await (
                await db.execute(
                    """SELECT n.status AS node_status,g.status AS goal_status,
                    g.root_task_id,j.status AS job_status,j.result_json
                    FROM plan_nodes AS n JOIN goal_runs AS g ON g.id=n.goal_run_id
                    JOIN agent_jobs AS j ON j.id=n.worker_job_id AND j.task_id=n.task_id
                    WHERE n.id=? AND n.goal_run_id=? AND j.id=?
                      AND n.required_skill=? AND j.required_skill=?""",
                    (node_id, goal_run_id, job_id, CODE_PROPOSAL_SKILL, CODE_PROPOSAL_SKILL),
                )
            ).fetchone()
            if row is None:
                raise GoalCodeApplicationConflict("code proposal job does not match its node")
            if str(row["goal_status"]) in _TERMINAL_GOALS:
                await db.rollback()
                return False
            existing = await (
                await db.execute("SELECT 1 FROM goal_code_proposals WHERE node_id=?", (node_id,))
            ).fetchone()
            if existing is not None:
                await db.rollback()
                return False
            if row["job_status"] != "completed" or row["node_status"] not in {
                "dispatched",
                "running",
            }:
                raise GoalCodeApplicationConflict("code proposal job is not ready for review")
            try:
                proposal = validate_code_proposal_result(json.loads(str(row["result_json"])))
            except (ValueError, TypeError) as exc:
                raise GoalCodeApplicationConflict(
                    "worker returned an invalid code proposal"
                ) from exc
            content = str(proposal["content"])
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            target = f"generated/{goal_run_id}/{node_id}/app.py"
            self.execution_engine.validate_arguments(
                "workspace.write_text", {"path": target, "content": content}
            )
            await db.execute(
                """INSERT INTO goal_code_proposals(
                    node_id,goal_run_id,worker_job_id,path,content,sha256,summary,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    node_id,
                    goal_run_id,
                    job_id,
                    target,
                    content,
                    digest,
                    proposal["summary"],
                    now,
                    now,
                ),
            )
            await db.execute(
                """UPDATE plan_nodes SET status='waiting_permission',result_summary=?,
                    updated_at=? WHERE id=?""",
                (_PROPOSAL_SUMMARY, now, node_id),
            )
            await db.execute(
                """UPDATE goal_runs SET status='waiting_permission',
                    current_phase='code_proposal_ready',failure_reason=NULL,updated_at=? WHERE id=?""",
                (now, goal_run_id),
            )
            await db.execute(
                "UPDATE tasks SET status='waiting_permission',updated_at=? WHERE id=?",
                (now, row["root_task_id"]),
            )
            await append_audit_event(
                db,
                "goal.code_proposal.ready",
                {
                    "goal_run_id": goal_run_id,
                    "node_id": node_id,
                    "worker_job_id": job_id,
                    "sha256": digest,
                    "bytes": len(content.encode("utf-8")),
                },
                actor_type="control-plane",
                actor_id="goal-manager",
                task_id=str(row["root_task_id"]),
                trace_id=goal_run_id,
                created_at=now,
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return True

    async def get_proposal(self, goal_run_id: str, node_id: str) -> dict[str, Any] | None:
        """Private, authenticated review payload; never use in sync or events."""

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """SELECT p.*,n.status AS node_status,g.status AS goal_status,
                        c.status AS tool_status,t.status AS application_task_status
                    FROM goal_code_proposals AS p
                    JOIN plan_nodes AS n ON n.id=p.node_id
                    JOIN goal_runs AS g ON g.id=p.goal_run_id
                    LEFT JOIN tasks AS t ON t.id=p.apply_task_id
                    LEFT JOIN tool_calls AS c ON c.task_id=p.apply_task_id
                    WHERE p.goal_run_id=? AND p.node_id=?""",
                    (goal_run_id, node_id),
                )
            ).fetchone()
        if row is None:
            return None
        tool_status = row["tool_status"]
        if tool_status == "completed":
            status = "applied"
        elif tool_status in {"failed", "denied", "cancelled"} or (
            row["goal_status"] in _TERMINAL_GOALS
            or row["node_status"] in {"failed", "cancelled"}
            or row["application_task_status"] in {"failed", "blocked", "cancelled"}
        ):
            status = "failed"
        elif tool_status is None and row["application_task_status"] in {"created", "planned"}:
            # The child link can survive a crash before its approval-bound call.
            # Keep that link private until a call exists; explicit apply resumes it.
            status = "proposal"
        elif row["apply_task_id"] is not None:
            status = "waiting_permission"
        else:
            status = "proposal"
        return {
            "node_id": node_id,
            "path": row["path"],
            "content": row["content"],
            "sha256": row["sha256"],
            "summary": row["summary"],
            "status": status,
            "task_id": None if status == "proposal" else row["apply_task_id"],
        }

    async def apply(
        self,
        goal_run_id: str,
        node_id: str,
        requester: AuthenticatedRequester,
        reviewed_sha256: str,
    ) -> dict[str, Any]:
        """Create or retrieve the one approval-bound write for a reviewed proposal."""

        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    """SELECT p.*,g.status AS goal_status,g.started_at,g.max_runtime_seconds,
                        n.status AS node_status FROM goal_code_proposals AS p
                    JOIN goal_runs AS g ON g.id=p.goal_run_id
                    JOIN plan_nodes AS n ON n.id=p.node_id
                    WHERE p.goal_run_id=? AND p.node_id=?""",
                    (goal_run_id, node_id),
                )
            ).fetchone()
            if row is None:
                raise GoalCodeApplicationConflict("code proposal not found")
            if (
                not isinstance(reviewed_sha256, str)
                or len(reviewed_sha256) != 64
                or any(value not in "0123456789abcdef" for value in reviewed_sha256)
                or not hmac.compare_digest(str(row["sha256"]), reviewed_sha256)
                or hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest() != row["sha256"]
            ):
                raise GoalCodeApplicationConflict("reviewed code proposal digest does not match")
            terminal = row["goal_status"] in _TERMINAL_GOALS
            expired = False
            if row["started_at"] is not None:
                elapsed = (
                    datetime.now(UTC) - datetime.fromisoformat(str(row["started_at"]))
                ).total_seconds()
                expired = elapsed >= int(row["max_runtime_seconds"])
            task_id = row["apply_task_id"]
            if task_id is None:
                self._require_write_approval()
                if terminal or expired:
                    raise GoalCodeApplicationConflict(
                        "goal is no longer accepting code applications"
                    )
                if row["node_status"] != "waiting_permission":
                    raise GoalCodeApplicationConflict(
                        "code proposal is no longer waiting for review"
                    )
                child = TaskRecord.new(
                    TaskCreate(
                        input="Apply the reviewed Python file proposal", mode=TaskMode.NORMAL
                    ),
                    source=requester.id,
                )
                await StateService._insert_task(db, child)
                task_id = child.id
                await db.execute(
                    "UPDATE goal_code_proposals SET apply_task_id=?,updated_at=? WHERE node_id=?",
                    (task_id, now, node_id),
                )
                await append_audit_event(
                    db,
                    "goal.code_proposal.reviewed",
                    {
                        "goal_run_id": goal_run_id,
                        "node_id": node_id,
                        "sha256": reviewed_sha256,
                        "apply_task_id": task_id,
                    },
                    actor_type="device",
                    actor_id=requester.id,
                    task_id=task_id,
                    trace_id=goal_run_id,
                    created_at=now,
                )
            arguments = {"path": str(row["path"]), "content": str(row["content"])}
            await db.commit()
        existing = await self._existing_application(str(task_id), arguments)
        if existing is not None:
            return existing
        if terminal or expired:
            raise GoalCodeApplicationConflict("goal is no longer accepting code applications")
        self._require_write_approval()
        try:
            return await self.execution_engine.create_tool_call(
                task_id=str(task_id),
                tool_name="workspace.write_text",
                arguments=arguments,
                summary="Apply the reviewed Python file proposal",
                requester=requester,
            )
        except ExecutionConflict:
            # Another request can win after the durable child link is committed.
            existing = await self._existing_application(str(task_id), arguments)
            if existing is not None:
                return existing
            raise

    def _require_write_approval(self) -> None:
        if self.execution_engine.policy.evaluate_tool("workspace.write_text").decision != "ask":
            raise GoalCodeApplicationConflict("code application requires one-use write approval")

    async def _existing_application(
        self, task_id: str, arguments: dict[str, str]
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            rows = list(
                await (
                    await db.execute(
                        "SELECT id,tool_name,arguments_json FROM tool_calls WHERE task_id=?",
                        (task_id,),
                    )
                ).fetchall()
            )
        if not rows:
            return None
        try:
            matches = (
                len(rows) == 1
                and rows[0][1] == "workspace.write_text"
                and json.loads(str(rows[0][2])) == arguments
            )
        except (ValueError, TypeError):
            matches = False
        if not matches:
            raise GoalCodeApplicationConflict(
                "application task does not match the reviewed proposal"
            )
        return await self.execution_engine.get(str(rows[0][0]))

    async def synchronize(
        self,
        tool_call_id: str | None = None,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> set[str]:
        """Project durable local execution outcomes without replaying approved writes."""

        await ApprovalGateway(self.db_path).expire_pending()
        now = self._now()
        changed: set[str] = set()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            rows = await (
                await db.execute(
                    """SELECT p.node_id,p.goal_run_id,p.path,p.sha256,p.content,g.root_task_id,
                        c.id AS tool_call_id,c.status AS tool_status,c.result_json,
                        c.tool_name,c.arguments_json
                    FROM goal_code_proposals AS p
                    JOIN plan_nodes AS n ON n.id=p.node_id
                    JOIN goal_runs AS g ON g.id=p.goal_run_id
                    JOIN tool_calls AS c ON c.task_id=p.apply_task_id
                    WHERE n.status='waiting_permission'
                      AND g.status NOT IN ('completed','failed','cancelled','budget_exhausted')
                      AND c.status IN ('completed','failed','denied','cancelled')
                      AND (? IS NULL OR c.id=?)""",
                    (tool_call_id, tool_call_id),
                )
            ).fetchall()
            for row in rows:
                succeeded = row["tool_status"] == "completed"
                if succeeded:
                    try:
                        receipt = json.loads(str(row["result_json"]))
                        arguments = json.loads(str(row["arguments_json"]))
                        succeeded = (
                            isinstance(receipt, dict)
                            and receipt.get("path") == row["path"]
                            and receipt.get("bytes") == len(str(row["content"]).encode("utf-8"))
                            and row["tool_name"] == "workspace.write_text"
                            and arguments == {"path": row["path"], "content": row["content"]}
                        )
                    except (ValueError, TypeError):
                        succeeded = False
                status = "completed" if succeeded else "failed"
                summary = (
                    f"Approved Python file written to {row['path']}. "
                    "The file has not been executed or tested."
                    if succeeded
                    else None
                )
                error = (
                    None if succeeded else "Python file application did not complete successfully."
                )
                await db.execute(
                    """UPDATE plan_nodes SET status=?,result_summary=?,error_summary=?,
                        updated_at=?,completed_at=? WHERE id=? AND status='waiting_permission'""",
                    (status, summary, error, now, now, row["node_id"]),
                )
                goal_id = str(row["goal_run_id"])
                changed.add(goal_id)
                await append_audit_event(
                    db,
                    "goal.code_proposal.applied" if succeeded else "goal.code_proposal.failed",
                    {
                        "goal_run_id": goal_id,
                        "node_id": row["node_id"],
                        "tool_call_id": row["tool_call_id"],
                        "sha256": row["sha256"],
                    },
                    actor_type="control-plane",
                    actor_id="goal-manager",
                    task_id=str(row["root_task_id"]),
                    trace_id=goal_id,
                    created_at=now,
                )
            for goal_id in changed:
                waiting = await (
                    await db.execute(
                        "SELECT 1 FROM plan_nodes WHERE goal_run_id=? AND status='waiting_permission' LIMIT 1",
                        (goal_id,),
                    )
                ).fetchone()
                if waiting is None:
                    await db.execute(
                        "UPDATE goal_runs SET status='running',current_phase='dispatching',updated_at=? WHERE id=?",
                        (now, goal_id),
                    )
                    await db.execute(
                        "UPDATE tasks SET status='running',updated_at=? WHERE id=(SELECT root_task_id FROM goal_runs WHERE id=?)",
                        (now, goal_id),
                    )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return changed
