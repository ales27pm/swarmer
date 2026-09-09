from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import (
    PlanValidationError,
    parse_swarm_plan_json,
    validate_swarm_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def policy() -> PermissionPolicy:
    return PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")


def valid_plan() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "objective": "Inspect the repository and summarize the evidence.",
        "rationale_summary": "Use two independent read-only workers before synthesis.",
        "nodes": [
            {
                "temporary_id": "inventory",
                "node_type": "worker",
                "title": "Inventory",
                "objective": "List the repository root.",
                "required_skill": "workspace.list_dir",
                "dependencies": [],
                "expected_output": "A bounded list of root entries.",
                "priority": 80,
                "preferred_agent_constraints": {
                    "agent_ids": ["agent-b", "agent-a"],
                    "model_ids": ["reviewer-v1"],
                    "runtime": "python",
                },
            },
            {
                "temporary_id": "readme",
                "node_type": "worker",
                "title": "Read documentation",
                "objective": "Read the repository overview.",
                "required_skill": "workspace.read_text",
                "dependencies": [],
                "expected_output": "A summary grounded in README content.",
                "priority": 70,
                "preferred_agent_constraints": None,
            },
            {
                "temporary_id": "synthesis",
                "node_type": "synthesis",
                "title": "Synthesize",
                "objective": "Combine the verified worker results.",
                "required_skill": None,
                "dependencies": ["readme", "inventory"],
                "expected_output": "A concise evidence-backed answer.",
                "priority": 10,
                "preferred_agent_constraints": None,
            },
        ],
        "completion_criteria": [
            "The repository root was inspected.",
            "The answer distinguishes observation from inference.",
        ],
        "max_parallelism": 2,
    }


def test_valid_plan_has_deterministic_topological_order_and_fingerprint(
    policy: PermissionPolicy,
) -> None:
    first = validate_swarm_plan(valid_plan(), policy=policy)
    reordered = deepcopy(valid_plan())
    assert isinstance(reordered["nodes"], list)
    reordered["nodes"] = list(reversed(reordered["nodes"]))
    reordered["rationale_summary"] = "Equivalent control semantics with different prose."
    reordered["completion_criteria"] = list(reversed(reordered["completion_criteria"]))

    second = validate_swarm_plan(reordered, policy=policy)

    assert first.topological_order == ("inventory", "readme", "synthesis")
    assert second.topological_order == first.topological_order
    assert second.fingerprint == first.fingerprint
    assert len(first.fingerprint) == 64


def test_plan_fingerprint_changes_when_executable_semantics_change(
    policy: PermissionPolicy,
) -> None:
    first = validate_swarm_plan(valid_plan(), policy=policy)
    changed = deepcopy(valid_plan())
    assert isinstance(changed["nodes"], list)
    assert isinstance(changed["nodes"][0], dict)
    changed["nodes"][0]["expected_output"] = "A different required result."
    assert validate_swarm_plan(changed, policy=policy).fingerprint != first.fingerprint


def test_plan_fingerprint_is_invariant_to_temporary_node_renaming(
    policy: PermissionPolicy,
) -> None:
    first = validate_swarm_plan(valid_plan(), policy=policy)
    renamed = deepcopy(valid_plan())
    assert isinstance(renamed["nodes"], list)
    replacements = {"inventory": "worker_a", "readme": "worker_b", "synthesis": "final"}
    for raw in renamed["nodes"]:
        assert isinstance(raw, dict)
        raw["temporary_id"] = replacements[str(raw["temporary_id"])]
        raw["dependencies"] = [replacements[str(item)] for item in raw["dependencies"]]

    assert validate_swarm_plan(renamed, policy=policy).fingerprint == first.fingerprint


def test_optional_dependency_is_validated_but_does_not_equal_a_hard_dependency(
    policy: PermissionPolicy,
) -> None:
    optional = deepcopy(valid_plan())
    assert isinstance(optional["nodes"], list)
    assert isinstance(optional["nodes"][2], dict)
    optional["nodes"][2]["dependencies"] = ["inventory"]
    optional["nodes"][2]["optional_dependencies"] = ["readme"]
    validated = validate_swarm_plan(optional, policy=policy)
    assert validated.topological_order == ("inventory", "readme", "synthesis")

    optional["nodes"][2]["dependencies"] = ["inventory", "readme"]
    with pytest.raises(PlanValidationError, match="both hard and optional"):
        validate_swarm_plan(optional, policy=policy)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda plan: plan["nodes"][2].update({"dependencies": ["missing"]}),
            "unknown dependency",
        ),
        (
            lambda plan: plan["nodes"][0].update({"dependencies": ["inventory"]}),
            "cannot depend on itself",
        ),
        (
            lambda plan: plan["nodes"][0].update({"dependencies": ["synthesis"]}),
            "contain a cycle",
        ),
        (
            lambda plan: plan["nodes"][1].update({"temporary_id": "inventory"}),
            "duplicate node id",
        ),
        (
            lambda plan: plan["nodes"][2].update({"dependencies": ["inventory", "inventory"]}),
            "repeats a dependency",
        ),
    ],
)
def test_plan_rejects_invalid_dependency_graph(
    policy: PermissionPolicy,
    mutate: object,
    message: str,
) -> None:
    plan = valid_plan()
    assert callable(mutate)
    mutate(plan)
    with pytest.raises(PlanValidationError, match=message):
        validate_swarm_plan(plan, policy=policy)


def test_plan_rejects_unknown_privileged_worker_skill(policy: PermissionPolicy) -> None:
    plan = valid_plan()
    assert isinstance(plan["nodes"], list)
    assert isinstance(plan["nodes"][0], dict)
    plan["nodes"][0]["required_skill"] = "process.run"
    with pytest.raises(PlanValidationError, match="unsupported worker skill"):
        validate_swarm_plan(plan, policy=policy)


def test_plan_rejects_policy_denied_worker_skill(policy: PermissionPolicy) -> None:
    rules = dict(policy.worker_skill_rules)
    rules["workspace.list_dir"] = replace(
        rules["workspace.list_dir"],
        decision="deny",
        auto_redistribute=False,
    )
    denied = PermissionPolicy(
        protected_paths=policy.protected_paths,
        process=policy.process,
        tool_rules=policy.tool_rules,
        capability_rules=policy.capability_rules,
        worker_skill_rules=rules,
    )
    with pytest.raises(PlanValidationError, match="denied by policy"):
        validate_swarm_plan(valid_plan(), policy=denied)


def test_plan_rejects_worker_skill_on_synthesis_node(policy: PermissionPolicy) -> None:
    plan = valid_plan()
    assert isinstance(plan["nodes"], list)
    assert isinstance(plan["nodes"][2], dict)
    plan["nodes"][2]["required_skill"] = "workspace.read_text"
    with pytest.raises(PlanValidationError, match="synthesis nodes cannot declare"):
        validate_swarm_plan(plan, policy=policy)


def test_plan_rejects_worker_agent_constraints_on_synthesis_node(
    policy: PermissionPolicy,
) -> None:
    plan = valid_plan()
    assert isinstance(plan["nodes"], list)
    assert isinstance(plan["nodes"][2], dict)
    plan["nodes"][2]["preferred_agent_constraints"] = {
        "agent_ids": ["worker-one"],
        "model_ids": [],
        "runtime": None,
    }
    with pytest.raises(PlanValidationError, match="preferred agent constraints"):
        validate_swarm_plan(plan, policy=policy)


@pytest.mark.parametrize("extra_field", ["status", "completed", "result", "tool_call"])
def test_plan_nodes_forbid_state_and_execution_fields(
    policy: PermissionPolicy, extra_field: str
) -> None:
    plan = valid_plan()
    assert isinstance(plan["nodes"], list)
    assert isinstance(plan["nodes"][0], dict)
    plan["nodes"][0][extra_field] = "forged"
    with pytest.raises(PlanValidationError, match="Extra inputs are not permitted"):
        validate_swarm_plan(plan, policy=policy)


def test_plan_forbids_extra_top_level_fields(policy: PermissionPolicy) -> None:
    plan = valid_plan()
    plan["claimed_completion"] = True
    with pytest.raises(PlanValidationError, match="Extra inputs are not permitted"):
        validate_swarm_plan(plan, policy=policy)


def test_plan_enforces_configured_parallelism_budget(policy: PermissionPolicy) -> None:
    with pytest.raises(PlanValidationError, match="configured parallelism"):
        validate_swarm_plan(valid_plan(), policy=policy, max_parallelism=1)


def test_json_parser_rejects_duplicate_keys_and_non_finite_values() -> None:
    with pytest.raises(PlanValidationError, match="duplicate JSON key"):
        parse_swarm_plan_json('{"schema_version":"1.0","schema_version":"1.0"}')
    with pytest.raises(PlanValidationError, match="non-finite JSON constant"):
        parse_swarm_plan_json('{"schema_version":"1.0","max_parallelism":NaN}')
