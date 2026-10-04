"""Focused tests of the counted grammar, independent of transport/model behavior."""

import json
from copy import deepcopy
from itertools import product
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from app.services.planner_graph_wire import constrain_planner_graph, decode_planner_graph
from app.services.swarm_contracts import MAX_PLAN_NODES

MUTATORS = ("code.build_project", "code.generate_python")


def _source(skills: tuple[str | None, ...]) -> dict[str, Any]:
    """Small capability-specific input fixture at the graph transform boundary."""
    branches = []
    for skill in skills:
        dependencies: dict[str, Any] = {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_PLAN_NODES,
        }
        optional = deepcopy(dependencies)
        if skill == "code.generate_python":
            dependencies["maxItems"] = optional["maxItems"] = 0
        if skill is None:
            dependencies["minItems"] = 1
        properties = {
            "00_required_skill": {"const": skill, "type": "string" if skill else "null"},
            "node_type": {"const": "worker" if skill else "synthesis", "type": "string"},
            "temporary_id": {"type": "string"},
            "dependencies": dependencies,
            "optional_dependencies": optional,
            "objective": {"type": "string", "minLength": 1},
        }
        branches.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": list(properties),
            }
        )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["nodes"],
        "properties": {"nodes": {"type": "array", "items": {"anyOf": branches}}},
    }


def _wire(skills: tuple[str | None, ...]) -> dict[str, Any]:
    steps = {
        f"step_{index:02d}": {
            "00_temporary_id": f"step_{index}",
            "01_node": {
                "00_required_skill": skill,
                "node_type": "worker" if skill else "synthesis",
                "objective": "Build a small React Native Pong web game",
            },
            "02_dependencies": [],
            "03_optional_dependencies": [],
        }
        for index, skill in enumerate(skills, start=1)
    }
    return {"nodes": {"00_node_count": len(skills), "01_steps": steps}}


def _validator(schema: dict[str, Any], count: int) -> Draft202012Validator:
    # Exercise exactly the selected count to keep exhaustive unit checks cheap.
    # End-to-end response-format coverage lives in test_project_plan_shape.py.
    branch = next(
        item
        for item in schema["properties"]["nodes"]["anyOf"]
        if item["properties"]["00_node_count"]["const"] == count
    )
    return Draft202012Validator({"$defs": schema["$defs"], **branch})


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_all_small_skill_sequences_match_the_single_mutator_rule(count: int) -> None:
    skills = ("writing.draft", *MUTATORS)
    source = _source(skills)
    before = deepcopy(source)
    schema = constrain_planner_graph(source)
    validator = _validator(schema, count)
    for sequence in product(skills, repeat=count):
        wire = _wire(sequence)
        original = deepcopy(wire)
        expected = sum(skill in MUTATORS for skill in sequence) <= 1
        assert validator.is_valid(wire["nodes"]) == expected, sequence
        assert wire == original
    assert source == before


@pytest.mark.parametrize("skill", MUTATORS)
@pytest.mark.parametrize("position", [1, 10, MAX_PLAN_NODES])
def test_single_mutator_can_be_anywhere_in_a_full_plan(skill: str, position: int) -> None:
    schema = constrain_planner_graph(_source(("writing.draft", *MUTATORS)))
    skills = ["writing.draft"] * MAX_PLAN_NODES
    skills[position - 1] = skill
    wire = _wire(tuple(skills))
    assert _validator(schema, MAX_PLAN_NODES).is_valid(wire["nodes"])
    assert len(decode_planner_graph(wire)["nodes"]) == MAX_PLAN_NODES
    # A distant extra mutator is still rejected, not just adjacent duplicates.
    skills[0 if position != 1 else -1] = skill
    assert not _validator(schema, MAX_PLAN_NODES).is_valid(_wire(tuple(skills))["nodes"])


@pytest.mark.parametrize("field", ["02_dependencies", "03_optional_dependencies"])
def test_research_project_and_downstream_synthesis_preserve_inputs(field: str) -> None:
    schema = constrain_planner_graph(_source(("research.collect", "code.build_project", None)))
    wire = _wire(("research.collect", "code.build_project", None))
    steps = wire["nodes"]["01_steps"]
    steps["step_02"][field] = ["step_1"]
    steps["step_03"]["02_dependencies"] = ["step_2"]
    original = deepcopy(wire)
    assert Draft202012Validator(schema).is_valid(wire)
    decoded = decode_planner_graph(wire)["nodes"]
    public_field = "dependencies" if field == "02_dependencies" else "optional_dependencies"
    assert decoded[1][public_field] == ["step_1"]
    assert decoded[2]["dependencies"] == ["step_2"]
    assert decoded[1]["objective"] == "Build a small React Native Pong web game"
    assert wire == original


@pytest.mark.parametrize("field", ["02_dependencies", "03_optional_dependencies"])
def test_legacy_inputs_are_not_allowed_while_independent_work_is(field: str) -> None:
    schema = constrain_planner_graph(_source(("research.collect", "code.generate_python")))
    wire = _wire(("research.collect", "code.generate_python"))
    assert Draft202012Validator(schema).is_valid(wire)
    wire["nodes"]["01_steps"]["step_02"][field] = ["step_1"]
    assert not Draft202012Validator(schema).is_valid(wire)


@pytest.mark.parametrize("field", ["02_dependencies", "03_optional_dependencies"])
@pytest.mark.parametrize("target", ["step_2", "step_3", "goal_current"])
def test_mutator_slot_does_not_allow_self_future_or_unknown_inputs(
    field: str, target: str
) -> None:
    schema = constrain_planner_graph(_source(("writing.draft", "code.build_project")))
    wire = _wire(("writing.draft", "code.build_project"))
    wire["nodes"]["01_steps"]["step_02"][field] = [target]
    assert not _validator(schema, 2).is_valid(wire["nodes"])


def test_unavailable_mutators_stay_unavailable_and_no_mutator_plan_is_valid() -> None:
    schema = constrain_planner_graph(_source(("writing.draft",)))
    assert Draft202012Validator(schema).is_valid(_wire(("writing.draft", "writing.draft")))
    for skill in MUTATORS:
        assert not Draft202012Validator(schema).is_valid(_wire((skill,)))


def test_new_definition_names_cannot_overwrite_source_definitions() -> None:
    source = _source(("writing.draft", "code.build_project"))
    source["$defs"] = {"Step1NonProject": {"type": "integer"}}
    before = deepcopy(source)
    with pytest.raises(ValueError, match="definitions collide"):
        constrain_planner_graph(source)
    assert source == before


def test_mixed_skill_branch_fails_closed_without_changing_the_source() -> None:
    source = _source(("writing.draft",))
    branch = source["properties"]["nodes"]["items"]["anyOf"][0]
    branch["properties"]["00_required_skill"] = {
        "type": "string",
        "enum": ["writing.draft", "code.build_project"],
    }
    before = deepcopy(source)
    with pytest.raises(ValueError, match="must separate"):
        constrain_planner_graph(source)
    assert source == before


def test_schema_uses_supported_branches_and_keeps_capability_bodies_shared() -> None:
    schema = constrain_planner_graph(_source(("research.collect", "writing.draft", *MUTATORS, None)))
    Draft202012Validator.check_schema(schema)
    encoded = json.dumps(schema)
    assert len(encoded.encode()) < 350_000
    assert encoded.count('"const": "research.collect"') == 1
    assert '"contains"' not in encoded
    assert '"maxContains"' not in encoded
    assert len(schema["properties"]["nodes"]["anyOf"]) == MAX_PLAN_NODES


@pytest.mark.parametrize("field", ["dependencies", "optional_dependencies"])
def test_server_legacy_diagnostic_is_specific_and_does_not_discard_inputs(
    field: str,
) -> None:
    from types import SimpleNamespace

    from app.services.plan_validation import PlanValidationError, _validate_project_plan_shape
    from app.services.planner_diagnostics import DIAGNOSTICS, rejection_diagnostic, rejection_reason
    from app.services.swarm_contracts import PlanNodeType

    node: Any = SimpleNamespace(
        required_skill="code.generate_python",
        node_type=PlanNodeType.WORKER,
        dependencies=[],
        optional_dependencies=[],
    )
    setattr(node, field, ["source"])
    with pytest.raises(PlanValidationError) as raised:
        _validate_project_plan_shape([node])
    assert raised.value.diagnostic_code == "legacy_code_dependencies"
    assert getattr(node, field) == ["source"]
    wrapper = RuntimeError("untrusted transport/model text")
    wrapper.__cause__ = raised.value
    assert rejection_diagnostic(wrapper) == "legacy_code_dependencies"
    message = rejection_reason("Plan rejeté.", raised.value.diagnostic_code)
    assert "Python" in message
    assert "untrusted" not in message
    assert "Never drop required inputs" in DIAGNOSTICS["legacy_code_dependencies"][1]
    node.required_skill = "code.build_project"
    _validate_project_plan_shape([node])
    assert getattr(node, field) == ["source"]


@pytest.mark.parametrize("skills", list(product(MUTATORS, repeat=2)))
def test_server_still_rejects_two_mutators_without_misleading_python_explanation(
    skills: tuple[str, str],
) -> None:
    from types import SimpleNamespace

    from app.services.plan_validation import PlanValidationError, _validate_project_plan_shape
    from app.services.planner_diagnostics import rejection_reason
    from app.services.swarm_contracts import PlanNodeType

    nodes: Any = [
        SimpleNamespace(
            required_skill=skill,
            node_type=PlanNodeType.WORKER,
            dependencies=[],
            optional_dependencies=[],
        )
        for skill in skills
    ]
    with pytest.raises(PlanValidationError) as raised:
        _validate_project_plan_shape(nodes)
    assert raised.value.diagnostic_code == "project_plan_shape"
    assert "Python" not in rejection_reason("Plan rejeté.", raised.value.diagnostic_code)
    assert [node.required_skill for node in nodes] == list(skills)


@pytest.mark.parametrize("skill", MUTATORS)
def test_mutator_only_capabilities_have_a_single_step_plan(skill: str) -> None:
    schema = constrain_planner_graph(_source((skill,)))
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(_wire((skill,)))
    assert not Draft202012Validator(schema).is_valid(_wire((skill, skill)))
