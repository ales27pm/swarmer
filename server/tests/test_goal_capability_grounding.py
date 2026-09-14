from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import AgentCreate
from app.services.context_builder import ContextBuilder
from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.project_memory import ProjectMemoryService
from app.services.swarm_contracts import (
    EvaluationDecision,
    GoalCreateRequest,
    GoalEvaluationContext,
    GoalMessageRequest,
    GoalReplanRequest,
    GoalStartRequest,
    PlannerSource,
    SwarmPlanProposal,
)
from tests.test_goal_context_payloads import _CapturingPlanner, _synthesis_plan
from tests.test_goal_runtime_recovery import _manager, _worker_plan


async def _agent(manager: GoalManager, skill: str, *, age: int = 0, status: str = "online") -> str:
    registration = await manager.state_service.register_agent(
        AgentCreate(name=f"Worker {skill}", endpoint="http://127.0.0.1:9001", skills=[skill]),
        "phone",
    )
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE agents SET status=?,last_seen_at=? WHERE id=?",
            (status, (datetime.now(UTC) - timedelta(seconds=age)).isoformat(), registration["id"]),
        )
        await db.commit()
    return str(registration["id"])


async def _production_manager(tmp_path: Path) -> tuple[GoalManager, str]:
    manager = await _manager(tmp_path / "grounding.db", _worker_plan())
    manager.require_execution_workers = True
    manager.context_builder = ContextBuilder(manager.db_path, max_tokens=2_048)
    await manager.context_builder.initialize()
    manager.planner = _CapturingPlanner(_worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository"), actor_id="phone"
    )
    return manager, str(goal["id"])


@pytest.mark.asyncio
async def test_initial_planner_cannot_use_a_stale_worker_absent_from_its_context(
    tmp_path: Path,
) -> None:
    manager, goal_id = await _production_manager(tmp_path)
    await _agent(manager, "code.build_project")
    await _agent(manager, "workspace.list_dir", age=67 * 3_600)
    with pytest.raises(GoalManagerConflict):
        await manager.start_goal(goal_id, GoalStartRequest())
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None and goal["status"] == "planning"
    assert goal["current_phase"] == "planner_invalid_response"
    assert goal["model_call_count"] == 1
    assert await manager.graph.list_nodes(goal_id) == []
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone() == (0,)
        assert await (
            await db.execute("SELECT status,error_category FROM goal_model_calls")
        ).fetchall() == [("failed", "invalid_response")]
    assert await manager.reconcile() == 0


@pytest.mark.asyncio
async def test_durable_policy_revocation_before_manual_insertion_rolls_back_goal_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, goal_id = await _production_manager(tmp_path)
    await _agent(manager, "workspace.list_dir")
    obtain = manager._obtain_plan

    async def revoke_after_review(*args: Any, **kwargs: Any):
        proposal = await obtain(*args, **kwargs)
        policy = manager.agent_dispatcher.worker_skill_policy
        async with aiosqlite.connect(manager.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            snapshot = await policy.load_locked(db, now=manager._now())
            assert snapshot is not None
            rules = dict(snapshot.rules)
            rules["workspace.list_dir"] = replace(
                rules["workspace.list_dir"], decision="deny", auto_redistribute=False
            )
            encoded, digest = policy.encode_rules(rules)
            await db.execute(
                "UPDATE worker_skill_policy_state SET epoch=epoch+1,rules_json=?,rules_digest=?",
                (encoded, digest),
            )
            await db.commit()
        return proposal

    monkeypatch.setattr(manager, "_obtain_plan", revoke_after_review)
    before = await _execution_state(manager)
    with pytest.raises(GoalManagerConflict, match="available capabilities"):
        await manager.start_goal(
            goal_id, GoalStartRequest(plan_proposal=_worker_plan(), planner_source="manual")
        )
    assert await _execution_state(manager) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["explicit_replan", "reply_replan"])
async def test_model_replans_cannot_add_unpresented_skills_and_account_one_attempt(
    tmp_path: Path, route: str
) -> None:
    manager, goal_id = await _production_manager(tmp_path)
    await _agent(manager, "code.build_project")
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None
    await manager._persist_initial_plan(
        goal,
        _synthesis_plan("Inspect the repository", suffix="initial"),
        source=PlannerSource.MANUAL,
        model_call_id=None,
    )
    await manager._drain_synthesis_nodes(goal_id)
    if route == "reply_replan":
        await manager.reply_goal(
            goal_id,
            GoalMessageRequest(message="Preserve existing files", client_message_id="new-guidance"),
            actor_id="phone",
        )
    original_nodes = await manager.graph.list_nodes(goal_id)
    with pytest.raises(GoalManagerConflict):
        if route == "explicit_replan":
            await manager.replan_goal(goal_id, GoalReplanRequest())
        else:
            await manager._resume_pending_conversation(goal_id)
    assert await manager.graph.list_nodes(goal_id) == original_nodes
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None and goal["replan_count"] == 0 and goal["model_call_count"] == 1
    assert goal["current_phase"] == "planner_invalid_response"
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone() == (0,)


@pytest.mark.asyncio
async def test_worker_appearing_during_generation_does_not_change_presented_capabilities(
    tmp_path: Path,
) -> None:
    manager, goal_id = await _production_manager(tmp_path)
    await _agent(manager, "code.build_project")

    class ArrivingWorkerPlanner(_CapturingPlanner):
        async def propose(self, context: Mapping[str, object]) -> SwarmPlanProposal:
            await _agent(manager, "workspace.list_dir")
            return await super().propose(context)

    manager.planner = ArrivingWorkerPlanner(_worker_plan())
    with pytest.raises(GoalManagerConflict):
        await manager.start_goal(goal_id, GoalStartRequest())
    assert await manager.graph.list_nodes(goal_id) == []


async def _execution_state(manager: GoalManager) -> dict[str, list[tuple[Any, ...]]]:
    async with aiosqlite.connect(manager.db_path) as db:
        return {
            table: await (await db.execute("SELECT * FROM " + table + " ORDER BY rowid")).fetchall()
            for table in (
                "goal_runs",
                "plan_nodes",
                "plan_edges",
                "tasks",
                "agent_jobs",
                "audit_events",
            )
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["manual", "iphone_local"])
@pytest.mark.parametrize("race", [False, True])
async def test_supplied_plan_refusal_is_atomic_even_if_worker_disappears_after_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, race: bool
) -> None:
    manager, goal_id = await _production_manager(tmp_path)
    agent_id = await _agent(manager, "workspace.list_dir", status="online" if race else "offline")
    request = GoalStartRequest(plan_proposal=_worker_plan(), planner_source=source)
    if source == "iphone_local":
        manager.project_memory = ProjectMemoryService(manager.db_path, None)
        await manager.project_memory.initialize()
        receipt = await manager.project_memory.retrieve_for_goal(goal_id, "planner")
        request.memory_context_fingerprint = receipt["context_fingerprint"]
    if race:
        obtain = manager._obtain_plan

        async def disconnect_after_review(*args: Any, **kwargs: Any):
            proposal = await obtain(*args, **kwargs)
            async with aiosqlite.connect(manager.db_path) as db:
                await db.execute("UPDATE agents SET status='offline' WHERE id=?", (agent_id,))
                await db.commit()
            return proposal

        monkeypatch.setattr(manager, "_obtain_plan", disconnect_after_review)
    before = await _execution_state(manager)
    with pytest.raises(GoalManagerConflict, match="available capabilities"):
        await manager.start_goal(goal_id, request)
    assert await _execution_state(manager) == before
    assert isinstance(manager.planner, _CapturingPlanner) and manager.planner.payloads == []


@pytest.mark.asyncio
async def test_busy_worker_is_a_planning_capability_even_without_an_available_slot(
    tmp_path: Path,
) -> None:
    manager, goal_id = await _production_manager(tmp_path)
    await _agent(manager, "workspace.list_dir", status="busy")
    detail = await manager.start_goal(
        goal_id, GoalStartRequest(plan_proposal=_worker_plan(), planner_source="manual")
    )
    assert detail["goal"]["started_at"] is not None
    assert detail["nodes"][0]["status"] == "dispatched"
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT status,claimed_by FROM agent_jobs")).fetchall() == [
            ("queued", None)
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["expired", "draining", "protocol"])
async def test_manual_plan_uses_canonical_freshness_status_and_protocol(
    tmp_path: Path, condition: str
) -> None:
    manager, goal_id = await _production_manager(tmp_path)
    agent_id = await _agent(manager, "workspace.list_dir")
    now = datetime.now(UTC)
    manager.agent_dispatcher.clock = lambda: now
    async with aiosqlite.connect(manager.db_path) as db:
        if condition == "expired":
            await db.execute(
                "UPDATE agents SET last_seen_at=? WHERE id=?",
                ((now - timedelta(seconds=90)).isoformat(), agent_id),
            )
        elif condition == "draining":
            await db.execute("UPDATE agents SET status='draining' WHERE id=?", (agent_id,))
        else:
            await db.execute(
                "UPDATE agents SET supported_protocol_version='unsupported' WHERE id=?", (agent_id,)
            )
        await db.commit()
    before = await _execution_state(manager)
    with pytest.raises(GoalManagerConflict, match="available capabilities"):
        await manager.start_goal(
            goal_id, GoalStartRequest(plan_proposal=_worker_plan(), planner_source="manual")
        )
    assert await _execution_state(manager) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["manual", "iphone_local"])
async def test_replan_insertion_refuses_missing_capability_without_replacing_existing_graph(
    tmp_path: Path, source: str
) -> None:
    manager, goal_id = await _production_manager(tmp_path)
    initial = _synthesis_plan("Inspect the repository", suffix="initial")
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None
    await manager._persist_initial_plan(
        goal, initial, source=PlannerSource.MANUAL, model_call_id=None
    )
    before = await _execution_state(manager)
    with pytest.raises(GoalManagerConflict, match="available capabilities"):
        await manager.replan_goal(
            goal_id, GoalReplanRequest(plan_proposal=_worker_plan(), planner_source=source)
        )
    assert await _execution_state(manager) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["new_unpresented_worker", "presented_worker_disappears"])
async def test_evaluator_capability_rejection_preserves_graph_and_accounts_one_attempt(
    tmp_path: Path, change: str
) -> None:
    manager, goal_id = await _production_manager(tmp_path)
    old_skill = (
        "workspace.read_text" if change == "new_unpresented_worker" else "workspace.list_dir"
    )
    agent_id = await _agent(manager, old_skill)
    initial = _synthesis_plan("Inspect the repository", suffix="initial")
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None
    await manager._persist_initial_plan(
        goal, initial, source=PlannerSource.MANUAL, model_call_id=None
    )
    await manager._drain_synthesis_nodes(goal_id)
    original_nodes = await manager.graph.list_nodes(goal_id)

    class ChangingEvaluator:
        source = PlannerSource.UBUNTU_LOCAL

        async def evaluate(self, context: GoalEvaluationContext) -> EvaluationDecision:
            assert context.available_skills == [old_skill]
            if change == "new_unpresented_worker":
                await _agent(manager, "workspace.list_dir")
            else:
                async with aiosqlite.connect(manager.db_path) as db:
                    await db.execute("UPDATE agents SET status='offline' WHERE id=?", (agent_id,))
                    await db.commit()
            return EvaluationDecision(
                schema_version="1.0",
                status="continue",
                reason_summary="Inspect the repository",
                missing_requirements=[],
                invalid_results=[],
                suggested_new_nodes=_worker_plan().nodes,
            )

    manager.evaluator = ChangingEvaluator()
    await manager._evaluate_if_quiescent(goal_id)
    assert await manager.graph.list_nodes(goal_id) == original_nodes
    goal = await manager.graph.get_goal(goal_id)
    assert goal is not None and goal["model_call_count"] == 1
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone() == (0,)
        assert await (await db.execute("SELECT COUNT(*) FROM goal_evaluations")).fetchone() == (0,)
        assert await (
            await db.execute("SELECT status,error_category FROM goal_model_calls")
        ).fetchall() == [("failed", "invalid_response")]
