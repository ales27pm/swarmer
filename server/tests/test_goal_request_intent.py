from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.evaluator_provider import UbuntuEvaluatorProvider
from app.services.planner_provider import UbuntuSwarmPlannerProvider
from app.services.swarm_contracts import GoalCreateRequest
from tests.test_evaluator import evaluation_context
from tests.test_goal_manager import _manager, _parallel_plan

LEGACY_CRITERION = "Provide an evidence-backed response to the objective"


def _response(proposal: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions"),
        json={"choices": [{"message": {"content": json.dumps(proposal)}}]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "objective", ["Crées une Application CRM en python", "Compare the supplied research findings"]
)
async def test_default_criterion_preserves_requested_outcome_without_spending_budget(
    tmp_path: Path, objective: str
) -> None:
    manager = await _manager(tmp_path, _parallel_plan(objective=objective))
    goal = await manager.create_goal(GoalCreateRequest(objective=objective), actor_id="phone")
    saved = await manager.graph.get_goal(goal["id"])
    assert saved is not None
    assert saved["objective"] == objective
    assert saved["completion_criteria"] == [
        (
            "Fulfill the user's requested outcome, preserving the requested deliverables and "
            "actions, with evidence for each claimed result."
        )
    ]
    assert saved["model_call_count"] == saved["step_count"] == 0


@pytest.mark.asyncio
async def test_explicit_completion_criteria_remain_authoritative(tmp_path: Path) -> None:
    manager = await _manager(tmp_path, _parallel_plan())
    criteria = ["Répondre en français avec les résultats vérifiés."]
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Comparer les résultats", completion_criteria=criteria),
        actor_id="phone",
    )
    assert goal["completion_criteria"] == criteria


@pytest.mark.asyncio
async def test_planner_wire_prioritizes_user_goal_over_legacy_generic_criterion() -> None:
    # Minimal reproduction of the recorded failure: the original French goal,
    # the old generic criterion, historical evidence and BOTH available skills.
    context = {
        "schema_version": "1.0",
        "purpose": "planner",
        "cards": [
            {
                "card_id": "goal:goal_current",
                "kind": "goal",
                "summary": f"objective=Crées une Application CRM en python; criteria={LEGACY_CRITERION}",
            },
            {
                "card_id": "episode:older",
                "kind": "episode",
                "summary": "No evidence summary was available for synthesis.",
            },
            {
                "card_id": "agent:legacy:1",
                "kind": "agent_card",
                "summary": "status=online; skills=code.generate_python",
            },
            {
                "card_id": "agent:project:1",
                "kind": "agent_card",
                "summary": "status=online; skills=code.build_project",
            },
        ],
    }
    proposal = {
        "schema_version": "1.0",
        "objective": "goal:goal_current",
        "rationale_summary": "Le développeur de projet réalisera l'application demandée.",
        "completion_criteria": ["Application CRM réalisée et vérifiée"],
        "max_parallelism": 1,
        "nodes": [
            {
                "temporary_id": "project",
                "node_type": "worker",
                "title": "Créer l'application CRM",
                "objective": "Crées une Application CRM en python",
                "required_skill": "code.build_project",
                "dependencies": [],
                "expected_output": "Projet complet avec fichiers et résultats des tests",
                "priority": 1,
            }
        ],
    }
    post = AsyncMock(return_value=_response(proposal))
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:11434/v1", model="local")
    with patch("httpx.AsyncClient.post", post):
        result = await provider.propose(context)
    post.assert_awaited_once()
    request = post.await_args.kwargs["json"]
    assert json.loads(request["messages"][1]["content"]) == context
    instruction = request["messages"][0]["content"]
    assert (
        "Generic completion criteria and historical context cannot replace or weaken it"
        in instruction
    )
    assert "create exactly one code.build_project worker node with no dependencies" in instruction
    assert "Do not use a synthesis-only plan" in instruction
    assert "Choose routine implementation details yourself" in instruction
    assert "in the user's language" in instruction
    assert result.nodes[0].required_skill == "code.build_project"
    assert result.objective == "goal:goal_current"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert not {"tools", "tool_choice", "execution"}.intersection(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "question"),
    [("replan", None), ("needs_user", "Les contacts doivent-ils être partagés par une équipe ?")],
)
async def test_evaluator_wire_keeps_implementation_work_with_agent(
    tmp_path: Path, status: str, question: str | None
) -> None:
    manager = await _manager(tmp_path, _parallel_plan())
    context = evaluation_context().model_copy(
        update={
            "objective": "Crées une Application CRM en python",
            "completion_criteria": [LEGACY_CRITERION],
            "node_results": [
                evaluation_context()
                .node_results[0]
                .model_copy(
                    update={
                        "title": "Synthesis",
                        "expected_output": LEGACY_CRITERION,
                        "result_summary": "No evidence summary was available for synthesis.",
                    }
                )
            ],
        }
    )
    proposal = {
        "schema_version": "1.0",
        "status": status,
        "reason_summary": "L'application demandée reste à réaliser.",
        "missing_requirements": ["Projet CRM en Python avec des résultats vérifiables"],
        "invalid_results": [],
        "suggested_new_nodes": [],
        "user_question": question,
        "completion_summary": None,
    }
    post = AsyncMock(return_value=_response(proposal))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:11434/v1", model="local", policy=manager.permission_policy
    )
    with patch("httpx.AsyncClient.post", post):
        result = await provider.evaluate(context)
    post.assert_awaited_once()
    request = post.await_args.kwargs["json"]
    assert json.loads(request["messages"][1]["content"]) == context.model_dump(mode="json")
    instruction = request["messages"][0]["content"]
    assert "Use needs_user only when missing material product requirements" in instruction
    assert "Do not ask the user how to set up a development environment" in instruction
    assert "choose a framework or libraries" in instruction
    assert "in the user's language" in instruction
    assert (
        "A synthesis with no implementation evidence does not fulfill an application request"
        in instruction
    )
    assert result.status == status and result.user_question == question
    assert request["response_format"]["json_schema"]["strict"] is True
    assert not {"tools", "tool_choice", "execution"}.intersection(request)
