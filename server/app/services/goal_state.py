from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import aiosqlite

from app.services.feedback_dataset import redact_dataset_text
from app.services.maintenance_lease import MaintenanceLeaseGuard

PUBLIC_GOAL_FIELDS = (
    "id",
    "root_task_id",
    "objective",
    "status",
    "autonomy_profile",
    "planner_source",
    "max_steps",
    "max_parallelism",
    "max_replans",
    "max_runtime_seconds",
    "max_model_calls",
    "step_count",
    "replan_count",
    "model_call_count",
    "completion_criteria",
    "current_phase",
    "evaluator_status",
    "evaluator_summary",
    "created_at",
    "updated_at",
    "started_at",
    "completed_at",
    "failure_reason",
)
PUBLIC_NODE_FIELDS = (
    "id",
    "goal_run_id",
    "parent_node_id",
    "node_type",
    "title",
    "objective",
    "required_skill",
    "status",
    "priority",
    "depends_on",
    "assigned_agent_id",
    "worker_job_id",
    "task_id",
    "expected_output",
    "result_summary",
    "error_summary",
    "created_at",
    "updated_at",
    "completed_at",
)


def public_goal(record: dict[str, Any]) -> dict[str, Any]:
    """Return only the documented mobile-facing goal projection."""

    return {field: record.get(field) for field in PUBLIC_GOAL_FIELDS}


def public_plan_node(record: dict[str, Any]) -> dict[str, Any]:
    """Omit planner metadata and internal fingerprints from shared responses."""

    return {field: record.get(field) for field in PUBLIC_NODE_FIELDS}


def public_goal_result(
    record: dict[str, Any],
    *,
    goal: dict[str, Any],
) -> dict[str, Any]:
    """Project the internal safe aggregation into the stable SwarmResult API."""

    raw_nodes = record.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    completed_nodes = [
        str(node.get("node_id"))
        for node in nodes
        if isinstance(node, dict)
        and node.get("status") == "completed"
        and isinstance(node.get("node_id"), str)
    ]
    failed_nodes = [
        str(node.get("node_id"))
        for node in nodes
        if isinstance(node, dict)
        and node.get("status") in {"failed", "blocked", "cancelled", "skipped"}
        and isinstance(node.get("node_id"), str)
    ]
    agents_used: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        provenance = node.get("provenance")
        agent_id = provenance.get("agent_id") if isinstance(provenance, dict) else None
        if isinstance(agent_id, str) and agent_id and agent_id not in agents_used:
            agents_used.append(agent_id)
    provenance = record.get("provenance")
    root_task_id = (
        provenance.get("root_task_id") if isinstance(provenance, dict) else goal.get("root_task_id")
    )
    limitations = [
        str(node.get("error_summary") or f"{node.get('title', 'Node')} did not complete")
        for node in nodes
        if isinstance(node, dict)
        and node.get("status") in {"failed", "blocked", "cancelled", "skipped"}
    ]
    return {
        "goal_run_id": str(record.get("goal_run_id") or goal["id"]),
        "root_task_id": str(root_task_id or goal["root_task_id"]),
        "status": str(record.get("status") or goal["status"]),
        "answer": str(record.get("answer") or record.get("summary") or ""),
        "completed_nodes": completed_nodes,
        "failed_nodes": failed_nodes,
        "agents_used": agents_used,
        "memory_ids": [str(item) for item in record.get("memory_ids", []) if isinstance(item, str)],
        "episode_ids": [
            str(item) for item in record.get("episode_ids", []) if isinstance(item, str)
        ],
        "started_at": str(goal.get("started_at") or goal["created_at"]),
        "completed_at": str(goal.get("completed_at") or goal["updated_at"]),
        "limitations": limitations,
    }


class GoalStateConflict(RuntimeError):
    """A goal or plan-node transition lost its authoritative compare-and-swap."""


class GoalStateService:
    GOAL_TERMINAL: ClassVar[frozenset[str]] = frozenset(
        {"completed", "failed", "cancelled", "budget_exhausted"}
    )
    NODE_TERMINAL: ClassVar[frozenset[str]] = frozenset(
        {"completed", "failed", "blocked", "cancelled", "skipped"}
    )
    GOAL_TRANSITIONS: ClassVar[dict[str, frozenset[str]]] = {
        "planning": frozenset(
            {"running", "waiting_permission", "failed", "cancelled", "budget_exhausted"}
        ),
        "running": frozenset(
            {
                "planning",
                "waiting_permission",
                "completed",
                "failed",
                "cancelled",
                "budget_exhausted",
            }
        ),
        "waiting_permission": frozenset(
            {"running", "planning", "failed", "cancelled", "budget_exhausted"}
        ),
        "completed": frozenset(),
        "failed": frozenset(),
        "cancelled": frozenset(),
        "budget_exhausted": frozenset(),
    }
    NODE_TRANSITIONS: ClassVar[dict[str, frozenset[str]]] = {
        "planned": frozenset({"ready", "blocked", "cancelled", "skipped"}),
        "ready": frozenset(
            {"dispatched", "running", "waiting_permission", "blocked", "cancelled", "skipped"}
        ),
        "dispatched": frozenset(
            {
                "running",
                "waiting_permission",
                "waiting_capability",
                "completed",
                "failed",
                "cancelled",
            }
        ),
        "running": frozenset(
            {"waiting_permission", "waiting_capability", "completed", "failed", "cancelled"}
        ),
        "waiting_permission": frozenset({"ready", "failed", "cancelled"}),
        "waiting_capability": frozenset({"running", "failed", "cancelled"}),
        "completed": frozenset(),
        "failed": frozenset(),
        "blocked": frozenset(),
        "cancelled": frozenset(),
        "skipped": frozenset(),
    }

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _safe_summary(value: str | None) -> str | None:
        redacted = redact_dataset_text(value)
        return redacted[:4_000] if redacted is not None else None

    async def transition_goal(
        self,
        goal_run_id: str,
        *,
        expected: str,
        target: str,
        failure_reason: str | None = None,
        current_phase: str | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, Any]:
        allowed = self.GOAL_TRANSITIONS.get(expected, frozenset())
        if target not in allowed:
            raise GoalStateConflict(f"goal cannot transition from {expected} to {target}")
        now = self._now()
        terminal = target in self.GOAL_TERMINAL
        safe_failure = self._safe_summary(failure_reason)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            cursor = await db.execute(
                """
                UPDATE goal_runs
                SET status=?,current_phase=?,updated_at=?,completed_at=?,failure_reason=?
                WHERE id=? AND status=?
                """,
                (
                    target,
                    current_phase or target,
                    now,
                    now if terminal else None,
                    safe_failure,
                    goal_run_id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise GoalStateConflict("goal changed during transition")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        record = await self.get_goal(goal_run_id)
        if record is None:
            raise RuntimeError("goal disappeared after transition")
        return record

    async def transition_node(
        self,
        node_id: str,
        *,
        expected: str,
        target: str,
        result_summary: str | None = None,
        error_summary: str | None = None,
        assigned_agent_id: str | None = None,
        worker_job_id: str | None = None,
        task_id: str | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, Any]:
        allowed = self.NODE_TRANSITIONS.get(expected, frozenset())
        if target not in allowed:
            raise GoalStateConflict(f"node cannot transition from {expected} to {target}")
        now = self._now()
        terminal = target in self.NODE_TERMINAL
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            cursor = await db.execute(
                """
                UPDATE plan_nodes
                SET status=?,result_summary=COALESCE(?,result_summary),
                    error_summary=COALESCE(?,error_summary),
                    assigned_agent_id=COALESCE(?,assigned_agent_id),
                    worker_job_id=COALESCE(?,worker_job_id),
                    task_id=COALESCE(?,task_id),updated_at=?,completed_at=?
                WHERE id=? AND status=?
                """,
                (
                    target,
                    self._safe_summary(result_summary),
                    self._safe_summary(error_summary),
                    assigned_agent_id,
                    worker_job_id,
                    task_id,
                    now,
                    now if terminal else None,
                    node_id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise GoalStateConflict("node changed during transition")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        record = await self.get_node(node_id)
        if record is None:
            raise RuntimeError("plan node disappeared after transition")
        return record

    async def complete_synthesis_node(
        self,
        node_id: str,
        *,
        result_summary: str,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, Any]:
        """Atomically consume one step and finish a ready synthesis node."""

        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            node = await (
                await db.execute(
                    """SELECT n.goal_run_id,n.status,n.node_type,g.status AS goal_status,
                              g.step_count,g.max_steps
                    FROM plan_nodes AS n JOIN goal_runs AS g ON g.id=n.goal_run_id
                    WHERE n.id=?""",
                    (node_id,),
                )
            ).fetchone()
            if node is None:
                await db.rollback()
                raise GoalStateConflict("synthesis node not found")
            if (
                str(node["node_type"]) != "synthesis"
                or str(node["status"]) != "ready"
                or str(node["goal_status"]) != "running"
            ):
                await db.rollback()
                raise GoalStateConflict("synthesis node changed before completion")
            if int(node["step_count"]) >= int(node["max_steps"]):
                await db.rollback()
                raise GoalStateConflict("goal step budget exhausted")
            cursor = await db.execute(
                """UPDATE plan_nodes SET status='running',updated_at=?
                WHERE id=? AND status='ready'""",
                (now, node_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise GoalStateConflict("synthesis node changed while starting")
            cursor = await db.execute(
                """UPDATE plan_nodes SET status='completed',result_summary=?,updated_at=?,
                    completed_at=? WHERE id=? AND status='running'""",
                (self._safe_summary(result_summary), now, now, node_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise GoalStateConflict("synthesis node changed while completing")
            cursor = await db.execute(
                """UPDATE goal_runs SET step_count=step_count+1,updated_at=?
                WHERE id=? AND status='running' AND step_count < max_steps""",
                (now, str(node["goal_run_id"])),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise GoalStateConflict("goal changed while synthesis completed")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        record = await self.get_node(node_id)
        if record is None:
            raise RuntimeError("synthesis node disappeared after completion")
        return record

    async def refresh_ready_nodes(
        self,
        goal_run_id: str,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> list[str]:
        """Advance planned nodes only from terminal, acceptable dependencies."""

        now = self._now()
        ready: list[str] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            nodes = await (
                await db.execute(
                    """
                    SELECT id FROM plan_nodes
                    WHERE goal_run_id=? AND status='planned'
                    ORDER BY priority DESC,created_at ASC,id ASC
                    """,
                    (goal_run_id,),
                )
            ).fetchall()
            for node in nodes:
                node_id = str(node["id"])
                dependencies = await (
                    await db.execute(
                        """
                        SELECT e.dependency_type,n.status
                        FROM plan_edges AS e
                        JOIN plan_nodes AS n ON n.id=e.from_node_id
                        WHERE e.goal_run_id=? AND e.to_node_id=?
                        ORDER BY e.from_node_id
                        """,
                        (goal_run_id, node_id),
                    )
                ).fetchall()
                hard_failed = any(
                    str(item["dependency_type"]) == "hard"
                    and str(item["status"]) in {"failed", "blocked", "cancelled", "skipped"}
                    for item in dependencies
                )
                if hard_failed:
                    cursor = await db.execute(
                        """
                        UPDATE plan_nodes SET status='blocked',error_summary=?,
                            updated_at=?,completed_at=?
                        WHERE id=? AND status='planned'
                        """,
                        ("hard dependency failed", now, now, node_id),
                    )
                    if cursor.rowcount != 1:
                        await db.rollback()
                        raise GoalStateConflict("node changed while dependency failure propagated")
                    continue
                if any(str(item["status"]) not in self.NODE_TERMINAL for item in dependencies):
                    continue
                if any(
                    str(item["dependency_type"]) == "hard" and str(item["status"]) != "completed"
                    for item in dependencies
                ):
                    continue
                cursor = await db.execute(
                    """
                    UPDATE plan_nodes SET status='ready',updated_at=?
                    WHERE id=? AND status='planned'
                    """,
                    (now, node_id),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    raise GoalStateConflict("node changed while becoming ready")
                ready.append(node_id)
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return ready

    async def get_goal(self, goal_run_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_run_id,))
            ).fetchone()
        return self._goal_from_row(row) if row is not None else None

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,))
            ).fetchone()
        return self._node_from_row(row) if row is not None else None

    async def list_nodes(self, goal_run_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM plan_nodes WHERE goal_run_id=?
                    ORDER BY priority DESC,created_at ASC,id ASC
                    """,
                    (goal_run_id,),
                )
            ).fetchall()
        return [self._node_from_row(row) for row in rows]

    async def list_goals(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 500))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM goal_runs ORDER BY updated_at DESC,id ASC LIMIT ?",
                    (bounded_limit,),
                )
            ).fetchall()
        return [self._goal_from_row(row) for row in rows]

    async def node_for_job(self, job_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM plan_nodes WHERE worker_job_id=?", (job_id,))
            ).fetchone()
        return self._node_from_row(row) if row is not None else None

    async def get_result(self, goal_run_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM goal_results WHERE goal_run_id=?", (goal_run_id,))
            ).fetchone()
            goal_row = await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_run_id,))
            ).fetchone()
        if row is None:
            return None
        record = json.loads(str(row["result_json"]))
        if not isinstance(record, dict):
            raise TypeError("stored goal result is corrupted")
        if goal_row is None:
            raise RuntimeError("goal result lost its parent goal")
        return public_goal_result(record, goal=self._goal_from_row(goal_row))

    @staticmethod
    def _goal_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        record = dict(row)
        record["completion_criteria"] = json.loads(str(record.pop("completion_criteria_json")))
        return record

    @staticmethod
    def _node_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        record = dict(row)
        record["depends_on"] = json.loads(str(record.pop("depends_on_json")))
        record["planner_metadata"] = json.loads(str(record.pop("planner_metadata_json")))
        return record
