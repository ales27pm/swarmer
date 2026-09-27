import json
from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from app.services.agent_card import SUPPORTED_AGENT_SKILLS
from app.services.model_wire_schema import (
    decode_research_query_nodes,
    encode_model_wire_response,
    model_wire_schema,
)
from app.services.plan_validation import PlanValidationError, parse_swarm_plan_json
from app.services.planner_graph_wire import constrain_planner_graph, decode_planner_graph
from app.services.planner_provider import UbuntuSwarmPlannerProvider, worker_node_array_schema
from app.services.swarm_contracts import MAX_PLAN_NODES, SwarmPlanProposal


def _source_schema(skills: list[str] | None = None) -> dict[str, Any]:
    schema = model_wire_schema(SwarmPlanProposal)
    schema["properties"]["nodes"] = worker_node_array_schema(
        schema["properties"]["nodes"],
        schema["$defs"]["SwarmPlanNodeProposal"],
        available_skills=skills or ["research.query", "writing.draft", "code.generate_python"],
        workers_first=True,
    )
    return schema


def _body(skill: str | None = "research.query") -> dict[str, Any]:
    body: dict[str, Any] = {
        "00_required_skill": skill,
        "node_type": "worker" if skill else "synthesis",
        "title": "Gather sources" if skill == "research.query" else "Prepare a summary",
        "objective": "Prepare a concise summary from the available sources.",
        "expected_output": "A summary with linked sources.",
        "priority": 50,
        "preferred_agent_constraints": None,
        "worker_arguments": None,
    }
    if skill == "research.query":
        body["search_query"] = body.pop("objective")
    return body


def _wire(*bodies: dict[str, Any]) -> dict[str, Any]:
    steps = {
        f"step_{index:02d}": {
            "00_temporary_id": f"step_{index}",
            "01_node": deepcopy(body),
            "02_dependencies": [],
            "03_optional_dependencies": [],
        }
        for index, body in enumerate(bodies, start=1)
    }
    return {
        "schema_version": "1.0",
        "objective": "Produce a sourced summary.",
        "rationale_summary": "Search first, then draft from the evidence.",
        "completion_criteria": ["A summary cites its sources."],
        "max_parallelism": 2,
        "nodes": {"00_node_count": len(bodies), "01_steps": steps},
    }


def _step(wire: dict[str, Any], number: int) -> dict[str, Any]:
    return wire["nodes"]["01_steps"][f"step_{number:02d}"]


def _legacy_chain(wire: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(wire)
    following = None
    for step in reversed(list(result["nodes"]["01_steps"].values())):
        following = {**step, "04_next": following}
    result["nodes"] = following
    return result


def _public(wire: dict[str, Any]) -> SwarmPlanProposal:
    return parse_swarm_plan_json(
        encode_model_wire_response(
            decode_research_query_nodes(decode_planner_graph(wire), node_field="nodes")
        )
    )


def test_research_then_writer_preserves_edges_and_original_schema() -> None:
    source = _source_schema()
    original = deepcopy(source)
    schema = constrain_planner_graph(source)
    Draft202012Validator.check_schema(schema)
    wire = _wire(_body(), _body("writing.draft"))
    _step(wire, 2)["02_dependencies"] = ["step_1"]
    before = deepcopy(wire)
    assert Draft202012Validator(schema).is_valid(wire)
    plan = _public(wire)
    assert [node.temporary_id for node in plan.nodes] == ["step_1", "step_2"]
    assert plan.nodes[0].dependencies == []
    assert plan.nodes[1].dependencies == ["step_1"]
    assert plan.nodes[0].required_skill == "research.query"
    assert plan.nodes[0].objective == _step(before, 1)["01_node"]["search_query"]
    assert source == original
    assert wire == before


def test_parallel_sources_and_optional_edges_are_not_serialized_into_a_chain() -> None:
    wire = _wire(_body(), _body(), _body("writing.draft"))
    _step(wire, 3)["02_dependencies"] = ["step_1"]
    _step(wire, 3)["03_optional_dependencies"] = ["step_2"]
    assert Draft202012Validator(constrain_planner_graph(_source_schema())).is_valid(wire)
    plan = _public(wire)
    assert plan.max_parallelism == 2
    assert [node.dependencies for node in plan.nodes] == [[], [], ["step_1"]]
    assert [node.optional_dependencies for node in plan.nodes] == [[], [], ["step_2"]]


def test_prompt_example_is_a_valid_complete_plan_with_independent_deliverables() -> None:
    example_line = next(
        line
        for line in UbuntuSwarmPlannerProvider.SYSTEM_PROMPT.splitlines()
        if line.startswith('{"00_node_count"')
    )
    wire = _wire(_body("writing.draft"))
    wire["nodes"] = json.loads(example_line)
    schema = constrain_planner_graph(_source_schema(["writing.draft"]))
    assert Draft202012Validator(schema).is_valid(wire)
    plan = _public(wire)
    assert [node.temporary_id for node in plan.nodes] == ["step_1", "step_2"]
    assert all(node.required_skill == "writing.draft" for node in plan.nodes)
    assert all(not node.dependencies and not node.optional_dependencies for node in plan.nodes)


@pytest.mark.parametrize("count", [1, MAX_PLAN_NODES])
def test_chain_bounds_accept_one_or_twenty_nodes(count: int) -> None:
    wire = _wire(*[_body("workspace.list_dir") for _ in range(count)])
    schema = constrain_planner_graph(_source_schema(["workspace.list_dir"]))
    assert Draft202012Validator(schema).is_valid(wire)
    assert len(_public(wire).nodes) == count


def test_chain_rejects_twenty_one_nodes_without_truncating() -> None:
    wire = _wire(*[_body("workspace.list_dir") for _ in range(MAX_PLAN_NODES + 1)])
    schema = constrain_planner_graph(_source_schema(["workspace.list_dir"]))
    assert not Draft202012Validator(schema).is_valid(wire)
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == "invalid_fields"
    assert _step(wire, MAX_PLAN_NODES + 1)["00_temporary_id"] == "step_21"


@pytest.mark.parametrize("field", ["02_dependencies", "03_optional_dependencies"])
@pytest.mark.parametrize(
    ("dependency", "diagnostic"),
    [
        ("goal_current", "unknown_dependency"),
        ("step_3", "unknown_dependency"),
        ("step_2", "self_dependency"),
    ],
)
def test_unknown_future_and_self_dependencies_are_rejected(
    field: str, dependency: str, diagnostic: str
) -> None:
    wire = _wire(_body(), _body("writing.draft"), _body())
    _step(wire, 2)[field] = [dependency]
    assert not Draft202012Validator(constrain_planner_graph(_source_schema())).is_valid(wire)
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == diagnostic


def test_dependency_cycle_cannot_be_encoded() -> None:
    wire = _wire(_body(), _body("writing.draft"))
    _step(wire, 1)["02_dependencies"] = ["step_2"]
    _step(wire, 2)["02_dependencies"] = ["step_1"]
    assert not Draft202012Validator(constrain_planner_graph(_source_schema())).is_valid(wire)
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == "unknown_dependency"


@pytest.mark.parametrize("field", ["02_dependencies", "03_optional_dependencies"])
def test_first_step_has_no_dependencies(field: str) -> None:
    wire = _wire(_body())
    _step(wire, 1)[field] = ["step_1"]
    assert not Draft202012Validator(constrain_planner_graph(_source_schema())).is_valid(wire)
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == "self_dependency"


def test_synthesis_input_and_legacy_no_input_constraints_survive_grouping() -> None:
    validator = Draft202012Validator(constrain_planner_graph(_source_schema()))
    assert not validator.is_valid(_wire(_body(None)))
    wire = _wire(_body(), _body(None))
    assert not validator.is_valid(wire)
    _step(wire, 2)["02_dependencies"] = ["step_1"]
    assert validator.is_valid(wire)
    legacy = _wire(_body(), _body("code.generate_python"))
    assert validator.is_valid(legacy)
    for field in ("02_dependencies", "03_optional_dependencies"):
        bad = deepcopy(legacy)
        _step(bad, 2)[field] = ["step_1"]
        assert not validator.is_valid(bad)


def test_synthesis_without_writer_retains_historical_empty_input_semantics() -> None:
    schema = constrain_planner_graph(_source_schema(["workspace.list_dir"]))
    assert Draft202012Validator(schema).is_valid(_wire(_body(None)))


@pytest.mark.parametrize(
    "shadow",
    [
        "temporary_id",
        "dependencies",
        "optional_dependencies",
        "00_temporary_id",
        "01_node",
        "02_dependencies",
        "03_optional_dependencies",
        "04_next",
    ],
)
def test_node_body_cannot_shadow_identity_or_dependencies(shadow: str) -> None:
    wire = _wire(_body())
    _step(wire, 1)["01_node"][shadow] = "step_9" if shadow == "temporary_id" else []
    assert not Draft202012Validator(constrain_planner_graph(_source_schema())).is_valid(wire)
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == "invalid_fields"


@pytest.mark.parametrize(
    "field",
    ["00_temporary_id", "01_node", "02_dependencies", "03_optional_dependencies"],
)
def test_each_wrapper_field_is_required(field: str) -> None:
    wire = _wire(_body())
    del _step(wire, 1)[field]
    assert not Draft202012Validator(constrain_planner_graph(_source_schema())).is_valid(wire)
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == "invalid_fields"


def test_inherited_optional_minimum_and_dependency_maximum_remain_effective() -> None:
    source = _source_schema()
    writer = next(
        branch
        for branch in source["properties"]["nodes"]["items"]["anyOf"]
        if "writing.draft" in branch["properties"]["00_required_skill"].get("enum", [])
    )
    writer["properties"]["dependencies"]["maxItems"] = 1
    writer["properties"]["optional_dependencies"]["minItems"] = 1
    validator = Draft202012Validator(constrain_planner_graph(source))
    assert not validator.is_valid(_wire(_body("writing.draft")))
    wire = _wire(_body(), _body(), _body("writing.draft"))
    _step(wire, 3)["02_dependencies"] = ["step_1"]
    assert not validator.is_valid(wire)
    _step(wire, 3)["03_optional_dependencies"] = ["step_2"]
    assert validator.is_valid(wire)
    _step(wire, 3)["02_dependencies"] = ["step_1", "step_2"]
    assert not validator.is_valid(wire)


def test_every_step_id_is_position_bound_and_in_memory_cycles_are_rejected() -> None:
    wire = _wire(_body(), _body())
    _step(wire, 2)["00_temporary_id"] = "step_1"
    assert not Draft202012Validator(constrain_planner_graph(_source_schema())).is_valid(wire)
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == "invalid_fields"
    # Defensive for callers supplying Python mappings; cyclic objects cannot arise from JSON.
    wire = _legacy_chain(_wire(_body()))
    wire["nodes"]["04_next"] = wire["nodes"]
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == "invalid_fields"


def test_decoder_preserves_edge_order_and_leaves_repeated_edges_to_public_validation() -> None:
    wire = _wire(_body(), _body(), _body("writing.draft"))
    _step(wire, 3)["02_dependencies"] = ["step_2", "step_1"]
    assert _public(wire).nodes[2].dependencies == ["step_2", "step_1"]
    _step(wire, 3)["02_dependencies"] = ["step_1", "step_1"]
    decoded = decode_planner_graph(wire)
    nodes = decoded["nodes"]
    assert isinstance(nodes, list)
    assert nodes[2]["dependencies"] == ["step_1", "step_1"]
    with pytest.raises(PlanValidationError) as raised:
        _public(wire)
    assert raised.value.diagnostic_code == "repeated_dependency"


def test_unavailable_capabilities_and_body_constraints_stay_in_the_schema() -> None:
    schema = constrain_planner_graph(_source_schema(["research.query", "writing.draft"]))
    validator = Draft202012Validator(schema)
    assert not validator.is_valid(_wire(_body("workspace.list_dir")))
    invalid_query = _wire(_body())
    body = _step(invalid_query, 1)["01_node"]
    body["objective"] = body.pop("search_query")
    assert not validator.is_valid(invalid_query)
    invalid_priority = _wire(_body())
    _step(invalid_priority, 1)["01_node"]["priority"] = 101
    assert not validator.is_valid(invalid_priority)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_key",
        "missing_count",
        "id_gap",
        "node_array",
        "dependencies_string",
        "dependencies_nonstring",
        "optional_null",
        "next_array",
        "next_string",
        "null_root",
    ],
)
def test_malformed_wrapper_is_rejected_by_schema_and_decoder(mutation: str) -> None:
    wire = _wire(_body())
    node = _step(wire, 1)
    if mutation == "extra_key":
        node["extra"] = 1
    elif mutation == "missing_count":
        del wire["nodes"]["00_node_count"]
    elif mutation == "id_gap":
        node["00_temporary_id"] = "step_2"
    elif mutation == "node_array":
        node["01_node"] = []
    elif mutation == "dependencies_string":
        node["02_dependencies"] = "step_1"
    elif mutation == "dependencies_nonstring":
        node["02_dependencies"] = [1]
    elif mutation == "optional_null":
        node["03_optional_dependencies"] = None
    elif mutation == "next_array":
        node["04_next"] = []
    elif mutation == "next_string":
        node["04_next"] = "step_2"
    elif mutation == "null_root":
        wire["nodes"] = None
    assert not Draft202012Validator(constrain_planner_graph(_source_schema())).is_valid(wire)
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == "invalid_fields"


def test_legacy_array_dag_remains_unchanged_including_forward_references() -> None:
    legacy = _public(_wire(_body(), _body("writing.draft"))).model_dump(mode="json")
    legacy["nodes"][0]["dependencies"] = ["step_2"]
    original = deepcopy(legacy)
    assert decode_planner_graph(legacy) == original
    assert legacy == original
    # The wire decoder does not repair malformed legacy data; public validation stays authoritative.
    legacy["nodes"].append({"unexpected": "node"})
    assert decode_planner_graph(legacy) == legacy
    with pytest.raises(PlanValidationError):
        parse_swarm_plan_json(encode_model_wire_response(decode_planner_graph(legacy)))


def test_shared_definitions_keep_all_capability_schema_growth_bounded() -> None:
    schema = constrain_planner_graph(_source_schema(sorted(SUPPORTED_AGENT_SKILLS)))
    Draft202012Validator.check_schema(schema)
    definitions = schema["$defs"]
    assert {f"Step{index}" for index in range(1, MAX_PLAN_NODES + 1)} <= definitions.keys()
    assert len(schema["properties"]["nodes"]["anyOf"]) == MAX_PLAN_NODES
    assert len(json.dumps(schema).encode()) < 100_000
    # Capability schemas must be shared rather than copied into every position.
    assert json.dumps(schema).count('"const": "research.query"') == 1


@pytest.mark.parametrize("count", [0, 21, True, 3.0, "3", None])
def test_count_must_be_a_bounded_exact_integer(count: object) -> None:
    wire = _wire(_body(), _body(), _body("writing.draft"))
    wire["nodes"]["00_node_count"] = count
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == "invalid_fields"


@pytest.mark.parametrize("mutation", ["extra_step", "missing_step", "extra_field", "wrong_slot"])
def test_declared_count_prevents_runaway_continuation_or_early_end(mutation: str) -> None:
    wire = _wire(_body(), _body("writing.draft"), _body("workspace.list_dir"))
    _step(wire, 2)["02_dependencies"] = ["step_1"]
    steps = wire["nodes"]["01_steps"]
    if mutation == "extra_step":
        steps["step_04"] = {**deepcopy(steps["step_03"]), "00_temporary_id": "step_4"}
    elif mutation == "missing_step":
        del steps["step_03"]
    elif mutation == "wrong_slot":
        steps["step_4"] = steps.pop("step_03")
    else:
        wire["nodes"]["extra"] = None
    validator = Draft202012Validator(
        constrain_planner_graph(
            _source_schema(["research.query", "writing.draft", "workspace.list_dir"])
        )
    )
    assert not validator.is_valid(wire)
    with pytest.raises(PlanValidationError) as raised:
        decode_planner_graph(wire)
    assert raised.value.diagnostic_code == "invalid_fields"


def test_counted_slot_mapping_order_does_not_change_dependencies() -> None:
    wire = _wire(_body(), _body(), _body("writing.draft"))
    _step(wire, 3)["02_dependencies"] = ["step_2", "step_1"]
    wire["nodes"]["01_steps"] = dict(reversed(list(wire["nodes"]["01_steps"].items())))
    assert [node.temporary_id for node in _public(wire).nodes] == ["step_1", "step_2", "step_3"]
    assert _public(wire).nodes[2].dependencies == ["step_2", "step_1"]


@pytest.mark.parametrize("count", [1, 3, 20, 21])
def test_legacy_chains_keep_validation_without_rewriting(count: int) -> None:
    wire = _legacy_chain(_wire(*[_body() for _ in range(count)]))
    before = deepcopy(wire)
    if count <= MAX_PLAN_NODES:
        assert len(_public(wire).nodes) == count
    else:
        with pytest.raises(PlanValidationError):
            decode_planner_graph(wire)
    assert wire == before
