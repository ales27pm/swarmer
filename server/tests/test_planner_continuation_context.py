from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.context_builder import ContextBuilder
from app.services.goal_conversation import GoalConversationService
from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.swarm_contracts import GoalMessageRequest
from tests.test_goal_context_payloads import _CapturingPlanner
from tests.test_goal_project_runtime import SOURCE, _project, _result
from tests.test_goal_runtime_recovery import _worker_plan


def _planner(skill: str = "code.build_project") -> _CapturingPlanner:
    plan = _worker_plan(objective="Build a web CRM")
    plan.nodes[0] = plan.nodes[0].model_copy(
        update={
            "required_skill": skill,
            "objective": "Build a web CRM" if skill == "code.build_project" else "Describe the CRM",
            "expected_output": "The requested project work",
        }
    )
    return _CapturingPlanner(plan)


async def _saved_project(tmp_path: Path) -> tuple[GoalManager, str]:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="clarify", message="Which customer fields are required?")
    manager.context_builder = ContextBuilder(manager.db_path, max_tokens=2_048)
    await manager.context_builder.initialize()
    return manager, goal_id


async def _reply(manager: GoalManager, goal_id: str, message: str, key: str = "continue") -> str:
    result = await manager.reply_goal(
        goal_id,
        GoalMessageRequest(message=message, client_message_id=key),
        actor_id="phone",
    )
    return str(result["goal"]["id"])


async def _revisions(manager: GoalManager) -> list[tuple[Any, ...]]:
    async with aiosqlite.connect(manager.db_path) as db:
        return await (
            await db.execute(
                "SELECT id,project_id,revision,sha256,snapshot_json FROM project_revisions ORDER BY id"
            )
        ).fetchall()


def _card(payload: dict[str, object], kind: str) -> dict[str, Any]:
    matches = [card for card in payload["cards"] if card["kind"] == kind]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.asyncio
async def test_continue_keeps_latest_instruction_and_saved_metadata_after_long_receipts(
    tmp_path: Path,
) -> None:
    manager, goal_id = await _saved_project(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        for index in range(6):
            await GoalConversationService.assistant_locked(
                db,
                goal_id,
                f"Receipt {index}: " + "The previous checks did not establish completion. " * 70,
                question=False,
                now=manager._now(),
            )
        await db.commit()
    before = await _revisions(manager)
    await _reply(manager, goal_id, "Continue")
    planner = _planner()
    manager.planner = planner

    await manager._resume_pending_conversation(goal_id)

    assert len(planner.payloads) == 1
    payload = planner.payloads[0]
    assert _card(payload, "latest_user_message")["summary"] == "Continue"
    state = json.loads(_card(payload, "saved_project_state")["summary"])
    assert state["revision_id"] == before[0][0]
    assert state["project_id"] == before[0][1]
    assert state["saved_file_count"] == 3
    assert state["runtime"] == "python"
    assert state["implementation_skill"] == "code.build_project"
    assert state["completion"] == "not inferred from saved files"
    assert set(state["file_names_untrusted"]) == {"app.py", "README.md", "test_app.py"}
    assert "def add_customer" not in json.dumps(payload)
    assert await _revisions(manager) == before
    async with aiosqlite.connect(manager.db_path) as db:
        stored = await (
            await db.execute(
                "SELECT context_json FROM goal_contexts WHERE goal_run_id=? AND purpose='planner'",
                (goal_id,),
            )
        ).fetchall()
    assert len(stored) == 1 and json.loads(stored[0][0]) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["cancelled", "failed", "completed"])
async def test_terminal_successor_routes_with_ancestor_snapshot_and_latest_reply(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    manager, old_id = await _saved_project(tmp_path)
    before = await _revisions(manager)
    await manager._terminate_goal(old_id, status=terminal_status, reason=None)
    goal_id = await _reply(manager, old_id, "Continue the saved implementation")
    assert goal_id != old_id
    planner = _planner()
    manager.planner = planner

    await manager._resume_pending_conversation(goal_id)

    assert len(planner.payloads) == 1
    payload = planner.payloads[0]
    assert _card(payload, "latest_user_message")["summary"] == "Continue the saved implementation"
    state = json.loads(_card(payload, "saved_project_state")["summary"])
    assert state["revision_id"] == before[0][0]
    assert state["project_id"] == before[0][1]
    assert await _revisions(manager) == before
    assert "def add_customer" not in json.dumps(payload)
    nodes = await manager.graph.list_nodes(goal_id)
    assert len(nodes) == 1 and nodes[0]["required_skill"] == "code.build_project"
    assert manager.project_applications is not None
    worker_payload = await manager.project_applications.payload(
        goal_id, nodes[0], await manager.recent_conversation(goal_id)
    )
    assert worker_payload["base_revision_id"] == before[0][0]
    assert any(file["content"] == SOURCE for file in worker_payload["files"])
    predecessor = await manager.graph.get_goal(old_id)
    assert predecessor is not None and predecessor["status"] == terminal_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "skill"),
    [
        ("Write a user guide for this CRM", "writing.draft"),
        ("Research current CRM accessibility guidance", "research.query"),
    ],
)
async def test_saved_project_context_does_not_force_every_explicit_request_to_coder(
    tmp_path: Path, message: str, skill: str
) -> None:
    manager, goal_id = await _saved_project(tmp_path)
    before = await _revisions(manager)
    await _reply(manager, goal_id, message)
    planner = _planner(skill)
    manager.planner = planner

    await manager._resume_pending_conversation(goal_id)

    assert len(planner.payloads) == 1
    assert _card(planner.payloads[0], "latest_user_message")["summary"] == message
    nodes = await manager.graph.list_nodes(goal_id)
    added = [node for node in nodes if node["conversation_revision"] == 1]
    assert len(added) == 1 and added[0]["required_skill"] == skill
    assert await _revisions(manager) == before


@pytest.mark.asyncio
async def test_long_latest_instruction_keeps_final_routing_constraint(tmp_path: Path) -> None:
    manager, goal_id = await _saved_project(tmp_path)
    message = "Keep the existing customer records and the interface conventions. " * 40
    message += "Finally, research accessibility before changing any project files."
    assert 1_000 < len(message) < 4_000
    await _reply(manager, goal_id, message)
    planner = _planner("research.query")
    manager.planner = planner

    await manager._resume_pending_conversation(goal_id)

    assert len(planner.payloads) == 1
    assert _card(planner.payloads[0], "latest_user_message")["summary"] == message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_tokens", "message"),
    [(64, "Continue"), (2_048, "Use /a " * 500 + "Keep this final constraint")],
)
async def test_unrepresentable_context_does_not_call_planner_or_change_snapshot(
    tmp_path: Path,
    max_tokens: int,
    message: str,
) -> None:
    manager, goal_id = await _saved_project(tmp_path)
    await _reply(manager, goal_id, message)
    manager.context_builder = ContextBuilder(manager.db_path, max_tokens=max_tokens)
    planner = _planner()
    manager.planner = planner
    before = await _revisions(manager)
    nodes = await manager.graph.list_nodes(goal_id)
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None

    with pytest.raises(GoalManagerConflict, match="context"):
        await manager._resume_pending_conversation(goal_id)

    assert planner.payloads == []
    assert await _revisions(manager) == before
    assert await manager.graph.list_nodes(goal_id) == nodes
    current = await manager.graph.get_goal(goal_id)
    assert current is not None and current["model_call_count"] == goal["model_call_count"]
    assert current["pending_message_revision"] == 1


@pytest.mark.asyncio
async def test_new_reply_during_planning_fences_old_continuation_without_losing_snapshot(
    tmp_path: Path,
) -> None:
    manager, goal_id = await _saved_project(tmp_path)
    await _reply(manager, goal_id, "Continue")
    entered, release = asyncio.Event(), asyncio.Event()
    planner = _planner()
    original_propose = planner.propose

    async def delayed_propose(context):
        proposal = await original_propose(context)
        entered.set()
        await release.wait()
        return proposal

    planner.propose = delayed_propose
    manager.planner = planner
    before = await _revisions(manager)
    nodes = await manager.graph.list_nodes(goal_id)
    pending = asyncio.create_task(manager._resume_pending_conversation(goal_id))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        await _reply(manager, goal_id, "Research accessibility first", key="new-steering")
    finally:
        release.set()
    with pytest.raises(GoalManagerConflict):
        await asyncio.wait_for(pending, timeout=3)

    assert await _revisions(manager) == before
    assert await manager.graph.list_nodes(goal_id) == nodes
    current = await manager.graph.get_goal(goal_id)
    assert current is not None and current["pending_message_revision"] == 2
    assert _card(planner.payloads[0], "latest_user_message")["summary"] == "Continue"


@pytest.mark.asyncio
async def test_new_reply_during_context_build_prevents_stale_model_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, goal_id = await _saved_project(tmp_path)
    await _reply(manager, goal_id, "Continue")
    planner = _planner()
    manager.planner = planner
    assert manager.context_builder is not None
    build_context = manager.context_builder.build_for_goal
    built_payloads: list[dict[str, object]] = []

    async def receive_reply_after_context_build(*args, **kwargs):
        context = await build_context(*args, **kwargs)
        built_payloads.append(context.model_payload())
        await _reply(manager, goal_id, "Research accessibility first", key="during-context-build")
        return context

    monkeypatch.setattr(
        manager.context_builder, "build_for_goal", receive_reply_after_context_build
    )
    before = await _revisions(manager)
    nodes = await manager.graph.list_nodes(goal_id)
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None
    async with aiosqlite.connect(manager.db_path) as db:
        calls_before = await (await db.execute("SELECT COUNT(*) FROM goal_model_calls")).fetchone()

    with pytest.raises(GoalManagerConflict):
        await manager._resume_pending_conversation(goal_id)

    assert len(built_payloads) == 1
    assert _card(built_payloads[0], "latest_user_message")["summary"] == "Continue"
    assert planner.payloads == []
    assert await _revisions(manager) == before
    assert await manager.graph.list_nodes(goal_id) == nodes
    current = await manager.graph.get_goal(goal_id)
    assert current is not None and current["model_call_count"] == goal["model_call_count"]
    assert current["pending_message_revision"] == 2
    messages = await manager.recent_conversation(goal_id)
    assert messages[-1] == {"role": "user", "content": "Research accessibility first"}
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await (await db.execute("SELECT COUNT(*) FROM goal_model_calls")).fetchone()
            == calls_before
        )
