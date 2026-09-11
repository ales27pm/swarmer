from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.audit_log import append_audit_event
from app.services.distributed_state import TaskStateMachine
from app.services.evaluator_provider import DeterministicEvaluatorProvider
from app.services.goal_manager import GoalManager
from app.services.outbox import OutboxService
from app.services.state_service import StateService
from app.services.swarm_contracts import EvaluationDecision, GoalCreateRequest, GoalStartRequest
from tests.test_goal_context_payloads import _synthesis_plan
from tests.test_goal_manager import _manager

ORPHAN_REASON = "active task has no executable local or remote work after reconciliation"
FAILURE_AT = "2026-09-11T08:51:41.035326+00:00"
PROTECTED_TABLES = (
    "goal_runs",
    "plan_nodes",
    "plan_edges",
    "goal_messages",
    "goal_conversations",
    "goal_conversation_links",
    "goal_contexts",
    "goal_model_calls",
    "goal_evaluations",
    "goal_results",
)


async def _waiting_goal(tmp_path: Path) -> tuple[GoalManager, str, str]:
    objective = "Crées une Application CRM en python"
    evaluator = DeterministicEvaluatorProvider(
        EvaluationDecision(
            schema_version="1.0",
            status="needs_user",
            reason_summary="Les besoins du produit restent à préciser.",
            missing_requirements=["Fonctionnalités souhaitées"],
            invalid_results=[],
            suggested_new_nodes=[],
            user_question="Quelles fonctionnalités souhaitez-vous ?",
        )
    )
    manager = await _manager(
        tmp_path, _synthesis_plan(objective, suffix="wait"), evaluator=evaluator
    )
    created = await manager.create_goal(
        GoalCreateRequest(objective=objective), actor_id="test-phone"
    )
    await manager.start_goal(created["id"], GoalStartRequest())
    goal = await manager.graph.get_goal(created["id"])
    assert goal is not None
    assert (goal["status"], goal["current_phase"]) == ("waiting_permission", "needs_user")
    return manager, str(goal["id"]), str(goal["root_task_id"])


async def _rows(db_path: Path, tables: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        return {
            table: [
                dict(row) for row in await (await db.execute(f"SELECT * FROM {table}")).fetchall()
            ]
            for table in tables
        }


async def _record_old_failure(db_path: Path, root_id: str, *, acknowledged: bool = False) -> int:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await TaskStateMachine.transition_locked(
            db,
            task_id=root_id,
            current="running",
            target="failed",
            now=FAILURE_AT,
            error=ORPHAN_REASON,
        )
        audit = await append_audit_event(
            db,
            "task.failed.migration",
            {"reason": ORPHAN_REASON},
            actor_type="control-plane",
            actor_id="migration",
            task_id=root_id,
            trace_id=root_id,
            created_at=FAILURE_AT,
        )
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="task",
            aggregate_id=root_id,
            topic="tasks.status",
            event_type="failed",
            payload={"task_id": root_id, "status": "failed", "reason": "orphaned_work"},
            task_id=root_id,
            message_id=root_id,
            dedupe_key=f"task:{root_id}:migration-orphaned",
            created_at=FAILURE_AT,
        )
        if acknowledged:
            await append_audit_event(
                db,
                "outbox.publication.acknowledged",
                {"outbox_id": 1},
                actor_type="control-plane",
                actor_id="test-publisher",
                task_id=root_id,
            )
        await db.commit()
    return int(audit["id"])


@pytest.mark.asyncio
async def test_restart_preserves_real_unanswered_goal_and_root_without_executable_work(
    tmp_path: Path,
) -> None:
    manager, _, root_id = await _waiting_goal(tmp_path)
    tables = (*PROTECTED_TABLES, "tasks", "audit_events", "outbox_events")
    before = await _rows(manager.db_path, tables)
    assert next(row for row in before["tasks"] if row["id"] == root_id)["status"] == "running"
    assert any(
        row["is_question"] and row["answered_by_message_id"] is None
        for row in before["goal_messages"]
    )

    await StateService(manager.db_path).initialize()
    await StateService(manager.db_path).initialize()

    assert await _rows(manager.db_path, tables) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_status", ["planning", "running", "waiting_permission"])
@pytest.mark.parametrize("task_status", ["queued", "running"])
async def test_active_goal_containers_are_not_executable_leaf_orphans(
    tmp_path: Path, goal_status: str, task_status: str
) -> None:
    manager, goal_id, root_id = await _waiting_goal(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE goal_runs SET status=? WHERE id=?", (goal_status, goal_id))
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (task_status, root_id))
        await db.commit()
    before = await _rows(manager.db_path, (*PROTECTED_TABLES, "tasks"))
    await StateService(manager.db_path).initialize()
    assert await _rows(manager.db_path, (*PROTECTED_TABLES, "tasks")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_restart_repairs_only_recorded_orphan_failure_and_keeps_history(
    tmp_path: Path, acknowledged: bool
) -> None:
    manager, goal_id, root_id = await _waiting_goal(tmp_path)
    failed_audit_id = await _record_old_failure(manager.db_path, root_id, acknowledged=acknowledged)
    tables = (*PROTECTED_TABLES, "tasks", "audit_events", "outbox_events")
    before = await _rows(manager.db_path, tables)

    await StateService(manager.db_path).initialize()

    after = await _rows(manager.db_path, tables)
    assert {table: after[table] for table in PROTECTED_TABLES} == {
        table: before[table] for table in PROTECTED_TABLES
    }
    task = next(row for row in after["tasks"] if row["id"] == root_id)
    assert (task["status"], task["completed_at"], task["error_json"]) == ("running", None, None)
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert after["outbox_events"][: len(before["outbox_events"])] == before["outbox_events"]
    repairs = [
        row for row in after["audit_events"] if row["event_type"] == "task.goal_root.recovered"
    ]
    assert len(repairs) == 1
    assert json.loads(repairs[0]["payload_json"]) == {
        "goal_run_id": goal_id,
        "migration_failure_audit_id": failed_audit_id,
        "previous_status": "failed",
        "status": "running",
    }
    recovery = after["outbox_events"][-1]
    assert recovery["topic"] == "tasks.status"
    assert recovery["event_type"] == "running"
    assert json.loads(recovery["payload_json"]) == {
        "task_id": root_id,
        "status": "running",
        "reason": "active_goal_root_recovered",
    }
    await StateService(manager.db_path).initialize()
    assert await _rows(manager.db_path, tables) == after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        "cancelled_task",
        "completed_task",
        "real_failure",
        "terminal_goal",
        "unstarted_goal",
        "completed_goal_timestamp",
        "missing_audit",
        "wrong_actor",
        "wrong_reason",
        "wrong_time",
        "later_task_audit",
        "direct_job",
        "direct_tool",
    ],
)
async def test_recovery_does_not_reopen_unproven_or_real_terminal_tasks(
    tmp_path: Path, changed: str
) -> None:
    manager, goal_id, root_id = await _waiting_goal(tmp_path)
    audit_id = await _record_old_failure(manager.db_path, root_id)
    async with aiosqlite.connect(manager.db_path) as db:
        if changed in {"cancelled_task", "completed_task"}:
            await db.execute(
                "UPDATE tasks SET status=? WHERE id=?", (changed.split("_")[0], root_id)
            )
        elif changed == "real_failure":
            await db.execute(
                "UPDATE tasks SET error_json=? WHERE id=?", ('{"message":"worker failed"}', root_id)
            )
        elif changed == "terminal_goal":
            await db.execute("UPDATE goal_runs SET status='cancelled' WHERE id=?", (goal_id,))
        elif changed == "unstarted_goal":
            await db.execute("UPDATE goal_runs SET started_at=NULL WHERE id=?", (goal_id,))
        elif changed == "completed_goal_timestamp":
            await db.execute(
                "UPDATE goal_runs SET completed_at=? WHERE id=?", (FAILURE_AT, goal_id)
            )
        elif changed == "missing_audit":
            await db.execute("DELETE FROM audit_events WHERE id=?", (audit_id,))
        elif changed == "wrong_actor":
            await db.execute(
                "UPDATE audit_events SET actor_id='test-phone' WHERE id=?", (audit_id,)
            )
        elif changed == "wrong_reason":
            await db.execute("UPDATE audit_events SET payload_json='{}' WHERE id=?", (audit_id,))
        elif changed == "wrong_time":
            await db.execute(
                "UPDATE tasks SET updated_at='2030-01-01T00:00:00+00:00' WHERE id=?", (root_id,)
            )
        elif changed == "later_task_audit":
            await append_audit_event(
                db, "task.failed", {"reason": "user decision"}, task_id=root_id
            )
        elif changed == "direct_job":
            await db.execute(
                """INSERT INTO agent_jobs(id,task_id,required_skill,payload_json,status,
                created_at,updated_at) VALUES('direct_job',?,'workspace.list_dir','{}','failed',?,?)""",
                (root_id, FAILURE_AT, FAILURE_AT),
            )
        elif changed == "direct_tool":
            await db.execute(
                """INSERT INTO tool_calls(id,task_id,tool_name,arguments_json,summary,risk,
                status,created_at,updated_at) VALUES('direct_tool',?,'file.read','{}','Read','low',
                'failed',?,?)""",
                (root_id, FAILURE_AT, FAILURE_AT),
            )
        await db.commit()
    tables = (*PROTECTED_TABLES, "tasks", "audit_events", "outbox_events")
    before = await _rows(manager.db_path, tables)
    await StateService(manager.db_path).initialize()
    assert await _rows(manager.db_path, tables) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_status", ["completed", "failed", "cancelled", "budget_exhausted"])
async def test_terminal_goal_does_not_exempt_real_orphaned_task(
    tmp_path: Path, goal_status: str
) -> None:
    manager, goal_id, root_id = await _waiting_goal(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE goal_runs SET status=? WHERE id=?", (goal_status, goal_id))
        await db.commit()
    await StateService(manager.db_path).initialize()
    task = next(
        row for row in (await _rows(manager.db_path, ("tasks",)))["tasks"] if row["id"] == root_id
    )
    assert task["status"] == "failed"
    assert json.loads(task["error_json"]) == {"message": ORPHAN_REASON}
