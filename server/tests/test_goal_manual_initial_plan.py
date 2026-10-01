from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from app.models import AgentCreate
from app.services.execution_engine import ExecutionEngine
from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.goal_project import GoalProjectService
from app.services.project_context import ProjectContextService
from app.services.swarm_contracts import (
    GoalCreateRequest,
    GoalMessageRequest,
    GoalStartRequest,
    PlannerSource,
    SwarmPlanProposal,
)
from tests.test_goal_runtime_recovery import _manager, _worker_plan

OBJECTIVE = "Build a local task application and preserve its existing HTML."
REPLY = "Keep index.html unchanged; pin Flask==3.1.3 and run the real browser test."


async def _pending_initial(
    tmp_path: Path, *, project: bool = False, max_steps: int = 6
) -> tuple[GoalManager, str, SwarmPlanProposal]:
    plan = _worker_plan(objective=OBJECTIVE)
    if project:
        plan.nodes[0] = plan.nodes[0].model_copy(
            update={
                "required_skill": "code.build_project",
                "objective": REPLY,
                "expected_output": "A tested application with unchanged HTML",
            }
        )
    manager = await _manager(tmp_path / "manual.db", plan)
    manager.require_execution_workers = True
    if project:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        manager.project_applications = GoalProjectService(
            manager.db_path, ExecutionEngine(manager.db_path, workspace, manager.permission_policy)
        )
        manager.project_applications.context = ProjectContextService(manager.db_path)
    registration = await manager.state_service.register_agent(
        AgentCreate(
            name="Qualification worker",
            endpoint="https://worker.invalid",
            skills=[str(plan.nodes[0].required_skill)],
        ),
        "phone",
    )
    await manager.state_service.heartbeat_agent(
        registration["id"], "online", registration["credential"]
    )
    parent = await manager.create_goal(
        GoalCreateRequest(
            objective=OBJECTIVE,
            completion_criteria=["Existing HTML is preserved", "Real tests pass"],
            max_steps=max_steps,
            max_model_calls=10,
        ),
        actor_id="phone",
    )
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE goal_runs SET status='cancelled' WHERE id=?", (parent["id"],))
        await db.commit()
    reply = await manager.reply_goal(
        str(parent["id"]),
        GoalMessageRequest(message=REPLY, client_message_id="targeted-repair"),
        actor_id="phone",
    )
    goal_id = str(reply["goal"]["id"])
    assert goal_id != parent["id"]
    current = await manager.graph.get_goal(goal_id)
    assert current is not None
    assert current["status"] == "planning" and current["current_phase"] == "continuation_pending"
    assert current["conversation_revision"] == current["pending_message_revision"] == 1
    assert await manager.graph.list_nodes(goal_id) == []
    return manager, goal_id, plan


def _forbid_automatic_planning(manager: GoalManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        manager.planner,
        "propose",
        AsyncMock(side_effect=AssertionError("automatic planner invoked")),
    )
    monkeypatch.setattr(
        manager,
        "_shared_project_memory",
        AsyncMock(side_effect=AssertionError("automatic planning memory invoked")),
    )


async def _assert_unplanned(manager: GoalManager, goal_id: str, *, revision: int = 1) -> None:
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None and goal["status"] == "planning"
    assert goal["conversation_revision"] == goal["pending_message_revision"] == revision
    assert goal["model_call_count"] == goal["step_count"] == goal["replan_count"] == 0
    assert await manager.graph.list_nodes(goal_id) == []
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone() == (0,)
        assert await (await db.execute("SELECT COUNT(*) FROM goal_model_calls")).fetchone() == (0,)


@pytest.mark.asyncio
async def test_pending_manual_initial_plan_needs_no_planner_memory_or_model_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, goal_id, plan = await _pending_initial(tmp_path)
    _forbid_automatic_planning(manager, monkeypatch)

    detail = await manager.start_goal(
        goal_id, GoalStartRequest(plan_proposal=plan, planner_source="manual")
    )

    assert detail["nodes"][0]["status"] == "dispatched"
    current = await manager.graph.get_goal(goal_id)
    assert current is not None and current["pending_message_revision"] == 0
    assert current["conversation_revision"] == 1
    assert current["model_call_count"] == current["replan_count"] == 0
    assert current["step_count"] == 1
    assert (await manager.graph.list_nodes(goal_id))[0]["conversation_revision"] == 1


@pytest.mark.asyncio
async def test_manual_continuation_delivers_original_and_latest_requirements_to_project_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, goal_id, plan = await _pending_initial(tmp_path, project=True)
    _forbid_automatic_planning(manager, monkeypatch)

    detail = await manager.start_goal(
        goal_id, GoalStartRequest(plan_proposal=plan, planner_source="manual")
    )

    node = detail["nodes"][0]
    assert node["status"] == "dispatched"
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                "SELECT payload_json FROM agent_jobs WHERE id=?", (node["worker_job_id"],)
            )
        ).fetchone()
        assert await (await db.execute("SELECT COUNT(*) FROM goal_model_calls")).fetchone() == (0,)
    assert row is not None
    payload = json.loads(row[0])
    assert payload["objective"] == REPLY
    assert payload["conversation"][-1] == {"role": "user", "content": REPLY}
    requirements = payload["durable_context"]["requirements"]
    assert any(item["text"] == OBJECTIVE and item["source_id"] for item in requirements)
    assert any(item["text"] == REPLY and item["source_id"] for item in requirements)
    current = await manager.graph.get_goal(goal_id)
    assert current is not None
    assert current["model_call_count"] == 1  # The project job alone; no planner or embedding.
    assert current["step_count"] == 1 and current["replan_count"] == 0
    assert current["pending_message_revision"] == 0


@pytest.mark.asyncio
async def test_pending_initial_without_explicit_plan_keeps_automatic_memory_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, goal_id, _ = await _pending_initial(tmp_path)
    memory = AsyncMock(side_effect=GoalManagerConflict("durable planner context exceeds budget"))
    monkeypatch.setattr(manager, "_shared_project_memory", memory)

    with pytest.raises(GoalManagerConflict, match="durable planner context exceeds budget"):
        await manager.start_goal(goal_id, GoalStartRequest())

    memory.assert_awaited_once()
    assert memory.await_args is not None and memory.await_args.args[1] == "planner"
    await _assert_unplanned(manager, goal_id)


@pytest.mark.asyncio
async def test_manual_request_does_not_skip_pending_routing_when_nodes_already_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, goal_id, plan = await _pending_initial(tmp_path)
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None
    await manager._persist_initial_plan(goal, plan, source=PlannerSource.MANUAL, model_call_id=None)
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(message="Also preserve the title", client_message_id="later-guidance"),
        actor_id="phone",
    )
    previous = await manager.graph.list_nodes(goal_id)
    resume = AsyncMock(side_effect=GoalManagerConflict("existing pending routing reached"))
    monkeypatch.setattr(manager, "_resume_pending_conversation", resume)

    with pytest.raises(GoalManagerConflict, match="existing pending routing reached"):
        await manager.start_goal(
            goal_id, GoalStartRequest(plan_proposal=plan, planner_source="manual")
        )

    resume.assert_awaited_once()
    assert await manager.graph.list_nodes(goal_id) == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["objective", "step_budget", "capabilities"])
async def test_manual_pending_plan_preserves_server_validation_and_pending_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    manager, goal_id, plan = await _pending_initial(tmp_path, max_steps=1)
    _forbid_automatic_planning(manager, monkeypatch)
    if invalid == "objective":
        plan = plan.model_copy(update={"objective": "Replace the application"})
        expected = "authoritative goal objective"
    elif invalid == "step_budget":
        plan = _worker_plan(objective=OBJECTIVE, include_review=True)
        expected = "step budget"
    else:
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute("UPDATE agents SET status='offline'")
            await db.commit()
        expected = "available capabilities"

    with pytest.raises(GoalManagerConflict, match=expected):
        await manager.start_goal(
            goal_id, GoalStartRequest(plan_proposal=plan, planner_source="manual")
        )

    await _assert_unplanned(manager, goal_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["obtain", "source_binding"])
async def test_concurrent_reply_fences_manual_plan_before_insertion_without_losing_new_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: str
) -> None:
    manager, goal_id, plan = await _pending_initial(tmp_path)
    _forbid_automatic_planning(manager, monkeypatch)
    seam = "_obtain_plan" if window == "obtain" else "_bind_research_source_requirements"
    original = getattr(manager, seam)

    async def reply_during_validation(*args: Any, **kwargs: Any) -> Any:
        value = await original(*args, **kwargs)
        await manager.reply_goal(
            goal_id,
            GoalMessageRequest(
                message="Preserve the new constraint", client_message_id="race-reply"
            ),
            actor_id="phone",
        )
        return value

    monkeypatch.setattr(manager, seam, reply_during_validation)
    with pytest.raises(GoalManagerConflict, match="conversation changed"):
        await manager.start_goal(
            goal_id, GoalStartRequest(plan_proposal=plan, planner_source="manual")
        )

    await _assert_unplanned(manager, goal_id, revision=2)
    conversation = await manager.conversation_messages(goal_id)
    assert conversation["messages"][-1]["content"] == "Preserve the new constraint"
