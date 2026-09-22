import json

import pytest

from app.services.goal_manager import GoalManager
from app.services.remote_job_policy import RemoteJobPolicyError, validate_remote_job
from app.services.swarm_contracts import SwarmPlanNodeProposal


def test_explicit_sqlite_parameters_survive_plan_and_dispatch() -> None:
    args = {
        "path": "customers.sqlite",
        "sql": "SELECT name FROM customers WHERE id=?",
        "parameters": [42],
    }
    node = SwarmPlanNodeProposal(
        temporary_id="query",
        node_type="worker",
        title="Read",
        objective="Read customer",
        required_skill="database.sqlite.query",
        dependencies=[],
        expected_output="Customer record",
        priority=1,
        worker_arguments=args,
    )
    result = GoalManager._payload_for_node(
        {
            "required_skill": node.required_skill,
            "objective": node.objective,
            "planner_metadata_json": json.dumps({"worker_arguments": node.worker_arguments}),
        }
    )
    assert result == args


@pytest.mark.parametrize(
    "skill,args",
    [
        ("database.sqlite.inspect", {"path": "../../state.db"}),
        ("database.sqlite.migrate", {"path": "a.db", "statements": []}),
        ("code.swift.test", {"kind": "swiftpm", "source_sha256": "invented"}),
        ("documents.extract", {"path": ".env"}),
        ("crm.command", {"operation": "messages.send", "data": {}}),
        ("crm.command", {"operation": "contacts.create", "data": {}}),
    ],
)
def test_unsafe_or_unbound_specialist_arguments_fail_before_dispatch(
    skill: str, args: dict
) -> None:
    with pytest.raises(RemoteJobPolicyError):
        validate_remote_job(skill, args)


def test_swift_requires_source_digest_and_operator_destination_key() -> None:
    args = {
        "kind": "xcode",
        "source_sha256": "a" * 64,
        "project": "App.xcodeproj",
        "scheme": "App",
        "destination": "simulator",
    }
    assert validate_remote_job("code.swift.test", args) == args


@pytest.mark.parametrize(
    "args",
    [
        {"operation": "tasks.update", "data": {}, "idempotency_key": "valid-key-01"},
        {"operation": "tasks.create", "data": [], "idempotency_key": "valid-key-01"},
        {"operation": "tasks.create", "data": {}, "idempotency_key": "\nheader-injection"},
        {"operation": "tasks.search", "limit": True},
        {"operation": ["tasks.search"]},
    ],
)
def test_crm_rejects_malformed_operation_before_network(args: dict) -> None:
    with pytest.raises(RemoteJobPolicyError):
        validate_remote_job("crm.command", args)


def test_specialist_plan_requires_arguments_and_generation_schema_constrains_them() -> None:
    from jsonschema import Draft202012Validator

    from app.services.planner_provider import UbuntuSwarmPlannerProvider
    from tests.test_project_plan_shape import _node, _plan

    raw = _node("inspect", "database.sqlite.inspect")
    with pytest.raises(ValueError, match="explicit operation arguments"):
        SwarmPlanNodeProposal.model_validate(raw)
    raw["worker_arguments"] = {"path": "customers.sqlite"}
    assert SwarmPlanNodeProposal.model_validate(raw).worker_arguments == raw["worker_arguments"]
    raw["00_required_skill"] = raw.pop("required_skill")
    validator = Draft202012Validator(
        UbuntuSwarmPlannerProvider._response_format()["json_schema"]["schema"]
    )
    assert validator.is_valid(_plan([raw]))
    raw["worker_arguments"] = {"shell": "arbitrary"}
    assert not validator.is_valid(_plan([raw]))
