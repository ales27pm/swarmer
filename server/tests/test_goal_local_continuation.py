from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.project_memory import ProjectMemoryService
from app.services.swarm_contracts import GoalCreateRequest, GoalMessageRequest, GoalStartRequest
from tests.test_goal_project_runtime import _project
from tests.test_goal_runtime_recovery import _manager, _worker_plan
from tests.test_project_memory import SemanticProvider


async def _terminal_project(tmp_path: Path) -> tuple[GoalManager, str, str]:
    manager, detail, agent = await _project(tmp_path, max_calls=30)
    parent = detail["goal"]["id"]
    await manager.reply_goal(
        parent,
        GoalMessageRequest(
            message="Customer records must keep shared identifiers and French labels.",
            client_message_id="historical-choice",
        ),
        actor_id="phone",
    )
    await manager.cancel_goal(parent, actor_id="phone")
    memory = ProjectMemoryService(manager.db_path, SemanticProvider(), model_revision="pinned")
    await memory.initialize()
    manager.project_memory = memory
    assert manager.project_applications is not None
    manager.project_applications.memory = memory
    return manager, parent, agent


def _request() -> GoalMessageRequest:
    return GoalMessageRequest(
        message="Add purchaser profiles and a calendar",
        client_message_id="local-next",
        planning_mode="iphone_local",
    )


async def _assert_waiting(manager: GoalManager, goal_id: str) -> None:
    detail = await manager.get_goal(goal_id)
    assert detail is not None
    assert detail["nodes"] == []
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None
    assert goal["status"] == "planning" and goal["current_phase"] == "awaiting_local_plan"
    assert goal["started_at"] is None and goal["planner_source"] == "iphone_local"
    assert goal["pending_message_revision"] == goal["reply_dispatch_credit"] == 0
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute(
                "SELECT COUNT(*) FROM goal_model_calls WHERE goal_run_id=?", (goal_id,)
            )
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_real_project_continuation_uses_shared_memory_only_after_explicit_start(
    tmp_path: Path,
) -> None:
    manager, parent, agent = await _terminal_project(tmp_path)
    old_history = await manager.conversation_messages(parent)
    assert old_history["project_id"]
    historical_id = old_history["messages"][-1]["id"]
    first, replay = await asyncio.gather(
        manager.reply_goal(parent, _request(), actor_id="phone"),
        manager.reply_goal(parent, _request(), actor_id="phone"),
    )
    child = first["goal"]["id"]
    assert child != parent and replay["goal"]["id"] == child
    history = await manager.conversation_messages(parent)
    assert history["active_goal_id"] == child
    assert history["project_id"] == old_history["project_id"]
    await _assert_waiting(manager, child)
    with pytest.raises(GoalManagerConflict, match="reviewed iPhone plan"):
        await manager.start_goal(child, GoalStartRequest())
    await _assert_waiting(manager, child)
    assert manager.project_memory is not None
    receipt = await manager.project_memory.retrieve_for_goal(child, "planner")
    assert receipt["local_planning_eligible"] is True and receipt["mode"] == "semantic"
    assert any(item["source_id"] == historical_id for item in receipt["items"])
    restarted = await _manager(manager.db_path, _worker_plan())
    assert await restarted.reconcile() == 0
    assert await restarted.reconcile() == 0
    await _assert_waiting(restarted, child)
    # Text edits stay durable and invalidate a reviewed plan without auto-starting work.
    await manager.reply_goal(
        child,
        GoalMessageRequest(message="Keep French labels", client_message_id="extra-instruction"),
        actor_id="phone",
    )
    await manager.reconcile()
    await _assert_waiting(manager, child)
    plan = _worker_plan(objective=first["goal"]["objective"])
    plan.nodes[0] = plan.nodes[0].model_copy(
        update={"required_skill": "code.build_project", "objective": "Add purchaser profiles"}
    )
    start = GoalStartRequest(
        planner_source="iphone_local",
        plan_proposal=plan,
        memory_context_fingerprint=receipt["context_fingerprint"],
    )
    with pytest.raises(GoalManagerConflict):
        await manager.start_goal(child, start)
    await _assert_waiting(manager, child)
    fresh = await manager.project_memory.retrieve_for_goal(child, "planner")
    started = await manager.start_goal(
        child, start.model_copy(update={"memory_context_fingerprint": fresh["context_fingerprint"]})
    )
    assert started["goal"]["started_at"] is not None
    assert started["nodes"][0]["status"] == "dispatched"
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None
    payload = job["payload"]
    assert (await manager.conversation_messages(child))["project_id"] == old_history["project_id"]
    assert any("French labels" in item["content"] for item in payload["conversation"])
    assert any(item["source_id"] == historical_id for item in payload["memory"]["items"])
    # Replay is bound to immutable acceptance intent, including after phase changes.
    assert (await manager.reply_goal(parent, _request(), actor_id="phone"))["goal"]["id"] == child
    with pytest.raises(GoalManagerConflict, match="different reply"):
        await manager.reply_goal(
            parent, _request().model_copy(update={"planning_mode": "automatic"}), actor_id="phone"
        )


async def _snapshot(manager: GoalManager) -> list[tuple[Any, ...]]:
    async with aiosqlite.connect(manager.db_path) as db:
        # Entire relevant state, not just counts, must remain unchanged on rejection.
        return [
            tuple(await (await db.execute("SELECT * FROM " + table + " ORDER BY rowid")).fetchall())
            for table in (
                "goal_runs",
                "goal_messages",
                "goal_conversations",
                "goal_conversation_links",
                "tasks",
                "audit_events",
                "coding_projects",
                "goal_project_links",
            )
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_local_continuation_without_project_rejects_without_mutation(
    tmp_path: Path, terminal: bool
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    goal = await manager.create_goal(GoalCreateRequest(objective="Inspect files"), actor_id="phone")
    if terminal:
        await manager.cancel_goal(goal["id"], actor_id="phone")
    before = await _snapshot(manager)
    with pytest.raises(GoalManagerConflict, match="existing project"):
        await manager.reply_goal(goal["id"], _request(), actor_id="phone")
    assert await _snapshot(manager) == before


@pytest.mark.asyncio
async def test_stale_local_action_cannot_redirect_active_automatic_continuation(
    tmp_path: Path,
) -> None:
    manager, parent, _ = await _terminal_project(tmp_path)
    automatic = await manager.reply_goal(
        parent,
        GoalMessageRequest(message="Add exports", client_message_id="automatic-next"),
        actor_id="phone",
    )
    assert automatic["goal"]["started_at"] is not None
    assert automatic["goal"]["current_phase"] == "continuation_pending"
    before = await _snapshot(manager)
    with pytest.raises(GoalManagerConflict, match="no active work"):
        await manager.reply_goal(parent, _request(), actor_id="phone")
    assert await _snapshot(manager) == before
