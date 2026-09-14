from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import aiosqlite
import pytest

from app.services.context_builder import ContextBuilder, ContextCard, bound_evaluation_context
from app.services.execution_engine import ExecutionEngine
from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.goal_project import GoalProjectService
from app.services.project_contracts import ProjectMemoryContext, ProjectMemoryItem
from app.services.project_memory import ProjectMemoryService
from app.services.swarm_contracts import (
    EvaluationConversationMessage,
    GoalCreateRequest,
    GoalEvaluationContext,
    GoalStartRequest,
)
from tests.test_goal_context_payloads import (
    _CapturingEvaluator,
    _CapturingPlanner,
    _synthesis_plan,
)
from tests.test_goal_runtime_recovery import _manager
from tests.test_project_memory import SemanticProvider, _messages


async def _shared_goal(tmp_path: Path) -> tuple[GoalManager, str, SemanticProvider]:
    objective = "Create purchaser profiles in the Python CRM"
    manager = await _manager(tmp_path / "shared.db", _synthesis_plan(objective, suffix="one"))
    manager.context_builder = ContextBuilder(manager.db_path, max_tokens=2_048)
    manager.planner = _CapturingPlanner(_synthesis_plan(objective, suffix="one"))
    manager.evaluator = _CapturingEvaluator()
    provider = SemanticProvider()
    manager.project_memory = ProjectMemoryService(
        manager.db_path, provider, model_revision="revision-one"
    )
    await manager.project_memory.initialize()
    projects = GoalProjectService(
        manager.db_path, ExecutionEngine(manager.db_path, tmp_path, manager.permission_policy)
    )
    goal = await manager.create_goal(GoalCreateRequest(objective=objective), actor_id="phone")
    goal_id = str(goal["id"])
    await projects.ensure_project(goal_id)
    await _messages(
        manager, goal_id, ["Customer records use a shared identifier and French labels."]
    )
    return manager, goal_id, provider


@pytest.mark.asyncio
async def test_shared_index_reaches_real_planner_and_evaluator_payloads(tmp_path: Path) -> None:
    manager, goal_id, provider = await _shared_goal(tmp_path)
    detail = await manager.start_goal(goal_id, GoalStartRequest())
    assert detail["goal"]["objective"] == "Create purchaser profiles in the Python CRM"
    assert isinstance(manager.planner, _CapturingPlanner)
    cards = manager.planner.payloads[0]["cards"]
    assert isinstance(cards, list)
    hints = [card for card in cards if card["kind"] == "project_memory_hint"]
    assert hints and any("shared identifier" in card["summary"] for card in hints)
    assert sum(len(card["summary"]) for card in hints) <= 2_400
    assert isinstance(manager.evaluator, _CapturingEvaluator)
    memory = manager.evaluator.contexts[0].project_memory
    assert memory is not None and memory.mode == "semantic"
    assert any("shared identifier" in item.summary for item in memory.items)
    assert provider.calls
    # The recorded payload is the exact bounded payload sent to each provider.
    async with aiosqlite.connect(manager.db_path) as db:
        calls = await (
            await db.execute(
                "SELECT role,context_id FROM goal_model_calls WHERE goal_run_id=?", (goal_id,)
            )
        ).fetchall()
    for role, context_id in calls:
        record = await manager.context_builder.get_record(context_id)
        assert record is not None
        if role == "planner":
            assert record.payload == manager.planner.payloads[0]
        else:
            assert record.payload == manager.evaluator.contexts[0].model_dump(mode="json")
        assert any(item.source_id in record.provenance_ids for item in memory.items)


@pytest.mark.asyncio
@pytest.mark.parametrize("race_after_first_check", [False, True])
async def test_stale_phone_receipt_never_starts_or_persists_a_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race_after_first_check: bool
) -> None:
    manager, goal_id, _ = await _shared_goal(tmp_path)
    assert manager.project_memory is not None
    receipt = await manager.project_memory.retrieve_for_goal(goal_id, "planner")
    assert receipt["local_planning_eligible"] is True
    request = GoalStartRequest(
        planner_source="iphone_local",
        plan_proposal=_synthesis_plan(
            "Create purchaser profiles in the Python CRM", suffix="phone"
        ),
        memory_context_fingerprint=receipt["context_fingerprint"],
    )
    if race_after_first_check:
        original = manager._obtain_plan

        async def changed_between_checks(*args, **kwargs):  # type: ignore[no-untyped-def]
            await _messages(manager, goal_id, ["Customer profiles must also include a calendar."])
            return await original(*args, **kwargs)

        monkeypatch.setattr(manager, "_obtain_plan", changed_between_checks)
    else:
        await _messages(manager, goal_id, ["Customer profiles must also include a calendar."])
    with pytest.raises(GoalManagerConflict):
        await manager.start_goal(goal_id, request)
    detail = await manager.get_goal(goal_id)
    assert detail is not None
    assert detail["goal"]["started_at"] is None
    assert detail["goal"]["status"] == "planning"
    assert detail["nodes"] == []
    assert detail["goal"]["model_call_count"] == receipt["planning_embedding_call_count"]


@pytest.mark.asyncio
async def test_current_phone_receipt_starts_without_server_planning(tmp_path: Path) -> None:
    manager, goal_id, _ = await _shared_goal(tmp_path)
    assert manager.project_memory is not None
    receipt = await manager.project_memory.retrieve_for_goal(goal_id, "planner")
    detail = await manager.start_goal(
        goal_id,
        GoalStartRequest(
            planner_source="iphone_local",
            plan_proposal=_synthesis_plan(
                "Create purchaser profiles in the Python CRM", suffix="phone"
            ),
            memory_context_fingerprint=receipt["context_fingerprint"],
        ),
    )
    assert detail["goal"]["started_at"] is not None and detail["nodes"]
    assert isinstance(manager.planner, _CapturingPlanner)
    assert manager.planner.payloads == []
    async with aiosqlite.connect(manager.db_path) as db:
        rows = await (
            await db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='goal.plan.accepted'"
            )
        ).fetchall()
    assert any(
        json.loads(row[0])["memory_context_fingerprint"] == receipt["context_fingerprint"]
        for row in rows
    )


@pytest.mark.asyncio
async def test_concurrent_start_does_not_advance_another_accepted_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, goal_id, _ = await _shared_goal(tmp_path)
    assert manager.project_memory is not None
    receipt = await manager.project_memory.retrieve_for_goal(goal_id, "planner")
    other = GoalManager(
        manager.db_path,
        state_service=manager.state_service,
        agent_dispatcher=manager.agent_dispatcher,
        planner=manager.planner,
        evaluator=_CapturingEvaluator(),
        permission_policy=manager.permission_policy,
    )
    original = manager._assert_local_memory_current
    accepted = []

    async def concurrently_start(identity, fingerprint, *, db=None):  # type: ignore[no-untyped-def]
        await original(identity, fingerprint, db=db)
        if db is None:
            accepted.append(
                await other.start_goal(
                    goal_id,
                    GoalStartRequest(
                        planner_source="manual",
                        plan_proposal=_synthesis_plan(
                            "Create purchaser profiles in the Python CRM", suffix="other"
                        ),
                    ),
                )
            )

    monkeypatch.setattr(manager, "_assert_local_memory_current", concurrently_start)
    with pytest.raises(GoalManagerConflict):
        await manager.start_goal(
            goal_id,
            GoalStartRequest(
                planner_source="iphone_local",
                plan_proposal=_synthesis_plan(
                    "Create purchaser profiles in the Python CRM", suffix="phone"
                ),
                memory_context_fingerprint=receipt["context_fingerprint"],
            ),
        )
    current = await manager.get_goal(goal_id)
    assert current == accepted[0]
    assert isinstance(manager.evaluator, _CapturingEvaluator)
    assert manager.evaluator.contexts == []


def test_optional_memory_never_displaces_current_goal_or_latest_answer() -> None:
    context = GoalEvaluationContext(
        schema_version="1.0",
        goal_run_id="goal_memory",
        objective="Create the Python CRM",
        completion_criteria=["Persist clients and calendar entries"],
        node_results=[],
        known_node_ids=[],
        conversation_revision=2,
        conversation=[
            EvaluationConversationMessage(role="assistant", content="Which features?"),
            EvaluationConversationMessage(role="user", content="Clients and calendar, in French."),
        ],
        remaining_step_budget=10,
        remaining_model_call_budget=5,
        elapsed_seconds=1,
        state_fingerprint="a" * 64,
    )
    canonical = json.dumps(
        context.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    exact_budget = math.ceil(len(canonical.encode()) / 4)
    memory = ProjectMemoryContext(
        mode="semantic",
        reason="matched",
        items=[
            ProjectMemoryItem(
                id="mem_one", source_id="message_one", summary="Old history " * 100, score=1.0
            )
        ],
    )
    enriched = context.model_copy(update={"project_memory": memory})
    bounded = bound_evaluation_context(enriched, max_tokens=exact_budget)
    assert bounded == context
    generous = bound_evaluation_context(enriched, max_tokens=2_048)
    assert generous.project_memory is not None
    assert generous.conversation == context.conversation
    assert len(generous.project_memory.items[0].summary) <= 600


@pytest.mark.asyncio
@pytest.mark.parametrize("max_tokens", [768, 1_024, 2_048])
async def test_planner_hints_never_displace_failure_or_current_guidance(
    tmp_path: Path, max_tokens: int
) -> None:
    manager, goal_id, _ = await _shared_goal(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET failure_reason=? WHERE id=?",
            ("The current build fails while saving a calendar entry. " * 30, goal_id),
        )
        await db.commit()
    builder = ContextBuilder(manager.db_path, max_tokens=max_tokens)
    guidance = ContextCard(
        card_id="user-current",
        kind="user_guidance",
        summary="Keep the current French UI. " * 35,
        provenance_ids=(goal_id,),
    )
    hints = tuple(
        ContextCard(
            card_id=f"history-{index}",
            kind="project_memory_hint",
            summary="Old decisions. " * 42,
            provenance_ids=(f"source-{index}",),
        )
        for index in range(4)
    )
    original = await builder.build_for_goal(goal_id, additional_cards=(guidance,))
    enriched = await builder.build_for_goal(goal_id, additional_cards=(guidance, *hints))
    assert (
        tuple(card for card in enriched.cards if card.kind != "project_memory_hint")
        == original.cards
    )
    assert enriched.approx_token_count <= max_tokens


@pytest.mark.asyncio
async def test_inflight_memory_retrieval_defers_evaluator_without_pausing_goal(
    tmp_path: Path,
) -> None:
    manager, goal_id, _ = await _shared_goal(tmp_path)
    manager.project_memory = None
    await manager.start_goal(
        goal_id,
        GoalStartRequest(
            planner_source="manual",
            plan_proposal=_synthesis_plan(
                "Create purchaser profiles in the Python CRM", suffix="one"
            ),
        ),
    )
    assert isinstance(manager.evaluator, _CapturingEvaluator)
    manager.evaluator.contexts.clear()

    class DelayedProvider(SemanticProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.started.set()
            await self.release.wait()
            return await super().embed(texts)

    provider = DelayedProvider()
    manager.project_memory = ProjectMemoryService(manager.db_path, provider, model_revision="one")
    pending = asyncio.create_task(manager.project_memory.retrieve_for_goal(goal_id, "evaluator"))
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        before = await manager.graph.get_goal(goal_id)
        await manager._evaluate_if_quiescent(goal_id, explicit_user_action=True)
        after = await manager.graph.get_goal(goal_id)
        assert before is not None and after is not None
        assert after["status"] == "running"
        assert after["current_phase"] == before["current_phase"]
        assert manager.evaluator.contexts == []
    finally:
        provider.release.set()
        await pending
    await manager._evaluate_if_quiescent(goal_id, explicit_user_action=True)
    assert len(manager.evaluator.contexts) == 1
    assert manager.evaluator.contexts[0].project_memory is not None
