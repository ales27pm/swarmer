from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.services.agent_card import MEDIA_SKILLS, SUPPORTED_AGENT_SKILLS
from app.services.evaluator_provider import UbuntuEvaluatorProvider
from app.services.planner_provider import UbuntuSwarmPlannerProvider
from app.services.specialist_contracts import SPECIALIST_SKILLS
from app.services.swarm_contracts import SwarmPlanNodeProposal
from tests.test_evaluator import continue_decision, wire_decision
from tests.test_planner_provider import _graph_wire_proposal, _proposal


def schema_and_wire(consumer: str, node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    skill = node["required_skill"]
    available = [skill] if skill else []
    if consumer == "planner":
        raw = _proposal(skill)
        raw["nodes"] = [node]
        schema = UbuntuSwarmPlannerProvider._response_format(available_skills=available)
        wire = _graph_wire_proposal(raw)
    else:
        raw = continue_decision()
        raw["suggested_new_nodes"] = [node]
        schema = UbuntuEvaluatorProvider._response_format(available)
        wire = wire_decision(raw)
    return schema["json_schema"]["schema"], wire


@pytest.mark.parametrize("consumer", ["planner", "evaluator"])
@pytest.mark.parametrize(
    "skill",
    [
        None,
        *sorted(
            SUPPORTED_AGENT_SKILLS
            - SPECIALIST_SKILLS
            - MEDIA_SKILLS
            - {"research.collect", "workspace.read_text"}
        ),
    ],
)
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
        ("image.generate", {"prompt": "blue", "width": 512, "height": 512, "steps": 4, "seed": 42}),
        (
            "audio.synthesize",
            {
                "text": "Bonjour",
                "language": "fr-FR",
                "voice": "ff_siwis",
                "max_duration_seconds": 30,
            },
        ),
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


@pytest.mark.parametrize("consumer", ["planner", "evaluator"])
def test_workspace_read_path_reaches_dispatch_payload(consumer: str) -> None:
    """Wire grammar -> public parser -> persisted metadata -> dispatch payload."""
    import json

    from app.services.goal_manager import GoalManager

    node = deepcopy(_proposal("workspace.read_text")["nodes"][0])
    node["worker_arguments"] = {"path": "notes.txt"}
    schema, wire = schema_and_wire(consumer, node)
    Draft202012Validator(schema).validate(wire)
    parsed = SwarmPlanNodeProposal.model_validate(node)
    payload = GoalManager._payload_for_node(
        {
            "required_skill": parsed.required_skill,
            "objective": parsed.objective,
            "planner_metadata_json": json.dumps({"worker_arguments": parsed.worker_arguments}),
        }
    )
    assert payload == {"path": "notes.txt"}


@pytest.mark.parametrize("consumer", ["planner", "evaluator"])
@pytest.mark.parametrize(
    "arguments",
    [
        None,
        {},
        {"path": ""},
        {"path": 12},
        {"path": "notes.txt", "start_line": 1},
        {"path": "notes.txt", "end_line": 200},
        {"path": "notes.txt", "max_bytes": 32768},
        {"path": "notes.txt", "capability_request": {}},
    ],
)
def test_workspace_read_wire_requires_only_explicit_path(consumer: str, arguments: Any) -> None:
    node = deepcopy(_proposal("workspace.read_text")["nodes"][0])
    node["worker_arguments"] = arguments
    schema, wire = schema_and_wire(consumer, node)
    assert not Draft202012Validator(schema).is_valid(wire)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "/etc/passwd",
        ".env",
        "private.key",
        "a" * 501,
        "src/access_token.txt",
        "src\\notes.txt",
        "https://example.test/notes.txt",
    ],
)
def test_workspace_read_fix_does_not_weaken_runtime_path_policy(path: str) -> None:
    from app.services.goal_manager import GoalManager, GoalManagerConflict

    with pytest.raises(GoalManagerConflict):
        GoalManager._payload_for_node(
            {
                "required_skill": "workspace.read_text",
                "objective": "Read a file.",
                "planner_metadata": {"worker_arguments": {"path": path}},
            }
        )


@pytest.mark.parametrize("consumer", ["planner", "evaluator"])
@pytest.mark.parametrize("skills", [[], ["writing.draft"], ["workspace.read_text"], None])
def test_workspace_read_has_no_missing_arguments_or_unadvertised_branch(
    consumer: str, skills: list[str] | None
) -> None:
    node = deepcopy(_proposal("workspace.read_text")["nodes"][0])
    node["worker_arguments"] = {"path": "notes.txt"}
    _, wire = schema_and_wire(consumer, node)
    if consumer == "planner":
        response_format = UbuntuSwarmPlannerProvider._response_format(available_skills=skills)
    else:
        response_format = UbuntuEvaluatorProvider._response_format(skills)
    validator = Draft202012Validator(response_format["json_schema"]["schema"])
    assert validator.is_valid(wire) is (skills is None or "workspace.read_text" in skills)
    node.pop("worker_arguments")
    _, missing = schema_and_wire(consumer, node)
    assert not validator.is_valid(missing)


@pytest.mark.parametrize("length", [500, 501])
def test_workspace_read_length_is_enforced_after_wire_grammar(length: int) -> None:
    # The Ollama dialect omits maxLength to avoid enormous llama.cpp grammars.
    # Both public validation and persisted dispatch must retain the real bound.
    from app.services.goal_manager import GoalManager, GoalManagerConflict

    node = deepcopy(_proposal("workspace.read_text")["nodes"][0])
    node["worker_arguments"] = {"path": "a" * length}
    schema, wire = schema_and_wire("planner", node)
    Draft202012Validator(schema).validate(wire)
    persisted = {
        "required_skill": "workspace.read_text",
        "objective": "Read a file.",
        "planner_metadata": {"worker_arguments": node["worker_arguments"]},
    }
    if length == 500:
        assert (
            SwarmPlanNodeProposal.model_validate(node).worker_arguments == node["worker_arguments"]
        )
        assert GoalManager._payload_for_node(persisted) == node["worker_arguments"]
    else:
        with pytest.raises(ValidationError):
            SwarmPlanNodeProposal.model_validate(node)
        with pytest.raises(GoalManagerConflict):
            GoalManager._payload_for_node(persisted)
