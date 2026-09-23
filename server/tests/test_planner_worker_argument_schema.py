from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.services.agent_card import SUPPORTED_AGENT_SKILLS
from app.services.evaluator_provider import UbuntuEvaluatorProvider
from app.services.planner_provider import UbuntuSwarmPlannerProvider
from app.services.specialist_contracts import SPECIALIST_SKILLS
from app.services.swarm_contracts import SwarmPlanNodeProposal
from tests.test_evaluator import continue_decision, wire_decision
from tests.test_planner_provider import _proposal, _wire_proposal


def schema_and_wire(consumer: str, node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    skill = node["required_skill"]
    available = [skill] if skill else []
    if consumer == "planner":
        raw = _proposal(skill)
        raw["nodes"] = [node]
        schema = UbuntuSwarmPlannerProvider._response_format(available_skills=available)
        wire = _wire_proposal(raw)
    else:
        raw = continue_decision()
        raw["suggested_new_nodes"] = [node]
        schema = UbuntuEvaluatorProvider._response_format(available)
        wire = wire_decision(raw)
    return schema["json_schema"]["schema"], wire


@pytest.mark.parametrize("consumer", ["planner", "evaluator"])
@pytest.mark.parametrize("skill", [None, *sorted(SUPPORTED_AGENT_SKILLS - SPECIALIST_SKILLS)])
def test_server_derived_worker_arguments_cannot_be_invented_by_the_grammar(
    consumer: str, skill: str | None
) -> None:
    node = deepcopy(_proposal(skill)["nodes"][0])
    node["worker_arguments"] = {"unexpected": "not an operation payload"}
    with pytest.raises(ValidationError):
        SwarmPlanNodeProposal.model_validate(node)
    schema, wire = schema_and_wire(consumer, node)
    validator = Draft202012Validator(schema)
    assert not validator.is_valid(wire)
    for explicit_null in (False, True):
        compatible = deepcopy(node)
        if explicit_null:
            compatible["worker_arguments"] = None
        else:
            compatible.pop("worker_arguments")
        assert SwarmPlanNodeProposal.model_validate(compatible).worker_arguments is None
        _, wire = schema_and_wire(consumer, compatible)
        validator.validate(wire)


@pytest.mark.parametrize("consumer", ["planner", "evaluator"])
@pytest.mark.parametrize(
    ("skill", "arguments"),
    [
        ("database.sqlite.inspect", {"path": "notes.sqlite"}),
        ("database.sqlite.query", {"path": "notes.sqlite", "sql": "SELECT 1"}),
        ("database.sqlite.backup", {"path": "notes.sqlite", "destination": "notes-backup.sqlite"}),
        (
            "database.sqlite.create",
            {
                "path": "notes.sqlite",
                "migration_id": "v1",
                "statements": [{"sql": "CREATE TABLE notes (id INTEGER PRIMARY KEY)"}],
            },
        ),
        (
            "database.sqlite.migrate",
            {
                "path": "notes.sqlite",
                "migration_id": "v2",
                "statements": [{"sql": "CREATE TABLE labels (id INTEGER PRIMARY KEY)"}],
            },
        ),
        ("documents.extract", {"path": "notes.txt"}),
        ("crm.command", {"operation": "contacts.search", "query": ""}),
        ("code.swift.build", {"kind": "swiftpm", "source_sha256": "a" * 64}),
        ("code.swift.test", {"kind": "swiftpm", "source_sha256": "a" * 64}),
    ],
)
def test_specialist_arguments_remain_required_and_bounded(
    consumer: str, skill: str, arguments: dict[str, Any]
) -> None:
    node = deepcopy(_proposal(skill)["nodes"][0])
    node["worker_arguments"] = arguments
    SwarmPlanNodeProposal.model_validate(node)
    schema, wire = schema_and_wire(consumer, node)
    Draft202012Validator(schema).validate(wire)
    for invalid in (None, {"unexpected": "not an operation payload"}):
        node["worker_arguments"] = invalid
        _, wire = schema_and_wire(consumer, node)
        assert not Draft202012Validator(schema).is_valid(wire)


@pytest.mark.parametrize(
    ("skill", "arguments"),
    [
        ("workspace.read_text", {"path": "notes.txt"}),
        ("research.query", {"query": "SQLite documentation", "max_results": 5}),
        ("code.generate_python", {"objective": "Create a notes parser."}),
    ],
)
def test_public_parser_still_accepts_valid_legacy_non_specialist_arguments(
    skill: str, arguments: dict[str, Any]
) -> None:
    node = deepcopy(_proposal(skill)["nodes"][0])
    node["worker_arguments"] = arguments
    assert SwarmPlanNodeProposal.model_validate(node).worker_arguments == arguments
