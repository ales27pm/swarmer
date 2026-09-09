from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from pydantic import BaseModel, ValidationError

from app.services.permission_policy import PermissionPolicy, PermissionPolicyError
from app.services.swarm_contracts import (
    MAX_PLAN_NODES,
    MAX_PLAN_PARALLELISM,
    EvaluationDecision,
    EvaluationStatus,
    PlanNodeType,
    SwarmPlanNodeProposal,
    SwarmPlanProposal,
)

MAX_PROPOSAL_BYTES = 131_072


class PlanValidationError(ValueError):
    """A model proposal failed a structural or server-policy invariant."""


@dataclass(frozen=True)
class ValidatedSwarmPlan:
    proposal: SwarmPlanProposal
    topological_order: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class ValidatedEvaluationDecision:
    decision: EvaluationDecision
    suggested_node_order: tuple[str, ...]
    fingerprint: str


def _format_validation_error(error: ValidationError) -> str:
    details: list[str] = []
    for item in error.errors(include_input=False):
        location = ".".join(str(part) for part in item["loc"])
        details.append(f"{location or 'proposal'}: {item['msg']}")
    return "; ".join(details[:10])


def _coerce_model[ModelT: BaseModel](
    model_type: type[ModelT], raw: ModelT | Mapping[str, object]
) -> ModelT:
    if isinstance(raw, model_type):
        return raw
    try:
        return model_type.model_validate(raw)
    except ValidationError as exc:
        raise PlanValidationError(_format_validation_error(exc)) from exc


def _reject_json_constant(value: str) -> NoReturn:
    raise PlanValidationError(f"non-finite JSON constant is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanValidationError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _parse_json_object(text: str) -> Mapping[str, object]:
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise PlanValidationError("proposal contains invalid Unicode") from exc
    if encoded_size > MAX_PROPOSAL_BYTES:
        raise PlanValidationError("proposal exceeds the maximum JSON size")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise PlanValidationError("proposal is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PlanValidationError("proposal must be a JSON object")
    return value


def parse_swarm_plan_json(text: str) -> SwarmPlanProposal:
    return _coerce_model(SwarmPlanProposal, _parse_json_object(text))


def parse_evaluation_json(text: str) -> EvaluationDecision:
    return _coerce_model(EvaluationDecision, _parse_json_object(text))


def _validate_worker_policy(node: SwarmPlanNodeProposal, policy: PermissionPolicy) -> None:
    if node.node_type is not PlanNodeType.WORKER:
        return
    if node.required_skill is None:  # protected by SwarmPlanNodeProposal validation
        raise PlanValidationError(f"node {node.temporary_id} is missing its worker skill")
    try:
        rule = policy.evaluate_worker_skill(node.required_skill)
    except PermissionPolicyError as exc:
        raise PlanValidationError(
            f"node {node.temporary_id} requests an unsupported worker skill"
        ) from exc
    if rule.decision != "allow":
        raise PlanValidationError(
            f"node {node.temporary_id} requests a worker skill denied by policy"
        )


def _validate_node_graph(
    nodes: Sequence[SwarmPlanNodeProposal],
    *,
    policy: PermissionPolicy,
    known_dependency_ids: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    if len(nodes) > MAX_PLAN_NODES:
        raise PlanValidationError(f"plans cannot contain more than {MAX_PLAN_NODES} nodes")

    by_id: dict[str, SwarmPlanNodeProposal] = {}
    for node in nodes:
        if node.temporary_id in by_id or node.temporary_id in known_dependency_ids:
            raise PlanValidationError(f"duplicate node id: {node.temporary_id}")
        by_id[node.temporary_id] = node
        _validate_worker_policy(node, policy)

    current_ids = frozenset(by_id)
    all_known_ids = current_ids | known_dependency_ids
    indegree = {node_id: 0 for node_id in current_ids}
    dependents: dict[str, list[str]] = {node_id: [] for node_id in current_ids}
    for node in nodes:
        if len(set(node.dependencies)) != len(node.dependencies):
            raise PlanValidationError(f"node {node.temporary_id} repeats a dependency")
        if len(set(node.optional_dependencies)) != len(node.optional_dependencies):
            raise PlanValidationError(f"node {node.temporary_id} repeats an optional dependency")
        for dependency in (*node.dependencies, *node.optional_dependencies):
            if dependency == node.temporary_id:
                raise PlanValidationError(f"node {node.temporary_id} cannot depend on itself")
            if dependency not in all_known_ids:
                raise PlanValidationError(
                    f"node {node.temporary_id} has unknown dependency {dependency}"
                )
            if dependency in current_ids:
                indegree[node.temporary_id] += 1
                dependents[dependency].append(node.temporary_id)

    ready = [node_id for node_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        node_id = heapq.heappop(ready)
        ordered.append(node_id)
        for dependent in sorted(dependents[node_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(nodes):
        raise PlanValidationError("plan dependencies contain a cycle")
    return tuple(ordered)


def _semantic_node_signatures(
    nodes: Sequence[SwarmPlanNodeProposal],
) -> list[str]:
    """Return rename-invariant DAG signatures while retaining external anchors."""

    by_id = {node.temporary_id: node for node in nodes}
    memo: dict[str, str] = {}
    visiting: set[str] = set()

    def visit(node_id: str) -> str:
        if node_id in memo:
            return memo[node_id]
        if node_id in visiting:
            raise PlanValidationError("plan dependencies contain a cycle")
        node = by_id[node_id]
        visiting.add(node_id)
        base = node.model_dump(
            mode="json",
            exclude={"temporary_id", "dependencies", "optional_dependencies"},
            exclude_none=True,
        )
        constraints = base.get("preferred_agent_constraints")
        if isinstance(constraints, dict):
            for field in ("agent_ids", "model_ids"):
                values = constraints.get(field)
                if isinstance(values, list):
                    constraints[field] = sorted(values)

        def dependency_signature(dependency: str) -> str:
            return visit(dependency) if dependency in by_id else f"external:{dependency}"

        normalized: dict[str, object] = {
            "node": base,
            "hard_dependencies": sorted(
                dependency_signature(dependency) for dependency in node.dependencies
            ),
            "optional_dependencies": sorted(
                dependency_signature(dependency) for dependency in node.optional_dependencies
            ),
        }
        visiting.remove(node_id)
        memo[node_id] = _fingerprint(normalized)
        return memo[node_id]

    return sorted(visit(node.temporary_id) for node in nodes)


def _fingerprint(value: Mapping[str, object]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def fingerprint_swarm_plan(proposal: SwarmPlanProposal) -> str:
    """Fingerprint executable semantics, excluding free-form rationale prose."""

    normalized: dict[str, object] = {
        "schema_version": proposal.schema_version,
        "objective": proposal.objective,
        "nodes": _semantic_node_signatures(proposal.nodes),
        "completion_criteria": sorted(proposal.completion_criteria),
        "max_parallelism": proposal.max_parallelism,
    }
    return _fingerprint(normalized)


def fingerprint_evaluation_decision(decision: EvaluationDecision) -> str:
    """Fingerprint evaluator control semantics, excluding mutable explanatory prose."""

    normalized: dict[str, object] = {
        "schema_version": decision.schema_version,
        "status": decision.status.value,
        "missing_requirements": sorted(decision.missing_requirements),
        "invalid_results": sorted(decision.invalid_results),
        "suggested_new_nodes": _semantic_node_signatures(decision.suggested_new_nodes),
        "user_question": decision.user_question,
    }
    return _fingerprint(normalized)


def validate_swarm_plan(
    raw: SwarmPlanProposal | Mapping[str, object],
    *,
    policy: PermissionPolicy,
    max_nodes: int = MAX_PLAN_NODES,
    max_parallelism: int = MAX_PLAN_PARALLELISM,
) -> ValidatedSwarmPlan:
    proposal = _coerce_model(SwarmPlanProposal, raw)
    if len(proposal.nodes) > min(max_nodes, MAX_PLAN_NODES):
        raise PlanValidationError(f"plan exceeds the configured {max_nodes}-node budget")
    if proposal.max_parallelism > min(max_parallelism, MAX_PLAN_PARALLELISM):
        raise PlanValidationError("plan exceeds the configured parallelism budget")
    order = _validate_node_graph(proposal.nodes, policy=policy)
    return ValidatedSwarmPlan(
        proposal=proposal,
        topological_order=order,
        fingerprint=fingerprint_swarm_plan(proposal),
    )


def validate_evaluation_decision(
    raw: EvaluationDecision | Mapping[str, object],
    *,
    policy: PermissionPolicy,
    known_node_ids: Sequence[str] = (),
) -> ValidatedEvaluationDecision:
    decision = _coerce_model(EvaluationDecision, raw)
    if (
        decision.status not in {EvaluationStatus.CONTINUE, EvaluationStatus.REPLAN}
        and decision.suggested_new_nodes
    ):
        raise PlanValidationError("only continue or replan may suggest new nodes")
    known = frozenset(known_node_ids)
    if len(known) != len(known_node_ids):
        raise PlanValidationError("known node ids must be unique")
    if len(known) + len(decision.suggested_new_nodes) > MAX_PLAN_NODES:
        raise PlanValidationError("suggested nodes exceed the goal node budget")
    order = _validate_node_graph(
        decision.suggested_new_nodes,
        policy=policy,
        known_dependency_ids=known,
    )
    return ValidatedEvaluationDecision(
        decision=decision,
        suggested_node_order=order,
        fingerprint=fingerprint_evaluation_decision(decision),
    )
