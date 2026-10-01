from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatcher
from app.services.evaluator_provider import NoopEvaluatorProvider
from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.message_board import SQLiteMessageBoard
from app.services.permission_policy import PermissionPolicy
from app.services.state_service import StateService
from app.services.swarm_contracts import (
    AutonomyProfile,
    GoalCreateRequest,
    GoalReplanRequest,
    GoalStartRequest,
    PlannerSource,
    SwarmPlanProposal,
)

OBJECTIVE = "Inspect local model admission"
REPO_ROOT = Path(__file__).resolve().parents[2]


class LocalPlanner:
    source = PlannerSource.UBUNTU_LOCAL
    model = "test-local-model"

    def __init__(self, *, block: bool = False) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()
        self.contexts: list[dict[str, object]] = []

    async def propose(self, context: Mapping[str, object]) -> SwarmPlanProposal:
        self.contexts.append(dict(context))
        self.entered.set()
        await self.release.wait()
        return SwarmPlanProposal.model_validate(
            {
                "schema_version": "1.0",
                "objective": OBJECTIVE,
                "rationale_summary": "Inspect the workspace without a model worker.",
                "completion_criteria": ["Directory entries are known"],
                "max_parallelism": 1,
                "nodes": [
                    {
                        "temporary_id": "files",
                        "node_type": "worker",
                        "title": "Read workspace",
                        "objective": "List the workspace root",
                        "required_skill": "workspace.list_dir",
                        "dependencies": [],
                        "expected_output": "Directory entries",
                        "priority": 0,
                    }
                ],
            }
        )


async def manager(tmp_path: Path, planner: LocalPlanner) -> GoalManager:
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs/permissions.yaml")
    state = StateService(tmp_path / "state.db", permission_policy=policy)
    await state.initialize()
    return GoalManager(
        state.db_path,
        state_service=state,
        agent_dispatcher=AgentDispatcher(
            state.db_path, SQLiteMessageBoard(state.db_path), permission_policy=policy
        ),
        planner=planner,
        evaluator=NoopEvaluatorProvider(),
        permission_policy=policy,
    )


async def goal(value: GoalManager) -> dict[str, Any]:
    return await value.create_goal(
        GoalCreateRequest(objective=OBJECTIVE, autonomy_profile=AutonomyProfile.AUTONOMOUS),
        actor_id="test-device",
    )


async def reserve(value: GoalManager, goal_id: str) -> str:
    return await value._reserve_model_call(
        goal_id,
        role="planner",
        context_id=None,
        input_digest="a" * 64,
        provider_source=PlannerSource.UBUNTU_LOCAL.value,
    )


async def stored_goal(value: GoalManager, goal_id: str) -> dict[str, Any]:
    async with aiosqlite.connect(value.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_id,))).fetchone()
    assert row is not None
    return dict(row)


async def worker(value: GoalManager, skills: list[str]) -> str:
    agent = await value.state_service.register_agent(
        AgentCreate(name="resource-test", endpoint="https://worker.invalid", skills=skills),
        "test-device",
    )
    assert await value.state_service.heartbeat_agent(agent["id"], "online", agent["credential"])
    return str(agent["id"])


async def queued(value: GoalManager, skill: str, payload: dict[str, Any]) -> dict[str, Any]:
    task = await value.state_service.create_task(
        TaskRecord.new(TaskCreate(input="resource test"), source="test-device")
    )
    return await value.agent_dispatcher.queue_job(task.id, skill, payload)


async def test_concurrent_starts_return_pending_then_reconcile_after_resource_release(
    tmp_path: Path,
) -> None:
    planner = LocalPlanner(block=True)
    first = await manager(tmp_path, planner)
    a, b = await goal(first), await goal(first)
    running = asyncio.create_task(first.start_goal(a["id"], GoalStartRequest()))
    try:
        await asyncio.wait_for(planner.entered.wait(), timeout=2)
        second = await manager(tmp_path, LocalPlanner())
        pending = await second.start_goal(b["id"], GoalStartRequest())
        assert pending["goal"]["status"] == "planning"
        assert pending["goal"]["model_call_count"] == 0
        assert pending["goal"]["failure_reason"] is None
        assert pending["goal"]["started_at"] is not None
        assert isinstance(second.planner, LocalPlanner) and second.planner.contexts == []
    finally:
        planner.release.set()
        await running
    restarted = await manager(tmp_path, LocalPlanner())
    await restarted.reconcile()
    after = await restarted.get_goal(b["id"])
    assert after is not None and after["goal"]["status"] == "running"
    assert after["goal"]["model_call_count"] == 1
    assert after["goal"]["failure_reason"] is None


@pytest.mark.parametrize(
    "parallel_skill,parallel_payload",
    [
        ("research.query", {"query": "SQLite", "max_results": 2}),
        ("workspace.list_dir", {"path": "."}),
    ],
)
async def test_busy_model_skips_generation_without_blocking_research_or_files(
    tmp_path: Path, parallel_skill: str, parallel_payload: dict[str, Any]
) -> None:
    value = await manager(tmp_path, LocalPlanner())
    model_goal = await goal(value)
    active_call = await reserve(value, model_goal["id"])
    heavy = await queued(
        value,
        "writing.draft",
        {"schema_version": "1.0", "objective": "Write a note", "conversation": []},
    )
    parallel = await queued(value, parallel_skill, parallel_payload)
    agent_id = await worker(value, [parallel_skill])
    claimed = await value.agent_dispatcher.claim(agent_id)
    assert claimed is not None and claimed["id"] == parallel["id"]
    waiting = await value.agent_dispatcher.get_job(heavy["id"])
    assert waiting is not None and waiting["status"] == "queued"
    assert waiting["attempt_count"] == 0 and waiting["lease_id"] is None
    # A different eligible writer acquires it after the API reservation ends.
    writer_id = await worker(value, ["writing.draft"])
    assert await value.agent_dispatcher.claim(writer_id) is None
    assert await value._finish_model_call(active_call, status="completed")
    admitted = await value.agent_dispatcher.claim(writer_id)
    assert admitted is not None and admitted["id"] == heavy["id"]
    assert admitted["attempt_count"] == 1


async def test_worker_lease_defers_api_without_spending_budget(tmp_path: Path) -> None:
    value = await manager(tmp_path, LocalPlanner())
    await queued(value, "code.generate_python", {"objective": "Write a function"})
    agent_id = await worker(value, ["code.generate_python"])
    claimed = await value.agent_dispatcher.claim(agent_id)
    assert claimed is not None
    target = await goal(value)
    pending = await value.start_goal(target["id"], GoalStartRequest())
    assert pending["goal"]["status"] == "planning"
    assert pending["goal"]["model_call_count"] == 0
    await value.agent_dispatcher.submit_result(
        agent_id,
        claimed["id"],
        claimed["claim_token"],
        status="failed",
        result=None,
        error="test worker stopped",
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
    )
    admitted = await value.start_goal(target["id"], GoalStartRequest())
    assert admitted["goal"]["status"] == "running"
    assert admitted["goal"]["model_call_count"] == 1


@pytest.mark.parametrize("reason", ["Inspect the saved files again", None])
async def test_busy_replan_is_durable_idempotent_and_resumes_after_restart(
    tmp_path: Path, reason: str | None
) -> None:
    value = await manager(tmp_path, LocalPlanner())
    target = await goal(value)
    # A quiescent running goal has no unfinished worker to interrupt.
    async with aiosqlite.connect(value.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET status='running',started_at=? WHERE id=?",
            (datetime.now(UTC).isoformat(), target["id"]),
        )
        await db.commit()
    other = await goal(value)
    active_call = await reserve(value, other["id"])
    request = GoalReplanRequest(reason=reason)
    first = await value.replan_goal(target["id"], request)
    first_state = await stored_goal(value, target["id"])
    second = await value.replan_goal(target["id"], request)
    second_state = await stored_goal(value, target["id"])
    assert first["goal"]["status"] == second["goal"]["status"] == "running"
    assert first_state["conversation_revision"] == second_state["conversation_revision"] == 1
    assert second_state["pending_message_revision"] == 1
    assert second_state["reply_dispatch_credit"] == 1
    assert second_state["model_call_count"] == second_state["replan_count"] == 0
    async with aiosqlite.connect(value.db_path) as db:
        db.row_factory = aiosqlite.Row
        deferred = await (
            await db.execute(
                "SELECT content FROM goal_messages WHERE goal_run_id=? AND actor_id='goal-replan'",
                (target["id"],),
            )
        ).fetchall()
    assert len(deferred) == 1
    if reason is not None:
        assert deferred[0]["content"] == reason
    else:
        assert deferred[0]["content"].startswith("[Server-recorded replan request]")
    assert await value._finish_model_call(active_call, status="completed")
    resumed_planner = LocalPlanner()
    restarted = await manager(tmp_path, resumed_planner)
    await restarted.reconcile()
    after = await stored_goal(restarted, target["id"])
    assert after["pending_message_revision"] == 0
    assert after["model_call_count"] == after["replan_count"] == 1
    assert len(resumed_planner.contexts) == 1
    assert deferred[0]["content"] in json.dumps(resumed_planner.contexts, ensure_ascii=False)


async def test_deferred_replan_rejects_terminal_state_without_creating_continuation(
    tmp_path: Path,
) -> None:
    value = await manager(tmp_path, LocalPlanner())
    target = await goal(value)
    async with aiosqlite.connect(value.db_path) as db:
        await db.execute("UPDATE goal_runs SET status='completed' WHERE id=?", (target["id"],))
        await db.commit()
    with pytest.raises(GoalManagerConflict, match="changed before deferred replan"):
        await value._defer_replan_for_resource(target, GoalReplanRequest(reason="Try again"))
    async with aiosqlite.connect(value.db_path) as db:
        count = await (await db.execute("SELECT count(*) FROM goal_runs")).fetchone()
    assert count == (1,)


async def test_model_reservation_clock_is_sampled_after_waiting_for_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await manager(tmp_path, LocalPlanner())
    target = await goal(value)
    current = [datetime.now(UTC)]

    class Clock:
        @staticmethod
        def now(tz: object) -> datetime:
            return current[0]

    monkeypatch.setattr("app.services.goal_manager.datetime", Clock)
    async with aiosqlite.connect(value.db_path) as blocker:
        await blocker.execute("BEGIN IMMEDIATE")
        reservation = asyncio.create_task(reserve(value, target["id"]))
        await asyncio.sleep(0.05)
        assert not reservation.done()
        current[0] += timedelta(seconds=value.model_call_lease_seconds + 1)
        await blocker.commit()
    call = await asyncio.wait_for(reservation, timeout=2)
    async with aiosqlite.connect(value.db_path) as db:
        row = await (
            await db.execute(
                "SELECT created_at,lease_expires_at FROM goal_model_calls WHERE id=?", (call,)
            )
        ).fetchone()
    assert row == (
        current[0].isoformat(),
        (current[0] + timedelta(seconds=value.model_call_lease_seconds)).isoformat(),
    )
