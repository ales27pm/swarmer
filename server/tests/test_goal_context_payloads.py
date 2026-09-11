from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path

import aiosqlite
import pytest

from app.services.agent_dispatcher import AgentDispatcher
from app.services.context_builder import ContextBuilder
from app.services.goal_manager import GoalManager
from app.services.message_board import SQLiteMessageBoard
from app.services.permission_policy import PermissionPolicy
from app.services.state_service import StateService
from app.services.swarm_contracts import (
    AutonomyProfile,
    EvaluationDecision,
    EvaluationStatus,
    GoalCreateRequest,
    GoalEvaluationContext,
    GoalReplanRequest,
    GoalStartRequest,
    PlannerSource,
    PlanNodeType,
    SwarmPlanNodeProposal,
    SwarmPlanProposal,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _synthesis_plan(objective: str, *, suffix: str) -> SwarmPlanProposal:
    return SwarmPlanProposal(
        schema_version="1.0",
        objective=objective,
        rationale_summary=f"Perform bounded synthesis {suffix}.",
        completion_criteria=["Return a bounded result."],
        max_parallelism=1,
        nodes=[
            SwarmPlanNodeProposal(
                temporary_id=f"synthesis_{suffix}",
                node_type=PlanNodeType.SYNTHESIS,
                title=f"Synthesis {suffix}",
                objective=f"Produce bounded result {suffix}.",
                required_skill=None,
                dependencies=[],
                expected_output=f"Bounded result {suffix}.",
                priority=1,
            )
        ],
    )


class _CapturingPlanner:
    source = PlannerSource.UBUNTU_LOCAL
    model = "capturing-planner"

    def __init__(self, proposal: SwarmPlanProposal) -> None:
        self.proposal = proposal
        self.payloads: list[dict[str, object]] = []

    async def propose(self, context: Mapping[str, object]) -> SwarmPlanProposal:
        self.payloads.append(json.loads(json.dumps(dict(context))))
        cards = context["cards"]
        assert isinstance(cards, list)
        goal_card = next(card for card in cards if card["kind"] == "goal")
        return self.proposal.model_copy(update={"objective": goal_card["card_id"]}, deep=True)


class _CapturingEvaluator:
    source = PlannerSource.UBUNTU_LOCAL
    model = "capturing-evaluator"

    def __init__(self) -> None:
        self.contexts: list[GoalEvaluationContext] = []

    async def evaluate(self, context: GoalEvaluationContext) -> EvaluationDecision:
        self.contexts.append(context.model_copy(deep=True))
        return EvaluationDecision(
            schema_version="1.0",
            status=EvaluationStatus.CONTINUE,
            reason_summary="No additional work was proposed.",
            missing_requirements=[],
            invalid_results=[],
            suggested_new_nodes=[],
        )


class _Hints:
    def as_dict(self) -> dict[str, object]:
        return {
            "successful": [
                {
                    "kind": "success",
                    "source_id": "episode_hint",
                    "text": "Reuse token=hint-secret from /root/private carefully.",
                    "relevance": 1.0,
                }
            ],
            "failures": [],
            "memory": [],
            "provenance_ids": ["episode_hint"],
        }


class _StrategyRetrieval:
    async def retrieve(self, query: str) -> _Hints:
        assert "goal-secret" in query
        return _Hints()


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_goal_model_payloads_match_redacted_budgeted_context_records_and_replan_reason(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    state = StateService(db_path, permission_policy=policy)
    await state.initialize()
    builder = ContextBuilder(
        db_path,
        max_tokens=768,
        max_memory_items=0,
        max_episode_items=0,
        max_agent_cards=0,
        max_upstream_results=0,
    )
    planner = _CapturingPlanner(_synthesis_plan("Refine safely", suffix="second"))
    evaluator = _CapturingEvaluator()
    manager = GoalManager(
        db_path,
        state_service=state,
        agent_dispatcher=AgentDispatcher(
            db_path,
            SQLiteMessageBoard(db_path),
            permission_policy=policy,
        ),
        planner=planner,
        evaluator=evaluator,
        permission_policy=policy,
        context_builder=builder,
        strategy_retrieval=_StrategyRetrieval(),
    )
    await manager.initialize()
    objective = "Refine password=goal-secret from /etc/private safely"
    created = await manager.create_goal(
        GoalCreateRequest(
            objective=objective,
            autonomy_profile=AutonomyProfile.ASSISTED,
            completion_criteria=["Use api_key=criterion-secret without exposing it."],
        ),
        actor_id="test-phone",
    )
    await manager.start_goal(
        str(created["id"]),
        GoalStartRequest(
            plan_proposal=_synthesis_plan(objective, suffix="first"),
            planner_source="manual",
        ),
    )
    assert evaluator.contexts

    await manager.replan_goal(
        str(created["id"]),
        GoalReplanRequest(
            reason="Account for token=replan-secret at /home/alice/repository.",
        ),
    )

    assert planner.payloads
    planner_payload = planner.payloads[-1]
    assert set(planner_payload) == {"schema_version", "purpose", "cards"}
    planner_json = _canonical(planner_payload)
    assert '"kind":"user_guidance"' in planner_json
    assert '"kind":"strategy_hint"' in planner_json
    for forbidden in (
        "goal-secret",
        "criterion-secret",
        "replan-secret",
        "hint-secret",
        "/etc/private",
        "/home/alice",
        "/root/private",
    ):
        assert forbidden not in planner_json

    evaluator_payload = evaluator.contexts[-1].model_dump(mode="json")
    evaluator_json = _canonical(evaluator_payload)
    for forbidden in ("goal-secret", "criterion-secret", "/etc/private"):
        assert forbidden not in evaluator_json

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        calls = await (
            await db.execute(
                """SELECT role,context_id,input_digest FROM goal_model_calls
                WHERE goal_run_id=? ORDER BY created_at ASC,id ASC""",
                (str(created["id"]),),
            )
        ).fetchall()
    latest_by_role = {str(row["role"]): row for row in calls}
    for role, payload in (("planner", planner_payload), ("evaluator", evaluator_payload)):
        row = latest_by_role[role]
        record = await builder.get_record(str(row["context_id"]))
        assert record is not None
        assert record.payload == payload
        assert record.approx_token_count <= 768
        assert record.approx_token_count == max(
            1,
            math.ceil(len(_canonical(payload).encode()) / 4),
        )
        assert str(row["input_digest"]) == hashlib.sha256(_canonical(payload).encode()).hexdigest()
    planner_record = await builder.get_record(str(latest_by_role["planner"]["context_id"]))
    assert planner_record is not None
    assert "episode_hint" in planner_record.provenance_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("use_builder", [False, True])
@pytest.mark.parametrize(
    "objective",
    [
        "Review /etc/private/repository safely",
        "Review these details. " + "Bounded repository inspection. " * 100 + " ORIGINAL_TAIL",
    ],
    ids=["redacted-path", "long-objective"],
)
async def test_model_plan_binds_redacted_or_long_objective_without_echoing_raw_text(
    tmp_path: Path, use_builder: bool, objective: str
) -> None:
    db_path = tmp_path / "binding.db"
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    state = StateService(db_path, permission_policy=policy)
    await state.initialize()
    planner = _CapturingPlanner(_synthesis_plan("unused", suffix="bound"))
    evaluator = _CapturingEvaluator()
    manager = GoalManager(
        db_path,
        state_service=state,
        agent_dispatcher=AgentDispatcher(
            db_path, SQLiteMessageBoard(db_path), permission_policy=policy
        ),
        planner=planner,
        evaluator=evaluator,
        permission_policy=policy,
        context_builder=ContextBuilder(db_path) if use_builder else None,
    )
    await manager.initialize()
    goal = await manager.create_goal(GoalCreateRequest(objective=objective), actor_id="test-phone")
    detail = await manager.start_goal(str(goal["id"]), GoalStartRequest())

    assert detail["goal"]["objective"] == objective
    assert detail["goal"]["status"] == "running"
    assert len(detail["nodes"]) == 1
    payload = _canonical(planner.payloads[-1])
    assert f"goal:{goal['id']}" in payload
    assert "/etc/private" not in payload
    assert "ORIGINAL_TAIL" not in payload
    # The audit digest describes the actual model response, before the server
    # restores the original objective for authoritative plan validation.
    model_response = _synthesis_plan(f"goal:{goal['id']}", suffix="bound")
    expected_digest = hashlib.sha256(
        _canonical(model_response.model_dump(mode="json")).encode()
    ).hexdigest()
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute("SELECT output_digest FROM goal_model_calls WHERE role='planner'")
        ).fetchone()
    assert row == (expected_digest,)
