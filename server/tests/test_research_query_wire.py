from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft202012Validator

from app.services.evaluator_provider import EvaluatorProviderError, UbuntuEvaluatorProvider
from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import (
    MAX_PROPOSAL_BYTES,
    PlanValidationError,
    parse_evaluation_json,
    parse_swarm_plan_json,
)
from app.services.planner_provider import SwarmPlannerProviderError, UbuntuSwarmPlannerProvider
from app.services.swarm_contracts import EvaluationDecision, SwarmPlanProposal
from tests.test_evaluator import (
    REPO_ROOT,
    _response_for,
    continue_decision,
    evaluation_context,
    wire_decision,
)
from tests.test_planner_provider import _planner_context, _proposal

Kind = Literal["planner", "evaluator"]
QUERY = "bibliothèques Sorel-Tracy services"


def public_response(kind: Kind, skill: str | None = "research.query") -> dict[str, Any]:
    result = deepcopy(_proposal(skill) if kind == "planner" else continue_decision())
    field = "nodes" if kind == "planner" else "suggested_new_nodes"
    result[field][0].update(required_skill=skill, objective=QUERY)
    if skill is None:
        result[field][0].update(node_type="synthesis", preferred_agent_constraints=None)
    return result


def wire_response(kind: Kind, value: dict[str, Any]) -> dict[str, Any]:
    wire = deepcopy(value if kind == "planner" else wire_decision(value))
    field = "nodes" if kind == "planner" else "50_suggested_new_nodes"
    for node in wire[field]:
        if node["required_skill"] == "research.query":
            node["search_query"] = node.pop("objective")
    return wire


def wire_nodes(kind: Kind, value: dict[str, Any]) -> list[dict[str, Any]]:
    return value["nodes" if kind == "planner" else "50_suggested_new_nodes"]


def grammar(kind: Kind, skills: list[str]) -> Draft202012Validator:
    response = (
        UbuntuSwarmPlannerProvider._response_format(available_skills=skills)
        if kind == "planner"
        else UbuntuEvaluatorProvider._response_format(skills)
    )
    schema = response["json_schema"]["schema"]
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


async def through_provider(
    kind: Kind, content: str, skills: list[str] | None = None
) -> SwarmPlanProposal | EvaluationDecision:
    available = ["research.query", "writing.draft"] if skills is None else skills
    post = AsyncMock(return_value=_response_for(content))
    with patch("httpx.AsyncClient.post", post):
        if kind == "planner":
            context = _planner_context()
            context["cards"][-1]["skills"] = available
            provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:1/v1", model="test")
            result = await provider.propose(context)
        else:
            context = evaluation_context()
            context.available_skills = available
            evaluator = UbuntuEvaluatorProvider(
                base_url="http://127.0.0.1:1/v1",
                model="test",
                policy=PermissionPolicy.from_yaml(REPO_ROOT / "configs/permissions.yaml"),
            )
            result = await evaluator.evaluate(context)
    assert post.await_count == 1
    return result


@pytest.mark.parametrize("kind", ["planner", "evaluator"])
def test_research_grammar_requires_dedicated_query_field_and_preserves_public_schema(
    kind: Kind,
) -> None:
    public = public_response(kind)
    wire = wire_response(kind, public)
    validator = grammar(kind, ["research.query", "writing.draft"])
    assert validator.is_valid(wire)
    legacy = public if kind == "planner" else wire_decision(public)
    assert not validator.is_valid(legacy)
    mixed = deepcopy(wire)
    wire_nodes(kind, mixed)[0]["objective"] = "An instruction is not the query field."
    assert not validator.is_valid(mixed)
    # Transport aliases do not become accepted public API fields.
    parse = parse_swarm_plan_json if kind == "planner" else parse_evaluation_json
    public_with_alias = deepcopy(public)
    field = "nodes" if kind == "planner" else "suggested_new_nodes"
    node = public_with_alias[field][0]
    node["search_query"] = node.pop("objective")
    with pytest.raises(PlanValidationError):
        parse(json.dumps(public_with_alias))
    assert parse(json.dumps(public)).model_dump()[field][0]["objective"] == QUERY


@pytest.mark.parametrize("kind", ["planner", "evaluator"])
@pytest.mark.parametrize("skill", ["writing.draft", "workspace.read_text", None])
def test_nonresearch_grammar_keeps_objective_and_rejects_query_alias(
    kind: Kind, skill: str | None
) -> None:
    public = public_response(kind, skill)
    wire = wire_response(kind, public)
    validator = grammar(kind, ["research.query", "writing.draft", "workspace.read_text"])
    assert validator.is_valid(wire)
    node = wire_nodes(kind, wire)[0]
    node["search_query"] = node.pop("objective")
    assert not validator.is_valid(wire)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["planner", "evaluator"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_new_wire_and_legacy_response_produce_identical_public_contract(
    kind: Kind, legacy: bool
) -> None:
    public = public_response(kind)
    wire = (
        (public if kind == "planner" else wire_decision(public))
        if legacy
        else wire_response(kind, public)
    )
    actual = await through_provider(kind, json.dumps(wire))
    model = SwarmPlanProposal if kind == "planner" else EvaluationDecision
    assert actual == model.model_validate(public)
    assert "search_query" not in actual.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["planner", "evaluator"])
@pytest.mark.parametrize(
    "case",
    [
        "mixed",
        "wrong_skill",
        "wrong_type",
        "unknown_field",
        "top_level_alias",
        "missing_query",
        "empty",
        "not_text",
        "too_long",
        "duplicate",
        "overflowing_number",
    ],
)
async def test_invalid_research_alias_response_is_rejected_without_repair(
    kind: Kind, case: str
) -> None:
    wire = wire_response(kind, public_response(kind))
    node = wire_nodes(kind, wire)[0]
    if case == "mixed":
        node["objective"] = "private second instruction"
    elif case == "wrong_skill":
        node["required_skill"] = "writing.draft"
    elif case == "wrong_type":
        node.update(node_type="synthesis", required_skill=None)
    elif case == "unknown_field":
        node["execute"] = "private command"
    elif case == "top_level_alias":
        wire["search_query"] = "private top-level instruction"
    elif case == "missing_query":
        node.pop("search_query")
    elif case == "empty":
        node["search_query"] = " \t "
    elif case == "not_text":
        node["search_query"] = {"objective": "private nested instruction"}
    elif case == "too_long":
        node["search_query"] = "x" * 4001
    content = json.dumps(wire)
    if case == "duplicate":
        content = content.replace(
            '"search_query": ', '"search_query": "private duplicate", "search_query": ', 1
        )
    elif case == "overflowing_number":
        content = content.replace('"priority": 50', '"priority": 1e999', 1)
    with pytest.raises((SwarmPlannerProviderError, EvaluatorProviderError)) as failed:
        await through_provider(kind, content)
    assert failed.value.category == "invalid_response"
    assert "private" not in str(failed.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["planner", "evaluator"])
async def test_research_alias_does_not_bypass_advertised_capabilities(kind: Kind) -> None:
    wire = wire_response(kind, public_response(kind))
    assert not grammar(kind, ["writing.draft"]).is_valid(wire)
    with pytest.raises((SwarmPlannerProviderError, EvaluatorProviderError)):
        await through_provider(kind, json.dumps(wire), ["writing.draft"])


@pytest.mark.asyncio
async def test_compact_legacy_plan_near_transport_limit_is_not_inflated_by_reencoding() -> None:
    public = public_response("planner")
    public["nodes"] = [
        {
            **public["nodes"][0],
            "temporary_id": f"research_{index}",
            "objective": "q" * 4000,
            "expected_output": "e" * 4000,
            "title": "t" * 400,
        }
        for index in range(15)
    ]
    public["rationale_summary"] = "x"
    compact = json.dumps(public, separators=(",", ":"))
    padding = MAX_PROPOSAL_BYTES - len(compact.encode())
    assert 0 < padding < 4000
    public["rationale_summary"] += "x" * padding
    compact = json.dumps(public, separators=(",", ":"))
    assert len(compact.encode()) == MAX_PROPOSAL_BYTES
    assert len(json.dumps(public).encode()) > MAX_PROPOSAL_BYTES
    expected = parse_swarm_plan_json(compact)
    assert await through_provider("planner", compact) == expected
    # The original JSON size limit remains authoritative before normalization.
    public["rationale_summary"] += "x"
    with pytest.raises(SwarmPlannerProviderError) as failed:
        await through_provider("planner", json.dumps(public, separators=(",", ":")))
    assert failed.value.category == "invalid_response"
