from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from app.services.agent_dispatcher import AgentDispatcher
from app.services.evaluator_provider import NoopEvaluatorProvider
from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.message_board import SQLiteMessageBoard
from app.services.permission_policy import PermissionPolicy
from app.services.planner_provider import NoopSwarmPlannerProvider
from app.services.state_service import StateService
from app.services.swarm_contracts import (
    EvaluationDecision,
    EvaluationStatus,
    GoalCreateRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


async def _managers(tmp_path: Path) -> tuple[GoalManager, GoalManager]:
    db_path = tmp_path / "state.db"
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    state = StateService(db_path, permission_policy=policy)
    await state.initialize()

    def build(instance_id: str) -> GoalManager:
        return GoalManager(
            db_path,
            state_service=state,
            agent_dispatcher=AgentDispatcher(
                db_path,
                SQLiteMessageBoard(db_path),
                permission_policy=policy,
            ),
            planner=NoopSwarmPlannerProvider(),
            evaluator=NoopEvaluatorProvider(),
            permission_policy=policy,
            instance_id=instance_id,
            model_call_lease_seconds=30,
        )

    return build("control-plane-a"), build("control-plane-b")


async def _create_goal(manager: GoalManager, *, max_model_calls: int = 10) -> dict[str, Any]:
    return await manager.create_goal(
        GoalCreateRequest(
            objective="Inspect model-call fencing",
            max_model_calls=max_model_calls,
        ),
        actor_id="test-phone",
    )


async def _reserve(manager: GoalManager, goal_run_id: str) -> str:
    return await manager._reserve_model_call(
        goal_run_id,
        role="evaluator",
        context_id=None,
        input_digest="a" * 64,
        provider_source="test",
    )


@pytest.mark.asyncio
async def test_only_one_active_model_call_is_allowed_per_goal(tmp_path: Path) -> None:
    manager_a, manager_b = await _managers(tmp_path)
    goal = await _create_goal(manager_a)

    call_id = await _reserve(manager_a, str(goal["id"]))

    with pytest.raises(GoalManagerConflict, match="already has a model call"):
        await _reserve(manager_b, str(goal["id"]))

    async with aiosqlite.connect(manager_a.db_path) as db:
        active = await (
            await db.execute(
                """SELECT id,owner_instance_id,lease_generation
                FROM goal_model_calls WHERE goal_run_id=? AND status='started'""",
                (goal["id"],),
            )
        ).fetchall()
        count = await (
            await db.execute(
                "SELECT model_call_count FROM goal_runs WHERE id=?",
                (goal["id"],),
            )
        ).fetchone()

    assert active == [(call_id, "control-plane-a", 1)]
    assert count == (1,)


@pytest.mark.asyncio
async def test_expired_model_call_is_reclaimed_with_generation_fencing(
    tmp_path: Path,
) -> None:
    manager_a, manager_b = await _managers(tmp_path)
    goal = await _create_goal(manager_a)
    first_call = await _reserve(manager_a, str(goal["id"]))
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    async with aiosqlite.connect(manager_a.db_path) as db:
        await db.execute(
            "UPDATE goal_model_calls SET lease_expires_at=? WHERE id=?",
            (expired_at, first_call),
        )
        await db.commit()

    second_call = await _reserve(manager_b, str(goal["id"]))

    async with aiosqlite.connect(manager_a.db_path) as db:
        rows = await (
            await db.execute(
                """SELECT id,status,error_category,owner_instance_id,lease_generation
                FROM goal_model_calls WHERE goal_run_id=? ORDER BY lease_generation""",
                (goal["id"],),
            )
        ).fetchall()
        count = await (
            await db.execute(
                "SELECT model_call_count FROM goal_runs WHERE id=?",
                (goal["id"],),
            )
        ).fetchone()

    assert rows == [
        (first_call, "failed", "lease_expired", "control-plane-a", 1),
        (second_call, "started", None, "control-plane-b", 2),
    ]
    assert count == (2,)


@pytest.mark.asyncio
async def test_stale_model_call_owner_cannot_finish_or_apply_evaluation(
    tmp_path: Path,
) -> None:
    manager_a, manager_b = await _managers(tmp_path)
    goal = await _create_goal(manager_a)
    first_call = await _reserve(manager_a, str(goal["id"]))
    async with aiosqlite.connect(manager_a.db_path) as db:
        await db.execute(
            "UPDATE goal_model_calls SET lease_expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), first_call),
        )
        await db.commit()
    second_call = await _reserve(manager_b, str(goal["id"]))

    assert (
        await manager_a._finish_model_call(
            first_call,
            status="completed",
        )
        is False
    )

    current_goal = await manager_a.graph.get_goal(str(goal["id"]))
    assert current_goal is not None
    decision = EvaluationDecision(
        schema_version="1.0",
        status=EvaluationStatus.CONTINUE,
        reason_summary="Continue after reviewing evidence.",
        missing_requirements=[],
        invalid_results=[],
        suggested_new_nodes=[],
    )
    with pytest.raises(GoalManagerConflict, match="expired or was fenced"):
        await manager_a._apply_evaluation(
            current_goal,
            [],
            decision,
            decision_fingerprint="decision-a",
            state_fingerprint="state-a",
            model_call_id=first_call,
        )

    async with aiosqlite.connect(manager_a.db_path) as db:
        evaluation_count = await (
            await db.execute(
                "SELECT COUNT(*) FROM goal_evaluations WHERE goal_run_id=?",
                (goal["id"],),
            )
        ).fetchone()
        current_call = await (
            await db.execute(
                "SELECT status,owner_instance_id,lease_generation FROM goal_model_calls WHERE id=?",
                (second_call,),
            )
        ).fetchone()

    assert evaluation_count == (0,)
    assert current_call == ("started", "control-plane-b", 2)
    assert (
        await manager_b._finish_model_call(
            second_call,
            status="completed",
        )
        is True
    )


@pytest.mark.asyncio
async def test_competing_evaluator_call_does_not_exhaust_goal_budget(tmp_path: Path) -> None:
    manager_a, manager_b = await _managers(tmp_path)
    goal = await _create_goal(manager_a, max_model_calls=2)
    now = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(manager_a.db_path) as db:
        await db.execute(
            """UPDATE goal_runs SET status='running',started_at=?,current_phase='evaluating',
            updated_at=? WHERE id=?""",
            (now, now, goal["id"]),
        )
        await db.execute(
            "UPDATE tasks SET status='running',updated_at=? WHERE id=?",
            (now, goal["root_task_id"]),
        )
        await db.execute(
            """INSERT INTO plan_nodes(
                id,goal_run_id,node_type,title,objective,required_skill,status,priority,
                depends_on_json,expected_output,planner_metadata_json,result_summary,
                created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "node_completed",
                goal["id"],
                "worker",
                "Inspect",
                "Inspect the repository",
                "workspace.list_dir",
                "completed",
                0,
                "[]",
                "Repository evidence",
                "{}",
                "Verified worker evidence",
                now,
                now,
                now,
            ),
        )
        await db.commit()

    active_call = await _reserve(manager_a, str(goal["id"]))
    await manager_b._evaluate_if_quiescent(str(goal["id"]))

    async with aiosqlite.connect(manager_a.db_path) as db:
        durable_goal = await (
            await db.execute(
                """SELECT status,current_phase,model_call_count,failure_reason
                FROM goal_runs WHERE id=?""",
                (goal["id"],),
            )
        ).fetchone()
        evaluations = await (
            await db.execute(
                "SELECT COUNT(*) FROM goal_evaluations WHERE goal_run_id=?",
                (goal["id"],),
            )
        ).fetchone()
        active = await (
            await db.execute(
                "SELECT id,owner_instance_id,status FROM goal_model_calls WHERE status='started'",
            )
        ).fetchall()

    assert durable_goal == ("running", "evaluator_model", 1, None)
    assert evaluations == (0,)
    assert active == [(active_call, "control-plane-a", "started")]


@pytest.mark.asyncio
async def test_evaluator_decision_and_terminal_state_commit_before_projection(
    tmp_path: Path,
) -> None:
    manager, _ = await _managers(tmp_path)
    goal = await _create_goal(manager)
    now = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            """UPDATE goal_runs SET status='running',started_at=?,updated_at=?
            WHERE id=?""",
            (now, now, goal["id"]),
        )
        await db.execute(
            "UPDATE tasks SET status='running',updated_at=? WHERE id=?",
            (now, goal["root_task_id"]),
        )
        await db.execute(
            """INSERT INTO plan_nodes(
                id,goal_run_id,node_type,title,objective,required_skill,status,priority,
                depends_on_json,expected_output,planner_metadata_json,result_summary,
                created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "node_evidence",
                goal["id"],
                "worker",
                "Inspect",
                "Inspect the repository",
                "workspace.list_dir",
                "completed",
                0,
                "[]",
                "Repository evidence",
                "{}",
                "README.md",
                now,
                now,
                now,
            ),
        )
        await db.commit()
    call_id = await _reserve(manager, str(goal["id"]))
    current_goal = await manager.graph.get_goal(str(goal["id"]))
    nodes = await manager.graph.list_nodes(str(goal["id"]))
    assert current_goal is not None
    decision = EvaluationDecision(
        schema_version="1.0",
        status=EvaluationStatus.DONE,
        reason_summary="Server-observed evidence satisfies the goal.",
        missing_requirements=[],
        invalid_results=[],
        suggested_new_nodes=[],
    )

    with (
        patch.object(
            manager,
            "_finalize_terminal_goal",
            AsyncMock(side_effect=RuntimeError("simulated projection crash")),
        ),
        pytest.raises(RuntimeError, match="projection crash"),
    ):
        await manager._apply_evaluation(
            current_goal,
            nodes,
            decision,
            decision_fingerprint="d" * 64,
            state_fingerprint=manager._state_fingerprint(nodes),
            model_call_id=call_id,
        )

    async with aiosqlite.connect(manager.db_path) as db:
        durable_goal = await (
            await db.execute(
                "SELECT status,evaluation_fingerprint FROM goal_runs WHERE id=?",
                (goal["id"],),
            )
        ).fetchone()
        root_task = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (goal["root_task_id"],))
        ).fetchone()
        model_call = await (
            await db.execute(
                "SELECT status,output_digest FROM goal_model_calls WHERE id=?",
                (call_id,),
            )
        ).fetchone()
        evaluations = await (
            await db.execute(
                "SELECT COUNT(*) FROM goal_evaluations WHERE goal_run_id=?",
                (goal["id"],),
            )
        ).fetchone()

    assert durable_goal == ("completed", "d" * 64)
    assert root_task == ("completed",)
    assert model_call is not None and model_call[0] == "completed"
    assert isinstance(model_call[1], str) and len(model_call[1]) == 64
    assert evaluations == (1,)
