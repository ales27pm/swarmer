from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.services.goal_limits import active_runtime_seconds
from app.services.planner_provider import DeterministicSwarmPlannerProvider
from app.services.swarm_contracts import (
    EvaluationDecision,
    EvaluationStatus,
    GoalCreateRequest,
    GoalEvaluationContext,
    GoalMessageRequest,
    GoalReplanRequest,
    GoalStartRequest,
    PlannerSource,
    PlanNodeType,
    SwarmPlanNodeProposal,
)
from tests.test_goal_context_payloads import _synthesis_plan
from tests.test_goal_manager import _manager

OBJECTIVE = "Crées une Application CRM en python"
QUESTION = "Quelles fonctionnalités souhaitez-vous inclure dans votre application CRM ?"
ANSWER = "Fiches clients, soumissions/projet, courriels, calendrier"


class ClarificationEvaluator:
    source = PlannerSource.TEST
    model = "clarification-regression"

    def __init__(self) -> None:
        self.contexts: list[GoalEvaluationContext] = []

    async def evaluate(self, context: GoalEvaluationContext) -> EvaluationDecision:
        self.contexts.append(context.model_copy(deep=True))
        answered = any(
            item["role"] == "user" and item["content"] == ANSWER
            for item in context.model_dump(mode="json").get("conversation", [])
        )
        return EvaluationDecision(
            schema_version="1.0",
            status=EvaluationStatus.CONTINUE if answered else EvaluationStatus.NEEDS_USER,
            reason_summary="Les besoins sont enregistrés." if answered else "Précisez les besoins.",
            missing_requirements=[] if answered else ["Fonctionnalités du CRM"],
            invalid_results=[],
            suggested_new_nodes=[
                SwarmPlanNodeProposal(
                    temporary_id="inspect_workspace",
                    node_type=PlanNodeType.WORKER,
                    title="Inspecter le projet",
                    objective="Inspecter les fichiers du projet avant son implémentation",
                    required_skill="workspace.list_dir",
                    dependencies=[],
                    expected_output="Liste des fichiers",
                    priority=1,
                )
            ]
            if answered
            else [],
            user_question=None if answered else QUESTION,
            completion_summary=None,
        )


@pytest.mark.asyncio
async def test_saved_answer_reaches_exact_evaluator_payload_and_new_nodes(tmp_path: Path) -> None:
    evaluator = ClarificationEvaluator()
    manager = await _manager(
        tmp_path, _synthesis_plan(OBJECTIVE, suffix="first"), evaluator=evaluator
    )
    created = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.start_goal(created["id"], GoalStartRequest())
    history = await manager.conversation_messages(created["id"])
    assert history["pending_question_id"] is not None
    manager.planner = DeterministicSwarmPlannerProvider(_synthesis_plan(OBJECTIVE, suffix="reply"))
    await manager.reply_goal(
        created["id"],
        GoalMessageRequest(
            message=ANSWER,
            client_message_id="crm-features",
            reply_to_message_id=history["pending_question_id"],
        ),
        actor_id="phone",
    )
    await manager.reconcile()

    context = evaluator.contexts[-1].model_dump(mode="json")
    assert context["conversation_revision"] == 1
    assert context["conversation"][-2:] == [
        {"role": "assistant", "content": QUESTION},
        {"role": "user", "content": ANSWER},
    ]
    assert (await manager.conversation_messages(created["id"]))["pending_question_id"] is None
    nodes = await manager.graph.list_nodes(created["id"])
    assert {
        node["conversation_revision"] for node in nodes if node["title"] != "Synthesis first"
    } == {1}
    assert any(node["required_skill"] == "workspace.list_dir" for node in nodes)
    async with aiosqlite.connect(manager.db_path) as db:
        record = await (
            await db.execute(
                """SELECT c.context_json,m.input_digest FROM goal_model_calls m
                JOIN goal_contexts c ON c.id=m.context_id
                WHERE m.goal_run_id=? AND m.role='evaluator' AND m.conversation_revision=1""",
                (created["id"],),
            )
        ).fetchone()
    assert record is not None and json.loads(record[0]) == context
    canonical = json.dumps(context, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert hashlib.sha256(canonical.encode()).hexdigest() == record[1]


@pytest.mark.asyncio
async def test_empty_synthesis_is_skipped_without_spending_a_step(tmp_path: Path) -> None:
    plan = _synthesis_plan(OBJECTIVE, suffix="empty")
    plan.nodes.append(
        plan.nodes[0].model_copy(
            update={"temporary_id": "chained", "dependencies": ["synthesis_empty"]}
        )
    )
    manager = await _manager(tmp_path, plan)
    created = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    detail = await manager.start_goal(created["id"], GoalStartRequest())
    assert detail["goal"]["step_count"] == 0
    assert {node["status"] for node in detail["nodes"]} == {"skipped", "blocked"}
    assert all(not node["result_summary"] for node in detail["nodes"])


@pytest.mark.asyncio
async def test_legacy_empty_summary_cannot_be_reused_as_synthesis_evidence(tmp_path: Path) -> None:
    plan = _synthesis_plan(OBJECTIVE, suffix="legacy")
    plan.nodes.append(
        plan.nodes[0].model_copy(
            update={"temporary_id": "chained", "dependencies": ["synthesis_legacy"]}
        )
    )
    manager = await _manager(tmp_path, plan)
    created = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.start_goal(created["id"], GoalStartRequest())
    nodes = await manager.graph.list_nodes(created["id"])
    parent = next(node for node in nodes if not node["depends_on"])
    child = next(node for node in nodes if node["depends_on"])
    # Reproduce persisted pre-fix data, including a chained placeholder.
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET status='completed',result_summary=? WHERE id=?",
            ("No evidence summary was available for synthesis.\n" * 2, parent["id"]),
        )
        await db.execute("UPDATE plan_nodes SET status='ready' WHERE id=?", (child["id"],))
        await db.commit()
    await manager._complete_deterministic_synthesis(child)
    current = await manager.graph.get_node(child["id"])
    assert current is not None and current["status"] == "skipped"
    assert current["result_summary"] is None


@pytest.mark.asyncio
async def test_replan_resumes_active_runtime_after_waiting_for_user(tmp_path: Path) -> None:
    manager = await _manager(
        tmp_path, _synthesis_plan(OBJECTIVE, suffix="first"), evaluator=ClarificationEvaluator()
    )
    created = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.start_goal(created["id"], GoalStartRequest())
    now = datetime.now(UTC)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET started_at=?,paused_at=? WHERE id=?",
            (
                (now - timedelta(days=2, seconds=10)).isoformat(),
                (now - timedelta(days=2)).isoformat(),
                created["id"],
            ),
        )
        await db.commit()
    manager.planner = DeterministicSwarmPlannerProvider(_synthesis_plan(OBJECTIVE, suffix="replan"))
    await manager.replan_goal(created["id"], GoalReplanRequest())
    current = await manager.graph.get_goal(created["id"])
    assert current is not None and current["paused_at"] is None
    assert 10 <= active_runtime_seconds(current) < 15
