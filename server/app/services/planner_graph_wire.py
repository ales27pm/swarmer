"""Counted, planner-only graph grammar; the public DAG contract stays unchanged."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from app.services.agent_card import CODE_GENERATION_SKILLS, PROJECT_BUILD_SKILLS
from app.services.plan_validation import PlanValidationError
from app.services.swarm_contracts import MAX_PLAN_NODES

_WRAPPER_FIELDS = (
    "00_temporary_id",
    "01_node",
    "02_dependencies",
    "03_optional_dependencies",
)
_CHAIN_FIELDS = (*_WRAPPER_FIELDS, "04_next")
_COUNT_FIELDS = frozenset({"00_node_count", "01_steps"})
_GRAPH_FIELDS = frozenset({"temporary_id", "dependencies", "optional_dependencies"})


def constrain_planner_graph(schema: dict[str, Any]) -> dict[str, Any]:
    """Select a bounded count first, then emit exactly that many shared steps.

    The count prevents a repeated next-link decision from extending an already
    complete plan. Slot order is serialization, never an execution dependency.
    Body groups retain the existing capability-specific dependency bounds, so
    synthesis still needs evidence and legacy code generation stays standalone.
    Counted slot alternatives permit a project mutator at only one position;
    other slots retain all non-project capabilities, including synthesis. This
    uses supported object/anyOf/$ref grammar, not contains/maxContains or a
    prompt-only rule. The public wire and input schema are not changed.
    """
    result = deepcopy(schema)
    items = result["properties"]["nodes"]["items"]
    branches = items.get("anyOf", [items])
    groups: dict[str, tuple[dict[str, Any], dict[str, Any], bool, list[dict[str, Any]]]] = {}
    mutating_skills = PROJECT_BUILD_SKILLS | CODE_GENERATION_SKILLS
    for branch in branches:
        body = deepcopy(branch)
        properties = body["properties"]
        dependencies = properties["dependencies"]
        optional = properties["optional_dependencies"]
        skill_schema = properties["00_required_skill"]
        skills = skill_schema.get("enum", [skill_schema.get("const")])
        mutates = any(skill in mutating_skills for skill in skills)
        if mutates and not all(skill in mutating_skills for skill in skills):
            raise ValueError("planner worker branches must separate project-mutating skills")
        key = json.dumps([dependencies, optional, mutates], sort_keys=True, separators=(",", ":"))
        for field in _GRAPH_FIELDS:
            del properties[field]
        body["required"] = [field for field in body["required"] if field not in _GRAPH_FIELDS]
        if key not in groups:
            groups[key] = (dependencies, optional, mutates, [])
        groups[key][3].append(body)

    definitions = result.setdefault("$defs", {})
    reserved_names = {
        *(f"Step{index}" for index in range(1, MAX_PLAN_NODES + 1)),
        *(f"Step{index}NonProject" for index in range(1, MAX_PLAN_NODES + 1)),
        *(f"PlannerGraphBody{index}" for index in range(1, len(groups) + 1)),
    }
    if reserved_names & definitions.keys():
        raise ValueError("planner graph definitions collide with the source schema")

    for group_index, (_, _, _, bodies) in enumerate(groups.values(), start=1):
        definitions[f"PlannerGraphBody{group_index}"] = (
            bodies[0] if len(bodies) == 1 else {"anyOf": bodies}
        )

    has_mutator = any(group[2] for group in groups.values())
    dependency_names: dict[str, str] = {}

    def dependency_reference(bounds: dict[str, Any], prior_ids: list[str]) -> dict[str, Any]:
        dependency = _prior_dependencies(bounds, prior_ids)
        title = dependency.pop("title", None)
        fingerprint = json.dumps(dependency, sort_keys=True, separators=(",", ":"))
        if fingerprint not in dependency_names:
            name = f"PlannerGraphDependencies{len(dependency_names) + 1}"
            if name in definitions:
                raise ValueError("planner graph definitions collide with the source schema")
            # Reuse exactly equal constraints, retaining each title at its use
            # site. Hard/optional edges with different bounds never share them.
            definitions[name] = dependency
            dependency_names[fingerprint] = name
        reference = {"$ref": f"#/$defs/{dependency_names[fingerprint]}"}
        if title is not None:
            reference["title"] = title
        return reference

    for index in range(1, MAX_PLAN_NODES + 1):
        prior_ids = [f"step_{previous}" for previous in range(1, index)]
        alternatives = []
        non_project = []
        for group_index, (dependencies, optional, mutates, _) in enumerate(groups.values(), start=1):
            if any(field.get("minItems", 0) > len(prior_ids) for field in (dependencies, optional)):
                continue
            alternatives.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "00_temporary_id": {"type": "string", "const": f"step_{index}"},
                        "01_node": {"$ref": f"#/$defs/PlannerGraphBody{group_index}"},
                        "02_dependencies": dependency_reference(dependencies, prior_ids),
                        "03_optional_dependencies": dependency_reference(optional, prior_ids),
                    },
                    "required": list(_WRAPPER_FIELDS),
                }
            )
            if not mutates:
                non_project.append(alternatives[-1])
        if not alternatives:
            raise ValueError("planner graph schema has no eligible root node")
        definitions[f"Step{index}"] = (
            alternatives[0] if len(alternatives) == 1 else {"anyOf": alternatives}
        )
        if has_mutator and non_project:
            definitions[f"Step{index}NonProject"] = (
                non_project[0] if len(non_project) == 1 else {"anyOf": non_project}
            )
    counted_alternatives = []
    for count in range(1, MAX_PLAN_NODES + 1):
        slot_alternatives = []
        # Selecting a permitted position does not require a mutator: zero is
        # valid too. No other slot can use either project-mutating capability.
        # A single position can contain the entire cumulative implementation;
        # its required sources and downstream consumers are not discarded.
        permitted_positions = range(1, count + 1) if has_mutator else (0,)
        for permitted in permitted_positions:
            names = {
                f"step_{index:02d}": (
                    f"Step{index}"
                    if not has_mutator or index == permitted
                    else f"Step{index}NonProject"
                )
                for index in range(1, count + 1)
            }
            if any(name not in definitions for name in names.values()):
                continue
            slots = {slot: {"$ref": f"#/$defs/{name}"} for slot, name in names.items()}
            slot_alternatives.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": slots,
                    "required": list(slots),
                }
            )
        if not slot_alternatives:
            continue
        counted_alternatives.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "00_node_count": {"type": "integer", "const": count},
                    "01_steps": (
                        slot_alternatives[0]
                        if len(slot_alternatives) == 1
                        else {"anyOf": slot_alternatives}
                    ),
                },
                "required": ["00_node_count", "01_steps"],
            }
        )
    result["properties"]["nodes"] = {"anyOf": counted_alternatives}
    return result


def _prior_dependencies(schema: dict[str, Any], prior_ids: list[str]) -> dict[str, Any]:
    result = deepcopy(schema)
    result["maxItems"] = min(result.get("maxItems", MAX_PLAN_NODES), len(prior_ids))
    result["items"] = {"type": "string", **({"enum": prior_ids} if prior_ids else {})}
    result["description"] = (
        "Exact IDs of preceding steps supplying input; use [] for independent work. "
        "Slot order does not imply a dependency."
    )
    return result


def decode_planner_graph(value: Mapping[str, object]) -> dict[str, object]:
    """Flatten the planner wire exactly, rejecting rather than repairing bad edges.

    Legacy node arrays pass through unchanged to the same strict public validator.
    Public validation also remains responsible for capabilities, repeated edges,
    overlapping hard/optional edges, and all node payload constraints.
    """
    result = dict(value)
    wire = value.get("nodes")
    if isinstance(wire, list):
        return result
    if not isinstance(wire, Mapping):
        raise _invalid_fields("planner nodes must be a counted graph, legacy chain or node array")
    steps = _counted_steps(wire) if _COUNT_FIELDS & wire.keys() else _legacy_steps(wire)
    nodes: list[dict[str, object]] = []
    prior_ids: set[str] = set()
    for current in steps:
        node_id = f"step_{len(nodes) + 1}"
        if current["00_temporary_id"] != node_id:
            raise _invalid_fields("planner step IDs must be consecutive and match their positions")
        body = current["01_node"]
        forbidden = _GRAPH_FIELDS | set(_CHAIN_FIELDS) | _COUNT_FIELDS
        if not isinstance(body, Mapping) or forbidden & body.keys():
            raise _invalid_fields("planner node body cannot shadow graph wire fields")
        dependencies = _decode_dependencies(current["02_dependencies"], node_id, prior_ids)
        optional = _decode_dependencies(current["03_optional_dependencies"], node_id, prior_ids)
        nodes.append(
            {
                "temporary_id": node_id,
                **body,
                "dependencies": dependencies,
                "optional_dependencies": optional,
            }
        )
        prior_ids.add(node_id)
    result["nodes"] = nodes
    return result


def _counted_steps(wire: Mapping[str, object]) -> list[Mapping[str, object]]:
    if wire.keys() != _COUNT_FIELDS:
        raise _invalid_fields("planner counted graph has missing or unknown fields")
    count, slots = wire["00_node_count"], wire["01_steps"]
    if type(count) is not int or not 1 <= count <= MAX_PLAN_NODES:
        raise _invalid_fields(f"planner node count must be an integer from 1 to {MAX_PLAN_NODES}")
    keys = [f"step_{index:02d}" for index in range(1, count + 1)]
    if not isinstance(slots, Mapping) or slots.keys() != set(keys):
        raise _invalid_fields("planner slots must match the declared node count exactly")
    steps: list[Mapping[str, object]] = []
    for key in keys:
        step = slots[key]
        if not isinstance(step, Mapping) or step.keys() != set(_WRAPPER_FIELDS):
            raise _invalid_fields("planner step has missing, unknown, or malformed wire fields")
        steps.append(step)
    return steps


def _legacy_steps(wire: Mapping[str, object]) -> list[Mapping[str, object]]:
    steps: list[Mapping[str, object]] = []
    current: object = wire
    while current is not None:
        if len(steps) >= MAX_PLAN_NODES:
            raise _invalid_fields(f"planner step chain exceeds {MAX_PLAN_NODES} nodes")
        if not isinstance(current, Mapping) or current.keys() != set(_CHAIN_FIELDS):
            raise _invalid_fields("planner step has missing, unknown, or malformed wire fields")
        steps.append(current)
        current = current["04_next"]
    return steps


def _decode_dependencies(value: object, node_id: str, prior_ids: set[str]) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_PLAN_NODES
        or any(not isinstance(dependency, str) for dependency in value)
    ):
        raise _invalid_fields("planner dependencies must be bounded arrays of step IDs")
    for dependency in value:
        if dependency == node_id:
            raise PlanValidationError(
                f"node {node_id} cannot depend on itself", diagnostic_code="self_dependency"
            )
        if dependency not in prior_ids:
            raise PlanValidationError(
                f"node {node_id} has a dependency outside its preceding steps",
                diagnostic_code="unknown_dependency",
            )
    return list(value)


def _invalid_fields(message: str) -> PlanValidationError:
    return PlanValidationError(message, diagnostic_code="invalid_fields")
