"""Read-only, bounded agent evidence attached to a root or child task."""

from pathlib import Path
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from app.services.result_aggregator import SafeIdentifier, SafeNodeResult, aggregate_goal_rows
from app.services.swarm_contracts import GoalRunStatus

MAX_TASK_EXECUTION_NODES = 20


class TaskGoalExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    task_id: SafeIdentifier
    goal_run_id: SafeIdentifier
    root_task_id: SafeIdentifier
    status: GoalRunStatus
    nodes: list[SafeNodeResult] = Field(max_length=MAX_TASK_EXECUTION_NODES)
    truncated: bool


async def read_task_goal_execution(db_path: Path, task_id: str) -> TaskGoalExecution | None:
    # Never call GoalManager.get_goal or ResultAggregator.aggregate_goal here:
    # those paths may reconcile/persist state. A task read must not advance work.
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        try:
            goal = await (
                await db.execute(
                    """SELECT g.id,g.root_task_id,g.status
                    FROM goal_runs g WHERE g.root_task_id=? OR EXISTS (
                        SELECT 1 FROM plan_nodes n WHERE n.goal_run_id=g.id AND n.task_id=?
                    ) ORDER BY g.id LIMIT 1""",
                    (task_id, task_id),
                )
            ).fetchone()
            if goal is None:
                return None
            rows = await (
                await db.execute(
                    """SELECT id,node_type,status,required_skill,assigned_agent_id,worker_job_id,
                              substr(title,1,4096) AS title,
                              substr(result_summary,1,4096) AS result_summary,
                              substr(error_summary,1,2048) AS error_summary
                    FROM plan_nodes WHERE goal_run_id=? AND node_type='worker'
                        AND (?=1 OR task_id=?)
                    ORDER BY priority DESC,created_at,id LIMIT ?""",
                    (
                        goal["id"],
                        int(goal["root_task_id"] == task_id),
                        task_id,
                        MAX_TASK_EXECUTION_NODES + 1,
                    ),
                )
            ).fetchall()
        finally:
            await db.rollback()
    # The pure projection shares the existing validated, redacted summary contract.
    # No payload, generated source, full draft, raw runner log or lease is read.
    bounded_rows = list(rows)
    safe = aggregate_goal_rows(
        dict(goal), [dict(row) for row in bounded_rows[:MAX_TASK_EXECUTION_NODES]]
    )
    return TaskGoalExecution(
        task_id=task_id,
        goal_run_id=safe.goal_run_id,
        root_task_id=safe.provenance.root_task_id,
        status=safe.status,
        nodes=safe.nodes,
        truncated=len(bounded_rows) > MAX_TASK_EXECUTION_NODES,
    )
