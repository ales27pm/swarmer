import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import (
    PlanValidationError,
    parse_swarm_plan_json,
    validate_evaluation_decision,
    validate_swarm_plan,
)
from tests.test_goal_context_payloads import _synthesis_plan
from tests.test_goal_runtime_recovery import _worker_plan


@pytest.fixture
def policy() -> PermissionPolicy:
    return PermissionPolicy.from_yaml(
        Path(__file__).resolve().parents[2] / "configs/permissions.yaml"
    )


@pytest.mark.parametrize("available", [[], ["code.build_project"]])
def test_plan_rejects_a_supported_but_unpresented_skill_without_rewriting(
    policy: PermissionPolicy, available: list[str]
) -> None:
    plan = _worker_plan().model_dump(mode="json")
    original = deepcopy(plan)
    with pytest.raises(PlanValidationError, match="available capabilities"):
        validate_swarm_plan(plan, policy=policy, available_skills=available)
    with pytest.raises(PlanValidationError, match="available capabilities"):
        parse_swarm_plan_json(json.dumps(plan), available_skills=available)
    assert plan == original


@pytest.mark.parametrize("available", [None, ["workspace.list_dir"]])
def test_known_or_legacy_unknown_capabilities_preserve_worker_plan(
    policy: PermissionPolicy, available: list[str] | None
) -> None:
    plan = _worker_plan()
    assert validate_swarm_plan(plan, policy=policy, available_skills=available).proposal == plan


def test_empty_capabilities_allow_synthesis_without_a_worker(policy: PermissionPolicy) -> None:
    plan = _synthesis_plan("Summarize available evidence", suffix="none")
    assert validate_swarm_plan(plan, policy=policy, available_skills=[]).proposal == plan


def test_evaluator_extension_cannot_invent_an_unpresented_worker(policy: PermissionPolicy) -> None:
    decision = {
        "schema_version": "1.0",
        "status": "continue",
        "reason_summary": "Collect more evidence",
        "missing_requirements": [],
        "invalid_results": [],
        "suggested_new_nodes": [_worker_plan().nodes[0].model_dump(mode="json")],
    }
    with pytest.raises(PlanValidationError, match="available capabilities"):
        validate_evaluation_decision(decision, policy=policy, available_skills=[])
    assert (
        validate_evaluation_decision(
            decision, policy=policy, available_skills=["workspace.list_dir"]
        )
        .decision.suggested_new_nodes[0]
        .required_skill
        == "workspace.list_dir"
    )
