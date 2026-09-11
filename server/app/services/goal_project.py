from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.models import TaskCreate, TaskMode, TaskRecord
from app.services.approval_gateway import ApprovalGateway
from app.services.audit_log import append_audit_event
from app.services.context_builder import safe_context_text
from app.services.execution_engine import AuthenticatedRequester, ExecutionConflict, ExecutionEngine
from app.services.goal_limits import runtime_expired
from app.services.maintenance_lease import MaintenanceLeaseGuard
from app.services.project_contracts import (
    PROJECT_SKILL,
    ProjectPayload,
    ProjectResult,
    ProjectWriteArguments,
    project_digest,
)
from app.services.project_memory import ProjectMemoryService
from app.services.state_service import StateService

TERMINAL = frozenset({"completed", "failed", "cancelled", "budget_exhausted"})


class GoalProjectConflict(RuntimeError):
    """A project operation no longer matches its durable revision or goal."""


class GoalProjectService:
    """Persist private project snapshots and publish one reviewed immutable revision."""

    def __init__(
        self,
        db_path: Path,
        execution_engine: ExecutionEngine,
        *,
        memory: ProjectMemoryService | None = None,
    ) -> None:
        self.db_path = db_path
        self.execution_engine = execution_engine
        self.memory = memory

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    async def ensure_project(self, goal_id: str) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT project_id FROM goal_project_links WHERE goal_run_id=?", (goal_id,)
                )
            ).fetchone()
            if existing:
                return str(existing[0])
            if not await (
                await db.execute("SELECT 1 FROM goal_runs WHERE id=?", (goal_id,))
            ).fetchone():
                raise GoalProjectConflict("goal not found")
            project_id = f"project_{uuid4().hex}"
            now = self._now()
            await db.execute("INSERT INTO coding_projects VALUES(?,?,?)", (project_id, now, now))
            await db.execute("INSERT INTO goal_project_links VALUES(?,?)", (goal_id, project_id))
            # An explicit continuation of a legacy Python proposal preserves its
            # actual source rather than silently beginning from an empty project.
            legacy = await (
                await db.execute(
                    """SELECT node_id,worker_job_id,content FROM goal_code_proposals
                    WHERE goal_run_id=? ORDER BY created_at DESC LIMIT 1""",
                    (goal_id,),
                )
            ).fetchone()
            if legacy:
                seed = ProjectResult.model_validate(
                    {
                        "schema_version": "1.0",
                        "action": "continue",
                        "message": "Existing Python source retained for project continuation.",
                        "plan": ["Extend the existing application and verify it with tests."],
                        "files": [{"path": "app.py", "content": str(legacy[2])}],
                        "checks": [],
                        "run_instructions": "",
                        "runtime": "python",
                        "base_revision_id": None,
                        "base_sha256": None,
                    }
                )
                await db.execute(
                    """INSERT INTO project_revisions(id,project_id,goal_run_id,node_id,
                    worker_job_id,revision,snapshot_json,sha256,created_at)
                    VALUES(?,?,?,?,?,1,?,?,?)""",
                    (
                        f"revision_{uuid4().hex}",
                        project_id,
                        goal_id,
                        str(legacy[0]),
                        str(legacy[1]),
                        seed.model_dump_json(),
                        project_digest(seed.files),
                        now,
                    ),
                )
            await db.commit()
            return project_id

    async def inherit_project(self, parent_goal_id: str, new_goal_id: str) -> None:
        project_id = await self.ensure_project(parent_goal_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT project_id FROM goal_project_links WHERE goal_run_id=?", (new_goal_id,)
                )
            ).fetchone()
            if existing and existing[0] != project_id:
                raise GoalProjectConflict("continuation belongs to a different project")
            await db.execute(
                "INSERT OR IGNORE INTO goal_project_links VALUES(?,?)", (new_goal_id, project_id)
            )
            await db.commit()

    async def _latest(self, goal_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """SELECT r.* FROM project_revisions r JOIN goal_project_links l
                    ON l.project_id=r.project_id WHERE l.goal_run_id=?
                    ORDER BY r.revision DESC LIMIT 1""",
                    (goal_id,),
                )
            ).fetchone()
        return dict(row) if row else None

    async def payload(
        self, goal_id: str, node: dict[str, Any], conversation: list[dict[str, str]]
    ) -> dict[str, Any]:
        await self.ensure_project(goal_id)
        latest = await self._latest(goal_id)
        snapshot = ProjectResult.model_validate_json(latest["snapshot_json"]) if latest else None
        memory = None
        if self.memory is not None:
            async with aiosqlite.connect(self.db_path) as db:
                goal = await (
                    await db.execute(
                        "SELECT conversation_revision FROM goal_runs WHERE id=?", (goal_id,)
                    )
                ).fetchone()
            latest_user = next(
                (
                    message["content"]
                    for message in reversed(conversation)
                    if message["role"] == "user"
                ),
                "",
            )
            memory = await self.memory.retrieve(
                goal_id,
                str(node["id"]),
                f"{latest_user}\n{node['objective']}",
                base_revision_id=str(latest["id"]) if latest else None,
                conversation_revision=int(goal[0]) if goal else None,
            )
        return ProjectPayload.model_validate(
            {
                "objective": safe_context_text(str(node["objective"]), max_chars=4_000),
                "conversation": [
                    {
                        "role": message["role"],
                        "content": safe_context_text(message["content"], max_chars=4_000),
                    }
                    for message in conversation[-40:]
                ],
                "files": [file.model_dump() for file in snapshot.files] if snapshot else [],
                "plan": snapshot.plan if snapshot else [],
                "checks": [check.model_dump() for check in snapshot.checks] if snapshot else [],
                "iteration": int(latest["revision"]) + 1 if latest else 1,
                "base_revision_id": latest["id"] if latest else None,
                "base_sha256": latest["sha256"] if latest else None,
                "focus_paths": snapshot.focus_paths if snapshot else [],
                "memory": memory,
            }
        ).model_dump()

    async def capture_result(
        self,
        goal_id: str,
        node_id: str,
        job_id: str,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, Any]:
        project_id = await self.ensure_project(goal_id)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard:
                await maintenance_guard.require_current_locked(db)
            existing = await (
                await db.execute("SELECT * FROM project_revisions WHERE node_id=?", (node_id,))
            ).fetchone()
            if existing:
                if existing["worker_job_id"] != job_id or existing["goal_run_id"] != goal_id:
                    raise GoalProjectConflict("project result belongs to another job")
                return self._captured(dict(existing))
            row = await (
                await db.execute(
                    """SELECT g.status AS goal_status,n.status AS node_status,j.status,
                    j.result_json,j.payload_json,g.root_task_id FROM plan_nodes n
                    JOIN goal_runs g ON g.id=n.goal_run_id
                    JOIN agent_jobs j ON j.id=n.worker_job_id AND j.task_id=n.task_id
                    WHERE n.id=? AND g.id=? AND j.id=?
                    AND n.required_skill=? AND j.required_skill=?""",
                    (node_id, goal_id, job_id, PROJECT_SKILL, PROJECT_SKILL),
                )
            ).fetchone()
            if (
                row is None
                or row["goal_status"] in TERMINAL
                or row["status"] != "completed"
                or row["node_status"] not in {"running", "dispatched"}
            ):
                raise GoalProjectConflict("project job is not accepting a result")
            try:
                result = ProjectResult.model_validate_json(str(row["result_json"]))
                payload = ProjectPayload.model_validate_json(str(row["payload_json"]))
            except ValueError as exc:
                raise GoalProjectConflict("project job returned an invalid snapshot") from exc
            latest = await (
                await db.execute(
                    "SELECT id,sha256,revision FROM project_revisions WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                    (project_id,),
                )
            ).fetchone()
            expected_base = (str(latest[0]), str(latest[1])) if latest else (None, None)
            if (payload.base_revision_id, payload.base_sha256) != expected_base or (
                result.base_revision_id,
                result.base_sha256,
            ) != expected_base:
                raise GoalProjectConflict("project changed while its iteration was running")
            now, revision_id = self._now(), f"revision_{uuid4().hex}"
            digest = project_digest(result.files)
            revision = int(latest[2]) + 1 if latest else 1
            await db.execute(
                """INSERT INTO project_revisions(id,project_id,goal_run_id,node_id,worker_job_id,
                revision,snapshot_json,sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    revision_id,
                    project_id,
                    goal_id,
                    node_id,
                    job_id,
                    revision,
                    result.model_dump_json(),
                    digest,
                    now,
                ),
            )
            await db.execute(
                "UPDATE coding_projects SET updated_at=? WHERE id=?", (now, project_id)
            )
            await append_audit_event(
                db,
                "goal.project.revision",
                {
                    "goal_run_id": goal_id,
                    "node_id": node_id,
                    "project_id": project_id,
                    "revision_id": revision_id,
                    "sha256": digest,
                    "file_count": len(result.files),
                    "action": result.action,
                },
                actor_type="control-plane",
                actor_id="goal-manager",
                task_id=row["root_task_id"],
                trace_id=goal_id,
                created_at=now,
            )
            if maintenance_guard:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return {
            **result.model_dump(),
            "revision_id": revision_id,
            "project_id": project_id,
            "sha256": digest,
        }

    @staticmethod
    def _captured(row: dict[str, Any]) -> dict[str, Any]:
        result = ProjectResult.model_validate_json(row["snapshot_json"])
        return {
            **result.model_dump(),
            "revision_id": row["id"],
            "project_id": row["project_id"],
            "sha256": row["sha256"],
        }

    async def get_project(self, goal_id: str) -> dict[str, Any] | None:
        latest = await self._latest(goal_id)
        if latest is None:
            return None
        result = ProjectResult.model_validate_json(latest["snapshot_json"])
        async with aiosqlite.connect(self.db_path) as db:
            goal = await (
                await db.execute(
                    "SELECT status,current_phase FROM goal_runs WHERE id=?", (goal_id,)
                )
            ).fetchone()
            call = await (
                await db.execute(
                    "SELECT status FROM tool_calls WHERE task_id=?", (latest["apply_task_id"],)
                )
            ).fetchone()
        state = "building"
        if call:
            state = (
                "applied"
                if call[0] == "completed"
                else "failed"
                if call[0] in {"denied", "failed", "cancelled"}
                else "waiting_permission"
            )
        elif goal and latest["goal_run_id"] == goal_id:
            if goal[0] in TERMINAL:
                state = "failed"
            elif goal[1] == "needs_user":
                state = "needs_user"
            elif goal[1] == "project_ready" and result.action == "complete":
                state = "ready"
        return {
            "project_id": latest["project_id"],
            "revision_id": latest["id"],
            "revision": latest["revision"],
            "sha256": latest["sha256"],
            "state": state,
            "message": result.message,
            "plan": result.plan,
            "files": [file.model_dump() for file in result.files],
            "checks": [check.model_dump() for check in result.checks],
            "run_instructions": result.run_instructions,
            "runtime": result.runtime,
            "task_id": latest["apply_task_id"] if call else None,
        }

    async def apply(
        self,
        goal_id: str,
        revision_id: str,
        requester: AuthenticatedRequester,
        sha256: str,
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    """SELECT r.*,g.status AS goal_status,g.current_phase,g.started_at,
                    g.max_runtime_seconds,g.paused_at,g.paused_seconds,n.status AS node_status,
                    g.conversation_revision,n.conversation_revision AS node_conversation_revision
                    FROM project_revisions r JOIN goal_runs g ON g.id=r.goal_run_id
                    JOIN plan_nodes n ON n.id=r.node_id WHERE r.id=? AND r.goal_run_id=?""",
                    (revision_id, goal_id),
                )
            ).fetchone()
            if row is None or not hmac.compare_digest(str(row["sha256"]), sha256):
                raise GoalProjectConflict("reviewed project revision does not match")
            result = ProjectResult.model_validate_json(str(row["snapshot_json"]))
            args = ProjectWriteArguments(
                project_id=row["project_id"],
                revision_id=revision_id,
                sha256=sha256,
                files=result.files,
            ).model_dump()
            task_id = row["apply_task_id"]
            latest = await (
                await db.execute(
                    "SELECT id FROM project_revisions WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                    (row["project_id"],),
                )
            ).fetchone()
            accepting = (
                row["goal_status"] == "waiting_permission"
                and row["current_phase"] == "project_ready"
                and row["node_status"] == "waiting_permission"
                and not runtime_expired(dict(row))
                and result.action == "complete"
                and latest is not None
                and latest[0] == revision_id
                and row["conversation_revision"] == row["node_conversation_revision"]
            )
            if task_id is None:
                if not accepting:
                    raise GoalProjectConflict("project is no longer waiting for this review")
                self._require_approval()
                child = TaskRecord.new(
                    TaskCreate(input="Save the reviewed project revision", mode=TaskMode.NORMAL),
                    source=requester.id,
                )
                await StateService._insert_task(db, child)
                task_id = child.id
                await db.execute(
                    "UPDATE project_revisions SET apply_task_id=? WHERE id=?",
                    (task_id, revision_id),
                )
                await append_audit_event(
                    db,
                    "goal.project.reviewed",
                    {
                        "goal_run_id": goal_id,
                        "revision_id": revision_id,
                        "sha256": sha256,
                        "apply_task_id": task_id,
                    },
                    actor_type="device",
                    actor_id=requester.id,
                    task_id=task_id,
                    trace_id=goal_id,
                    created_at=self._now(),
                )
            await db.commit()
        existing = await self._existing_application(str(task_id), args)
        if existing:
            return existing
        if not accepting:
            raise GoalProjectConflict("project is no longer accepting an application")
        self._require_approval()
        try:
            return await self.execution_engine.create_tool_call(
                task_id=str(task_id),
                tool_name="workspace.write_project",
                arguments=args,
                summary="Save one reviewed project revision",
                requester=requester,
            )
        except ExecutionConflict:
            existing = await self._existing_application(str(task_id), args)
            if existing:
                return existing
            raise

    def _require_approval(self) -> None:
        if self.execution_engine.policy.evaluate_tool("workspace.write_project").decision != "ask":
            raise GoalProjectConflict("project publication requires one-use approval")

    async def _existing_application(
        self, task_id: str, args: dict[str, Any]
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
        if (
            len(rows) != 1
            or rows[0][1] != "workspace.write_project"
            or json.loads(rows[0][2]) != args
        ):
            raise GoalProjectConflict("application task differs from reviewed project")
        return await self.execution_engine.get(str(rows[0][0]))

    async def application_task_ids(self, goal_id: str) -> list[str]:
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (
                await db.execute(
                    "SELECT apply_task_id FROM project_revisions WHERE goal_run_id=? AND apply_task_id IS NOT NULL",
                    (goal_id,),
                )
            ).fetchall()
        return [str(row[0]) for row in rows]

    async def synchronize(
        self,
        tool_call_id: str | None = None,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> set[str]:
        await ApprovalGateway(self.db_path).expire_pending()
        changed: set[str] = set()
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard:
                await maintenance_guard.require_current_locked(db)
            rows = await (
                await db.execute(
                    """SELECT r.*,c.id AS call_id,c.tool_name,c.arguments_json,c.result_json,
                c.status AS tool_status,g.root_task_id FROM project_revisions r
                JOIN tool_calls c ON c.task_id=r.apply_task_id
                JOIN plan_nodes n ON n.id=r.node_id JOIN goal_runs g ON g.id=r.goal_run_id
                WHERE n.status='waiting_permission' AND g.status NOT IN
                ('completed','failed','cancelled','budget_exhausted') AND c.status IN
                ('completed','failed','denied','cancelled') AND (? IS NULL OR c.id=?)""",
                    (tool_call_id, tool_call_id),
                )
            ).fetchall()
            for row in rows:
                snapshot = ProjectResult.model_validate_json(str(row["snapshot_json"]))
                arguments = ProjectWriteArguments(
                    project_id=row["project_id"],
                    revision_id=row["id"],
                    sha256=row["sha256"],
                    files=snapshot.files,
                )
                try:
                    receipt = json.loads(str(row["result_json"]))
                    succeeded = (
                        row["tool_status"] == "completed"
                        and row["tool_name"] == "workspace.write_project"
                        and json.loads(str(row["arguments_json"])) == arguments.model_dump()
                        and receipt
                        == {
                            "path": arguments.path,
                            "sha256": arguments.sha256,
                            "files": len(arguments.files),
                            "bytes": sum(len(f.content.encode("utf-8")) for f in arguments.files),
                        }
                    )
                except (ValueError, TypeError):
                    succeeded = False
                await db.execute(
                    """UPDATE plan_nodes SET status=?,result_summary=?,error_summary=?,
                    updated_at=?,completed_at=? WHERE id=? AND status='waiting_permission'""",
                    (
                        "completed" if succeeded else "failed",
                        f"Project revision saved to {arguments.path}; isolated build and test checks passed. Not deployed."
                        if succeeded
                        else None,
                        None if succeeded else "Reviewed project publication did not complete.",
                        now,
                        now,
                        row["node_id"],
                    ),
                )
                goal_id = str(row["goal_run_id"])
                changed.add(goal_id)
                await append_audit_event(
                    db,
                    "goal.project.applied" if succeeded else "goal.project.failed",
                    {
                        "goal_run_id": goal_id,
                        "revision_id": row["id"],
                        "sha256": row["sha256"],
                        "tool_call_id": row["call_id"],
                    },
                    actor_type="control-plane",
                    actor_id="goal-manager",
                    task_id=row["root_task_id"],
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
                if not waiting:
                    await db.execute(
                        """UPDATE goal_runs SET status='running',current_phase='dispatching',
                        paused_seconds=paused_seconds+CASE WHEN paused_at IS NULL THEN 0
                        ELSE MAX(0,(julianday(?)-julianday(paused_at))*86400.0) END,
                        paused_at=NULL,updated_at=? WHERE id=?""",
                        (now, now, goal_id),
                    )
                    await db.execute(
                        "UPDATE tasks SET status='running',updated_at=? WHERE id=(SELECT root_task_id FROM goal_runs WHERE id=?)",
                        (now, goal_id),
                    )
            if maintenance_guard:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return changed
