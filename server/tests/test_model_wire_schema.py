from copy import deepcopy
from typing import Annotated, Any
from unittest.mock import patch

import pytest
from app.services.evaluator_provider import UbuntuEvaluatorProvider
from app.services.model_wire_schema import model_wire_schema
from app.services.planner_provider import UbuntuSwarmPlannerProvider
from app.services.swarm_contracts import EvaluationDecision, SwarmPlanProposal
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field


@pytest.mark.parametrize("model", [SwarmPlanProposal, EvaluationDecision])
def test_wire_schema_preserves_authoritative_schema_and_other_constraints(
    model: type[BaseModel],
) -> None:
    source = model.model_json_schema()
    original = deepcopy(source)
    with patch.object(model, "model_json_schema", return_value=source):
        wire = model_wire_schema(model)

    assert source == original
    assert wire is not source
    assert wire["additionalProperties"] is False
    assert wire["required"] == source["required"]
    node = wire["$defs"]["SwarmPlanNodeProposal"]["properties"]
    assert node["objective"]["minLength"] == 1
    assert "maxLength" not in node["objective"]
    assert source["$defs"]["SwarmPlanNodeProposal"]["properties"]["objective"]["maxLength"] == 4000
    assert node["temporary_id"]["pattern"] == "^[A-Za-z][A-Za-z0-9_-]{0,63}$"
    assert node["dependencies"]["maxItems"] == 20
    assert node["priority"]["minimum"] == 0
    assert node["priority"]["maximum"] == 100
    assert node["required_skill"]["anyOf"][1] == {"type": "null"}
    # Mutating the wire schema cannot change cached/caller-owned source data.
    node["objective"]["minLength"] = 0
    assert source == original


@pytest.mark.parametrize("model", [SwarmPlanProposal, EvaluationDecision])
@pytest.mark.parametrize("field", ["temporary_id", "dependencies", "optional_dependencies"])
@pytest.mark.parametrize("length", [1, 64, 65, 67, 74])
def test_wire_identifier_pattern_enforces_length_without_sibling_max_length(
    model: type[BaseModel], field: str, length: int
) -> None:
    properties = model_wire_schema(model)["$defs"]["SwarmPlanNodeProposal"]["properties"]
    identifier = properties[field] if field == "temporary_id" else properties[field]["items"]
    assert "maxLength" not in identifier
    # Validate the pattern alone: local converters need not combine string bounds.
    validator = Draft202012Validator({"type": "string", "pattern": identifier["pattern"]})
    assert validator.is_valid("n" * length) is (length <= 64)


@pytest.mark.parametrize(
    "value,valid",
    [
        ("a", True),
        ("Z_0-A", True),
        ("", False),
        ("0abc", False),
        ("é", False),
        ("node.id", False),
        ("node/child", False),
        ("node child", False),
    ],
)
def test_bounded_identifier_keeps_the_public_character_set(value: str, valid: bool) -> None:
    schema = model_wire_schema(SwarmPlanProposal)["$defs"]["SwarmPlanNodeProposal"]
    assert Draft202012Validator(schema["properties"]["temporary_id"]).is_valid(value) is valid


def test_identifier_quantifier_comes_from_its_declared_bound_only() -> None:
    class Example(BaseModel):
        identifier: Annotated[str, Field(max_length=8, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
        prose: Annotated[str, Field(max_length=4_000)]
        other_pattern: Annotated[str, Field(max_length=64, pattern=r"^[a-z]*$")]

    public = Example.model_json_schema()
    wire = model_wire_schema(Example)
    properties = wire["properties"]
    assert properties["identifier"]["pattern"] == "^[A-Za-z][A-Za-z0-9_-]{0,7}$"
    assert properties["other_pattern"]["pattern"] == "^[a-z]*$"
    assert all("maxLength" not in field for field in properties.values())
    assert public == Example.model_json_schema()
    assert public["properties"]["prose"]["maxLength"] == 4_000


@pytest.mark.parametrize("provider", ["planner", "evaluator"])
@pytest.mark.parametrize("field", ["temporary_id", "dependencies", "optional_dependencies"])
def test_actual_provider_grammars_reject_overlong_ids_in_worker_nodes(
    provider: str, field: str
) -> None:
    node: dict[str, Any] = {
        "00_required_skill": "writing.draft",
        "temporary_id": "draft",
        "node_type": "worker",
        "title": "Draft",
        "objective": "Write a brief overview.",
        "dependencies": [],
        "optional_dependencies": [],
        "expected_output": "Overview",
        "priority": 1,
    }
    if provider == "planner":
        schema = UbuntuSwarmPlannerProvider._response_format(available_skills=["writing.draft"])[
            "json_schema"
        ]["schema"]
        value = {
            "schema_version": "1.0",
            "objective": "Overview",
            "rationale_summary": "Write.",
            "nodes": [node],
            "completion_criteria": ["Overview"],
            "max_parallelism": 1,
        }
    else:
        schema = UbuntuEvaluatorProvider._response_format(["writing.draft"])["json_schema"][
            "schema"
        ]
        value = {
            "00_schema_version": "1.0",
            "10_invalid_results": [],
            "20_missing_requirements": ["Overview"],
            "30_reason_summary": "A draft is needed.",
            "40_status": "replan",
            "50_suggested_new_nodes": [node],
            "60_user_question": None,
            "70_completion_summary": None,
        }
    validator = Draft202012Validator(schema)
    for length in (1, 64, 65, 67, 74):
        node[field] = "n" * length if field == "temporary_id" else ["n" * length]
        assert validator.is_valid(value) is (length <= 64)
