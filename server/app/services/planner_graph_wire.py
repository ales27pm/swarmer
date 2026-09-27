"""Bounded, planner-only graph grammar; the public DAG contract stays unchanged."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from app.services.plan_validation import PlanValidationError
from app.services.swarm_contracts import MAX_PLAN_NODES

_WRAPPER_FIELDS = (
    "00_temporary_id",
    "01_node",
    "02_dependencies",
    "03_optional_dependencies",
    "04_next",
)
_GRAPH_FIELDS = frozenset({"temporary_id", "dependencies", "optional_dependencies"})


def constrain_planner_graph(schema: dict[str, Any]) -> dict[str, Any]:
    """Encode topologically listed nodes using shared, bounded step definitions.

    ``04_next`` is only the wire representation of the node list, never an
    execution dependency. Every position can still declare independent work.
    Body groups retain the existing capability-specific dependency bounds, so
    synthesis still needs evidence and legacy code generation stays standalone.
    The already materialized worker branches and the input schema are not changed.
    """
    result = deepcopy(schema)
    items = result["properties"]["nodes"]["items"]
    branches = items.get("anyOf", [items])
    groups: dict[str, tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = {}
    for branch in branches:
        body = deepcopy(branch)
        properties = body["properties"]
        dependencies = properties["dependencies"]
        optional = properties["optional_dependencies"]
        key = json.dumps([dependencies, optional], sort_keys=True, separators=(",", ":"))
        for field in _GRAPH_FIELDS:
            del properties[field]
        body["required"] = [field for field in body["required"] if field not in _GRAPH_FIELDS]
        if key not in groups:
            groups[key] = (dependencies, optional, [])
        groups[key][2].append(body)

    definitions = result.setdefault("$defs", {})
    reserved_names = {
        *(f"Step{index}" for index in range(1, MAX_PLAN_NODES + 1)),
        *(f"PlannerGraphBody{index}" for index in range(1, len(groups) + 1)),
    }
    if reserved_names & definitions.keys():
        raise ValueError("planner graph definitions collide with the source schema")

    for group_index, (_, _, bodies) in enumerate(groups.values(), start=1):
        definitions[f"PlannerGraphBody{group_index}"] = (
            bodies[0] if len(bodies) == 1 else {"anyOf": bodies}
        )

    for index in range(1, MAX_PLAN_NODES + 1):
        prior_ids = [f"step_{previous}" for previous in range(1, index)]
        alternatives = []
        for group_index, (dependencies, optional, _) in enumerate(groups.values(), start=1):
            if any(field.get("minItems", 0) > len(prior_ids) for field in (dependencies, optional)):
                continue
            next_schema: dict[str, Any] = {"type": "null"}
            if index < MAX_PLAN_NODES:
                next_schema = {"anyOf": [{"type": "null"}, {"$ref": f"#/$defs/Step{index + 1}"}]}
            alternatives.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "00_temporary_id": {"type": "string", "const": f"step_{index}"},
                        "01_node": {"$ref": f"#/$defs/PlannerGraphBody{group_index}"},
                        "02_dependencies": _prior_dependencies(dependencies, prior_ids),
                        "03_optional_dependencies": _prior_dependencies(optional, prior_ids),
                        "04_next": next_schema,
                    },
                    "required": list(_WRAPPER_FIELDS),
                }
            )
        if not alternatives:
            raise ValueError("planner graph schema has no eligible root node")
        definitions[f"Step{index}"] = (
            alternatives[0] if len(alternatives) == 1 else {"anyOf": alternatives}
        )
    result["properties"]["nodes"] = {"$ref": "#/$defs/Step1"}
    return result


def _prior_dependencies(schema: dict[str, Any], prior_ids: list[str]) -> dict[str, Any]:
    result = deepcopy(schema)
    result["maxItems"] = min(result.get("maxItems", MAX_PLAN_NODES), len(prior_ids))
    result["items"] = {"type": "string", **({"enum": prior_ids} if prior_ids else {})}
    result["description"] = (
        "Exact IDs of preceding steps supplying input; use [] for independent work. "
        "The next link does not imply a dependency."
    )
    return result


def decode_planner_graph(value: Mapping[str, object]) -> dict[str, object]:
    """Flatten the planner wire exactly, rejecting rather than repairing bad edges.

    Legacy node arrays pass through unchanged to the same strict public validator.
    Public validation also remains responsible for capabilities, repeated edges,
    overlapping hard/optional edges, and all node payload constraints.
    """
    result = dict(value)
    current = value.get("nodes")
    if isinstance(current, list):
        return result
    if not isinstance(current, Mapping):
        raise _invalid_fields("planner nodes must be a nonempty step chain or a legacy node array")
    nodes: list[dict[str, object]] = []
    prior_ids: set[str] = set()
    while current is not None:
        if len(nodes) >= MAX_PLAN_NODES:
            raise _invalid_fields(f"planner step chain exceeds {MAX_PLAN_NODES} nodes")
        if not isinstance(current, Mapping) or current.keys() != set(_WRAPPER_FIELDS):
            raise _invalid_fields("planner step has missing, unknown, or malformed wire fields")
        node_id = f"step_{len(nodes) + 1}"
        if current["00_temporary_id"] != node_id:
            raise _invalid_fields("planner step IDs must be consecutive and match their positions")
        body = current["01_node"]
        if not isinstance(body, Mapping) or (_GRAPH_FIELDS | set(_WRAPPER_FIELDS)) & body.keys():
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
        current = current["04_next"]
    result["nodes"] = nodes
    return result


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
