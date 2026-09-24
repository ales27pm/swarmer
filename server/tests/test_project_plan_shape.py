from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from jsonschema import Draft202012Validator

from app.services.agent_card import SUPPORTED_AGENT_SKILLS
from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import (
    PlanValidationError,
    parse_swarm_plan_json,
    validate_evaluation_decision,
    validate_swarm_plan,
)
from app.services.planner_diagnostics import DIAGNOSTICS
from app.services.planner_provider import SwarmPlannerProviderError, UbuntuSwarmPlannerProvider
from app.services.swarm_contracts import SwarmPlanProposal
from tests.test_plan_validation import valid_plan


@pytest.fixture
def policy() -> PermissionPolicy:
    return PermissionPolicy.from_yaml(
        Path(__file__).resolve().parents[2] / "configs/permissions.yaml"
    )


def _node(identifier: str, skill: str | None) -> dict[str, Any]:
    return {
        "temporary_id": identifier,
        "node_type": "worker" if skill else "synthesis",
        "title": "Réaliser le projet" if skill else "Synthèse",
        "objective": "Créer une application utile et vérifiée",
        "required_skill": skill,
        "dependencies": [],
        "optional_dependencies": [],
        "expected_output": "Résultats vérifiables",
        "priority": 1,
    }


def _plan(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "objective": "Créer une application",
        "rationale_summary": "Réaliser le travail demandé",
        "nodes": nodes,
        "completion_criteria": ["Projet réalisé et vérifié"],
        "max_parallelism": 1,
    }


def _invalid_nodes(case: str) -> list[dict[str, Any]]:
    project = _node("project", "code.build_project")
    if case == "mixed_legacy":
        return [project, _node("legacy", "code.generate_python")]
    if case == "two_projects":
        return [project, _node("other_project", "code.build_project")]
    return [_node("legacy", "code.generate_python"), _node("other_legacy", "code.generate_python")]


def _mixed_nodes(case: str, skill: str = "code.build_project") -> list[dict[str, Any]]:
    project = _node("project", skill)
    if case == "synthesis":
        summary = _node("summary", None)
        summary["dependencies"] = ["project"]
        return [project, summary]
    setup = _node("setup", "research.query")
    if case != "independent":
        project[case] = ["setup"]
    return [setup, project]


@pytest.mark.parametrize(
    "case",
    ["mixed_legacy", "two_projects", "two_legacy"],
)
def test_project_plan_rejects_multiple_mutators_without_repairing_input(
    policy: PermissionPolicy, case: str
) -> None:
    plan = _plan(_invalid_nodes(case))
    original = deepcopy(plan)
    with pytest.raises(PlanValidationError, match="project plan"):
        validate_swarm_plan(plan, policy=policy)
    with pytest.raises(PlanValidationError, match="project plan"):
        parse_swarm_plan_json(json.dumps(plan))
    assert plan == original


def test_duplicate_project_diagnostic_preserves_mixed_plan_options() -> None:
    explanation, correction = DIAGNOSTICS["project_plan_shape"]
    assert "avec les dépendances nécessaires" in explanation
    assert "at most one project-mutating worker" in correction
    assert "code.build_project and code.generate_python combined" in correction
    assert "other capabilities and required dependencies are allowed" in correction


@pytest.mark.parametrize(
    "case",
    ["mixed_legacy", "two_projects", "two_legacy"],
)
def test_evaluator_suggested_nodes_share_project_plan_rule(
    policy: PermissionPolicy, case: str
) -> None:
    decision = {
        "schema_version": "1.0",
        "status": "replan",
        "reason_summary": "Réaliser le projet",
        "missing_requirements": [],
        "invalid_results": [],
        "suggested_new_nodes": _invalid_nodes(case),
    }
    with pytest.raises(PlanValidationError, match="project plan"):
        validate_evaluation_decision(decision, policy=policy)


@pytest.mark.parametrize("field", ["dependencies", "optional_dependencies"])
def test_single_suggested_project_can_depend_on_verified_existing_node_id(
    policy: PermissionPolicy, field: str
) -> None:
    node = _node("project", "code.build_project")
    node[field] = ["existing"]
    decision = {
        "schema_version": "1.0",
        "status": "continue",
        "reason_summary": "Réaliser le projet",
        "missing_requirements": [],
        "invalid_results": [],
        "suggested_new_nodes": [node],
    }
    result = validate_evaluation_decision(decision, policy=policy, known_node_ids=["existing"])
    assert result.suggested_node_order == ("project",)
    with pytest.raises(PlanValidationError, match="unknown dependency"):
        validate_evaluation_decision(decision, policy=policy)


@pytest.mark.parametrize(
    ("skill", "case"),
    [
        ("code.build_project", case)
        for case in ("synthesis", "dependencies", "optional_dependencies", "independent")
    ]
    + [("code.generate_python", case) for case in ("synthesis", "independent")],
)
def test_mixed_project_dag_preserves_dependencies_and_requested_work(
    policy: PermissionPolicy, skill: str, case: str
) -> None:
    plan = _plan(_mixed_nodes(case, skill))
    plan["max_parallelism"] = 2
    original = deepcopy(plan)
    validated = validate_swarm_plan(plan, policy=policy)
    parsed = parse_swarm_plan_json(json.dumps(plan))
    assert [node.model_dump() for node in parsed.nodes] == [
        node.model_dump() for node in validated.proposal.nodes
    ]
    assert plan == original
    if case in {"dependencies", "optional_dependencies"}:
        assert validated.topological_order == ("setup", "project")
    decision = {
        "schema_version": "1.0",
        "status": "replan",
        "reason_summary": "Poursuivre",
        "missing_requirements": [],
        "invalid_results": [],
        "suggested_new_nodes": plan["nodes"],
    }
    assert (
        validate_evaluation_decision(decision, policy=policy).suggested_node_order
        == validated.topological_order
    )


@pytest.mark.parametrize("case", ["missing", "cycle", "self"])
def test_mixed_project_plan_keeps_dependency_rejections(
    policy: PermissionPolicy, case: str
) -> None:
    nodes = _mixed_nodes("dependencies")
    if case == "missing":
        nodes[1]["dependencies"] = ["absent"]
    elif case == "self":
        nodes[1]["dependencies"] = ["project"]
    else:
        nodes[0]["dependencies"] = ["project"]
    with pytest.raises(PlanValidationError, match="dependency|dependencies|itself"):
        validate_swarm_plan(_plan(nodes), policy=policy)


def test_single_project_and_general_dag_remain_valid(policy: PermissionPolicy) -> None:
    project = _plan([_node("project", "code.build_project")])
    assert validate_swarm_plan(project, policy=policy).topological_order == ("project",)
    assert (
        parse_swarm_plan_json(json.dumps(project)).nodes[0].required_skill == "code.build_project"
    )
    assert validate_swarm_plan(valid_plan(), policy=policy).topological_order == (
        "inventory",
        "readme",
        "synthesis",
    )


def test_planner_generation_schema_supports_mixed_project_dags_with_server_single_writer_guard() -> (
    None
):
    before = SwarmPlanProposal.model_json_schema()
    schema = UbuntuSwarmPlannerProvider._response_format()["json_schema"]["schema"]
    validator = Draft202012Validator(schema)

    def wire(plan: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(plan)
        for node in result["nodes"]:
            skill = node.pop("required_skill")
            node["00_required_skill"] = skill
            if skill == "research.query":
                node["search_query"] = node.pop("objective")
        return result

    assert validator.is_valid(wire(_plan([_node("project", "code.build_project")])))
    assert validator.is_valid(wire(valid_plan()))
    from app.services.specialist_contracts import SPECIALIST_SKILLS

    for skill in sorted(SUPPORTED_AGENT_SKILLS - {"code.build_project"} - SPECIALIST_SKILLS):
        assert validator.is_valid(wire(_plan([_node("worker", skill), _node("summary", None)])))
    for case in ("synthesis", "dependencies", "optional_dependencies", "independent"):
        assert validator.is_valid(wire(_plan(_mixed_nodes(case)))), case
    # Use the supported items.anyOf grammar. Cross-item uniqueness is enforced
    # by the independent server parser, not unsupported contains/maxContains.
    for case in ("mixed_legacy", "two_projects", "two_legacy"):
        duplicate = _plan(_invalid_nodes(case))
        assert validator.is_valid(wire(duplicate))
        with pytest.raises(PlanValidationError, match="project plan"):
            parse_swarm_plan_json(json.dumps(duplicate))
    assert SwarmPlanProposal.model_json_schema() == before


@pytest.mark.asyncio
async def test_planner_accepts_mixed_dependency_output_without_rewriting_it() -> None:
    skill = "code.build_project"
    proposal = _plan(_mixed_nodes("dependencies", skill))
    proposal["objective"] = "goal:goal_current"
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions"),
        json={"choices": [{"message": {"content": json.dumps(proposal)}}]},
    )
    post = AsyncMock(return_value=response)
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:11434/v1", model="local")
    with patch("httpx.AsyncClient.post", post):
        result = await provider.propose(
            {
                "cards": [
                    {"kind": "goal", "card_id": "goal:goal_current", "summary": "Créer un projet"},
                    {"kind": "agent_card", "skills": ["research.query", skill]},
                ]
            }
        )
    assert [node.required_skill for node in result.nodes] == ["research.query", skill]
    assert result.nodes[1].dependencies == ["setup"]
    assert result.nodes[1].objective == proposal["nodes"][1]["objective"]
    with pytest.raises(PlanValidationError, match="absent from available capabilities"):
        parse_swarm_plan_json(json.dumps(proposal), available_skills=[skill])
    post.assert_awaited_once()


@pytest.mark.parametrize("field", ["dependencies", "optional_dependencies"])
def test_legacy_generator_cannot_claim_it_consumes_other_worker_results(
    policy: PermissionPolicy, field: str
) -> None:
    proposal = _plan(_mixed_nodes(field, "code.generate_python"))
    with pytest.raises(PlanValidationError, match="legacy.*dependencies"):
        validate_swarm_plan(proposal, policy=policy)
    with pytest.raises(PlanValidationError, match="legacy.*dependencies"):
        parse_swarm_plan_json(json.dumps(proposal))
    decision = {
        "schema_version": "1.0",
        "status": "continue",
        "reason_summary": "Proceed",
        "missing_requirements": [],
        "invalid_results": [],
        "suggested_new_nodes": proposal["nodes"],
    }
    with pytest.raises(PlanValidationError, match="legacy.*dependencies"):
        validate_evaluation_decision(decision, policy=policy)
    schema = UbuntuSwarmPlannerProvider._response_format()["json_schema"]["schema"]
    wire = deepcopy(proposal)
    for item in wire["nodes"]:
        item["00_required_skill"] = item.pop("required_skill")
        if item["00_required_skill"] == "research.query":
            item["search_query"] = item.pop("objective")
    assert not Draft202012Validator(schema).is_valid(wire)


@pytest.mark.asyncio
async def test_planner_rejects_mixed_output_even_when_transport_ignores_schema() -> None:
    proposal = _plan(_invalid_nodes("mixed_legacy"))
    proposal["objective"] = "goal:goal_current"
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions"),
        json={"choices": [{"message": {"content": json.dumps(proposal)}}]},
    )
    post = AsyncMock(return_value=response)
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:11434/v1", model="local")
    with patch("httpx.AsyncClient.post", post), pytest.raises(SwarmPlannerProviderError) as raised:
        await provider.propose(
            {
                "cards": [
                    {"kind": "goal", "card_id": "goal:goal_current", "summary": "Créer un projet"}
                ]
            }
        )
    assert raised.value.category == "invalid_response"
    assert isinstance(raised.value.__cause__, PlanValidationError)
    post.assert_awaited_once()
