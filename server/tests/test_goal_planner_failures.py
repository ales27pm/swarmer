from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.services.goal_manager import GoalManagerConflict
from app.services.planner_provider import PlannerFailureCategory, SwarmPlannerProviderError
from app.services.swarm_contracts import (
    GoalCreateRequest,
    GoalReplanRequest,
    GoalStartRequest,
    PlannerSource,
    SwarmPlanProposal,
)
from tests.test_goal_runtime_recovery import _manager, _worker_plan


class _FailingPlanner:
    source = PlannerSource.TEST

    def __init__(self, category: PlannerFailureCategory) -> None:
        self.category = category
        self.calls = 0
        self.available = False

    async def propose(self, context: Mapping[str, object]) -> SwarmPlanProposal:
        del context
        self.calls += 1
        if not self.available:
            raise SwarmPlannerProviderError(
                "private-provider-response-do-not-persist", category=self.category
            )
        return _worker_plan()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "phase", "reason"),
    [
        ("transport_unavailable", "planner_unavailable", "Planner transport is unavailable."),
        (
            "request_rejected",
            "planner_request_rejected",
            "The model provider rejected the planner request.",
        ),
        (
            "invalid_response",
            "planner_invalid_response",
            "The planner response did not pass server validation.",
        ),
        (
            "invalid_context",
            "planner_invalid_context",
            "The planner context could not be prepared.",
        ),
    ],
)
async def test_planner_failures_are_distinct_safe_and_retry_after_cooldown(
    tmp_path: Path, category: PlannerFailureCategory, phase: str, reason: str
) -> None:
    manager = await _manager(tmp_path / "failure.db", _worker_plan())
    planner = _FailingPlanner(category)
    manager.planner = planner
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository"), actor_id="test-phone"
    )

    with pytest.raises(GoalManagerConflict) as failure:
        await manager.start_goal(str(goal["id"]), GoalStartRequest())

    assert "private-provider-response" not in str(failure.value)
    failed = await manager.graph.get_goal(str(goal["id"]))
    assert failed is not None
    assert failed["status"] == "planning"
    assert failed["current_phase"] == phase
    assert failed["failure_reason"] == reason
    assert failed["model_call_count"] == planner.calls == 1
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT status,error_category FROM goal_model_calls")
        ).fetchall() == [("failed", category)]
    assert await manager.graph.list_nodes(str(goal["id"])) == []
    for _ in range(3):
        assert await manager.reconcile() == 0
    assert planner.calls == 1

    planner.available = True
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET updated_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=61)).isoformat(), goal["id"]),
        )
        await db.commit()

    assert await manager.reconcile() >= 1
    recovered = await manager.graph.get_goal(str(goal["id"]))
    assert recovered is not None
    assert recovered["status"] == "running"
    assert recovered["model_call_count"] == planner.calls == 2
    assert recovered["started_at"] == failed["started_at"]
    assert recovered["max_model_calls"] == failed["max_model_calls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_part", ["objective", "dependency", "skill", "step_budget"])
async def test_semantically_invalid_initial_plan_is_cooled_without_persisting_nodes(
    tmp_path: Path, invalid_part: str
) -> None:
    plan = _worker_plan(include_review=invalid_part == "step_budget")
    if invalid_part == "objective":
        plan = plan.model_copy(update={"objective": "goal_stale"})
    elif invalid_part == "dependency":
        plan.nodes[0] = plan.nodes[0].model_copy(update={"dependencies": ["strategy_failures_0"]})
    elif invalid_part == "skill":
        plan.nodes[0] = plan.nodes[0].model_copy(update={"required_skill": "invented.skill"})
    manager = await _manager(tmp_path / "semantic.db", plan)
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository", max_steps=1), actor_id="test-phone"
    )

    with pytest.raises(GoalManagerConflict):
        await manager.start_goal(str(goal["id"]), GoalStartRequest())

    record = await manager.graph.get_goal(str(goal["id"]))
    assert record is not None
    assert record["current_phase"] == "planner_invalid_response"
    assert record["failure_reason"] == "The planner response did not pass server validation."
    assert record["model_call_count"] == 1
    assert await manager.graph.list_nodes(str(goal["id"])) == []
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT status,error_category FROM goal_model_calls")
        ).fetchall() == [("failed", "invalid_response")]
    assert await manager.reconcile() == 0
    after = await manager.graph.get_goal(str(goal["id"]))
    assert after is not None and after["model_call_count"] == 1


@pytest.mark.asyncio
async def test_rejected_model_replan_records_invalid_response_without_changing_graph(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "replan.db", _worker_plan(objective="goal_stale"))
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository"), actor_id="test-phone"
    )
    started = await manager._mark_start_requested(str(goal["id"]))
    await manager._persist_initial_plan(
        started, _worker_plan(), source=PlannerSource.MANUAL, model_call_id=None
    )
    original_nodes = await manager.graph.list_nodes(str(goal["id"]))

    with pytest.raises(GoalManagerConflict, match="authoritative goal objective"):
        await manager.replan_goal(str(goal["id"]), GoalReplanRequest())

    record = await manager.graph.get_goal(str(goal["id"]))
    assert record is not None
    assert record["current_phase"] == "planner_invalid_response"
    assert record["replan_count"] == 0
    assert record["model_call_count"] == 1
    assert await manager.graph.list_nodes(str(goal["id"])) == original_nodes
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT status,error_category FROM goal_model_calls")
        ).fetchall() == [("failed", "invalid_response")]


@pytest.mark.asyncio
async def test_late_planner_failure_cannot_overwrite_newer_success(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class DelayedPlanner:
        source = PlannerSource.TEST

        async def propose(self, context: Mapping[str, object]) -> SwarmPlanProposal:
            del context
            entered.set()
            await release.wait()
            raise SwarmPlannerProviderError("late transport failure")

    database = tmp_path / "late-failure.db"
    first = await _manager(database, _worker_plan())
    second = await _manager(database, _worker_plan())
    first.planner = DelayedPlanner()
    goal = await first.create_goal(
        GoalCreateRequest(objective="Inspect the repository"), actor_id="test-phone"
    )
    pending = asyncio.create_task(first.start_goal(str(goal["id"]), GoalStartRequest()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with aiosqlite.connect(database) as db:
            await db.execute(
                "UPDATE goal_model_calls SET lease_expires_at=? WHERE goal_run_id=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), goal["id"]),
            )
            await db.commit()
        succeeded = await second.start_goal(str(goal["id"]), GoalStartRequest())
        assert succeeded["goal"]["status"] == "running"
    finally:
        release.set()
    with pytest.raises(GoalManagerConflict, match="planner unavailable"):
        await asyncio.wait_for(pending, timeout=5)

    current = await second.get_goal(str(goal["id"]))
    assert current == succeeded
    async with aiosqlite.connect(database) as db:
        assert await (
            await db.execute(
                "SELECT status,error_category FROM goal_model_calls ORDER BY lease_generation"
            )
        ).fetchall() == [("failed", "lease_expired"), ("completed", None)]
