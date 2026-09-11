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
    if case == "mixed_synthesis":
        return [project, _node("summary", None)]
    if case == "mixed_legacy":
        return [project, _node("legacy", "code.generate_python")]
    if case == "two_projects":
        return [project, _node("other_project", "code.build_project")]
    project[case] = ["setup"]
    return [project, _node("setup", "workspace.list_dir")]


@pytest.mark.parametrize(
    "case",
    ["mixed_synthesis", "mixed_legacy", "two_projects", "dependencies", "optional_dependencies"],
)
def test_project_plan_rejects_mixed_nodes_without_repairing_input(
    policy: PermissionPolicy, case: str
) -> None:
    plan = _plan(_invalid_nodes(case))
    original = deepcopy(plan)
    with pytest.raises(PlanValidationError, match="project plan"):
        validate_swarm_plan(plan, policy=policy)
    with pytest.raises(PlanValidationError, match="project plan"):
        parse_swarm_plan_json(json.dumps(plan))
    assert plan == original


@pytest.mark.parametrize(
    "case",
    ["mixed_synthesis", "mixed_legacy", "two_projects", "dependencies", "optional_dependencies"],
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
def test_single_suggested_project_cannot_depend_on_existing_node(
    policy: PermissionPolicy, field: str
) -> None:
    node = _node("project", "code.build_project")
    node[field] = ["existing"]
    with pytest.raises(PlanValidationError, match="project plan"):
        validate_evaluation_decision(
            {
                "schema_version": "1.0",
                "status": "continue",
                "reason_summary": "Réaliser le projet",
                "missing_requirements": [],
                "invalid_results": [],
                "suggested_new_nodes": [node],
            },
            policy=policy,
            known_node_ids=["existing"],
        )


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


def test_planner_generation_schema_expresses_exclusive_project_shape() -> None:
    before = SwarmPlanProposal.model_json_schema()
    schema = UbuntuSwarmPlannerProvider._response_format()["json_schema"]["schema"]
    validator = Draft202012Validator(schema)
    assert validator.is_valid(_plan([_node("project", "code.build_project")]))
    assert validator.is_valid(valid_plan())
    for skill in sorted(SUPPORTED_AGENT_SKILLS - {"code.build_project"}):
        assert validator.is_valid(_plan([_node("worker", skill), _node("summary", None)]))
    for case in (
        "mixed_synthesis",
        "mixed_legacy",
        "two_projects",
        "dependencies",
        "optional_dependencies",
    ):
        assert not validator.is_valid(_plan(_invalid_nodes(case))), case
    for field in ("dependencies", "optional_dependencies"):
        node = _node("project", "code.build_project")
        node[field] = ["existing"]
        assert not validator.is_valid(_plan([node]))
    assert SwarmPlanProposal.model_json_schema() == before


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
