import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.services.approval_binding import public_tool_arguments
from app.services.event_privacy import safe_websocket_event

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_URI = "https://27pm.org/openapi.yaml"


def validate_json_schema(name: str, instance: Any) -> None:
    schema = json.loads((REPO_ROOT / "schemas" / name).read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)


def validate_openapi_component(name: str, instance: Any) -> None:
    document = yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    registry = Registry().with_resource(
        OPENAPI_URI,
        Resource(contents=document, specification=DRAFT202012),
    )
    validator = Draft202012Validator(
        {"$ref": f"{OPENAPI_URI}#/components/schemas/{name}"},
        registry=registry,
        format_checker=FormatChecker(),
    )
    validator.validate(instance)


def test_current_resource_schemas_validate_live_api_payloads(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "exercise published schemas"}
    ).json()
    proposal = client.post(
        f"/tasks/{task['id']}/tool-calls",
        headers=paired_headers,
        json={
            "tool_name": "workspace.write_text",
            "arguments": {"path": "schema-proof.txt", "content": "proof"},
            "summary": "Write schema proof",
        },
    ).json()
    detail = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()
    validate_json_schema("tool-call.schema.json", proposal)
    validate_openapi_component("ToolCall", proposal)
    public_process_projection = {
        **proposal,
        "tool_name": "process.run",
        "summary": "Run a sandboxed process",
        "arguments": {
            "argv": ["<redacted>"],
            "argument_count": 3,
            "arguments_redacted": True,
        },
    }
    validate_json_schema("tool-call.schema.json", public_process_projection)
    validate_openapi_component("ToolCall", public_process_projection)
    empty_process_projection = {
        **public_process_projection,
        "arguments": public_tool_arguments("process.run", None),
    }
    validate_json_schema("tool-call.schema.json", empty_process_projection)
    validate_openapi_component("ToolCall", empty_process_projection)
    malformed_legacy_write_projection = {
        **proposal,
        "arguments": public_tool_arguments(
            "workspace.write_text",
            {"path": {"private": "value"}, "content": ["private-value"]},
        ),
    }
    validate_json_schema("tool-call.schema.json", malformed_legacy_write_projection)
    validate_openapi_component("ToolCall", malformed_legacy_write_projection)
    assert "private-value" not in json.dumps(malformed_legacy_write_projection)
    public_process_completion = {
        **public_process_projection,
        "status": "completed",
        "result": {
            "output_redacted": True,
            "returncode": 0,
            "stdout": "<redacted: 2 UTF-8 bytes>",
            "stderr": "<redacted: 0 UTF-8 bytes>",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "sandbox": "bubblewrap",
            "network": "denied",
        },
    }
    validate_json_schema("tool-call.schema.json", public_process_completion)
    validate_openapi_component("ToolCall", public_process_completion)
    completed_without_result = {**proposal, "status": "completed", "result": None}
    process_completed_without_result = {
        **public_process_projection,
        "status": "completed",
        "result": None,
    }
    raw_write_projection = {
        **proposal,
        "arguments": {
            "path": "schema-proof.txt",
            "content": "proof",
            "arguments_redacted": True,
        },
    }
    raw_process_projection = {
        **proposal,
        "tool_name": "process.run",
        "arguments": {
            "argv": ["pytest", "-k", "credential-like-process-argument"],
            "cwd": ".",
            "timeout_seconds": 20,
        },
    }
    mismatched_process_projection = {
        **proposal,
        "tool_name": "process.run",
        "arguments": {"path": "schema-proof.txt"},
    }
    raw_process_result = {
        **public_process_completion,
        "result": {
            **public_process_completion["result"],
            "stdout": "credential-like-process-argument",
        },
    }
    mismatched_public_summary = {
        **proposal,
        "summary": "Run a sandboxed process",
    }
    for unsafe_projection in (
        completed_without_result,
        process_completed_without_result,
        raw_write_projection,
        raw_process_projection,
        mismatched_process_projection,
        raw_process_result,
        mismatched_public_summary,
    ):
        with pytest.raises(ValidationError):
            validate_json_schema("tool-call.schema.json", unsafe_projection)
        with pytest.raises(ValidationError):
            validate_openapi_component("ToolCall", unsafe_projection)
    approval = detail["approvals"][0]
    validate_json_schema("permission-request.schema.json", approval)
    mismatched_approval_summary = {
        **approval,
        "summary": "Run a sandboxed process",
    }
    with pytest.raises(ValidationError):
        validate_json_schema("permission-request.schema.json", mismatched_approval_summary)
    with pytest.raises(ValidationError):
        validate_openapi_component("Approval", mismatched_approval_summary)

    contradictory = {
        **approval,
        "requester": None,
        "policy": None,
        "affected_data_summary": None,
        "audit_id": None,
        "consent_context_valid": True,
    }
    with pytest.raises(ValidationError):
        validate_json_schema("permission-request.schema.json", contradictory)
    with pytest.raises(ValidationError):
        validate_openapi_component("Approval", contradictory)

    invalid_approval_shapes = (
        {**approval, "action_digest": ""},
        {
            **approval,
            "action": "workspace.write_text ",
            "summary": "Write text to a workspace file",
        },
        {
            **approval,
            "action_preview": {
                key: value for key, value in approval["action_preview"].items() if key != "target"
            },
        },
        {
            **approval,
            "action_preview": {**approval["action_preview"], "operation": ""},
        },
        {
            **approval,
            "action_preview": {**approval["action_preview"], "details": [""]},
        },
        {
            **approval,
            "action_preview": {**approval["action_preview"], "command": ["pytest"]},
        },
        {
            **approval,
            "action_preview": {**approval["action_preview"], "arguments_redacted": False},
        },
    )
    for invalid_approval in invalid_approval_shapes:
        with pytest.raises(ValidationError):
            validate_json_schema("permission-request.schema.json", invalid_approval)
        with pytest.raises(ValidationError):
            validate_openapi_component("Approval", invalid_approval)

    decided = client.post(
        f"/approvals/{approval['id']}/decision",
        headers=paired_headers,
        json={"decision": "deny", "user_note": "contract proof"},
    ).json()
    validate_json_schema("permission-request.schema.json", decided["approval"])
    validate_openapi_component("ApprovalToolResult", decided)

    monkeypatch.setattr(
        test_app.state.execution_engine.process_sandbox,
        "validate",
        lambda argv: None,
    )
    process_task = client.post(
        "/tasks", headers=paired_headers, json={"input": "validate process approval schema"}
    ).json()
    process_proposal = client.post(
        f"/tasks/{process_task['id']}/tool-calls",
        headers=paired_headers,
        json={
            "tool_name": "process.run",
            "arguments": {"argv": ["pytest"], "cwd": "server/tests", "timeout_seconds": 20},
            "summary": "Run server tests",
        },
    ).json()
    process_detail = client.get(f"/tasks/{process_task['id']}", headers=paired_headers).json()
    process_approval = process_detail["approvals"][0]
    assert process_approval["action_preview"]["working_directory"] == "server/tests"
    validate_json_schema("permission-request.schema.json", process_approval)
    validate_openapi_component("Approval", process_approval)
    invalid_process_approval = {
        **process_approval,
        "action_preview": {**process_approval["action_preview"], "command": []},
    }
    with pytest.raises(ValidationError):
        validate_json_schema("permission-request.schema.json", invalid_process_approval)
    with pytest.raises(ValidationError):
        validate_openapi_component("Approval", invalid_process_approval)
    validate_json_schema("tool-call.schema.json", process_proposal)
    validate_openapi_component("ToolCall", process_proposal)

    memory = client.post(
        "/memory", headers=paired_headers, json={"content": "Schema-backed memory"}
    ).json()
    validate_json_schema("memory-record.schema.json", memory)

    registration = client.post(
        "/agents/register",
        headers=paired_headers,
        json={
            "name": "Schema agent",
            "endpoint": "http://127.0.0.1:9001",
            "skills": ["workspace.list_dir"],
        },
    ).json()
    registration.pop("credential")
    validate_json_schema("agent-card.schema.json", registration)

    feedback = client.post(
        "/feedback",
        headers=paired_headers,
        json={"task_id": task["id"], "score": 5, "type": "rating"},
    ).json()
    validate_json_schema("feedback-event.schema.json", feedback)

    bootstrap = client.get("/sync/bootstrap", headers=paired_headers).json()
    validate_openapi_component("Bootstrap", bootstrap)


def test_openapi_declares_executor_validation_failure_for_plan_route() -> None:
    document = yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    responses = document["paths"]["/tasks/{task_id}/plan"]["post"]["responses"]
    assert responses["400"]["description"] == "Model tool proposal failed executor validation"


def test_openapi_keeps_legacy_unchained_audit_rows_readable() -> None:
    validate_openapi_component(
        "AuditEvent",
        {
            "id": 1,
            "trace_id": None,
            "event_type": "tool.failed",
            "actor_type": None,
            "actor_id": None,
            "task_id": None,
            "payload": {"tool_call_id": "call_legacy"},
            "prev_hash": None,
            "hash": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )


def test_websocket_event_matches_current_envelope_schema(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    ticket = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]
    with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
        validate_json_schema("event-envelope.schema.json", websocket.receive_json())


def test_all_committed_json_schemas_are_valid_and_roadmap_is_explicit() -> None:
    for path in sorted((REPO_ROOT / "schemas").glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    sync_schema = json.loads(
        (REPO_ROOT / "schemas" / "sync-operation.schema.json").read_text(encoding="utf-8")
    )
    assert "Roadmap-only" in sync_schema["$comment"]


def test_openapi_covers_runtime_transport_statuses_and_response_credentials() -> None:
    document = yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    assert document["info"]["version"] == "0.14.0"
    runtime_status = document["components"]["schemas"]["RuntimeStatus"]
    assert runtime_status["additionalProperties"] is False
    assert runtime_status["properties"]["version"] == {"const": "0.14.0"}
    assert {
        "redis_reconnect_count",
        "redis_last_error_category",
        "outbox_duplicate_publications",
        "outbox_claim_expirations",
        "outbox_publish_latency_ms_count",
        "outbox_publish_latency_ms_total",
        "outbox_publish_latency_ms_max",
        "quarantined_jobs",
        "maintenance_lease_renewal_failures",
        "vector_generation_age_seconds",
    }.issubset(runtime_status["required"])
    assert runtime_status["properties"]["redis_last_error_category"]["type"] == [
        "string",
        "null",
    ]
    assert set(runtime_status["properties"]["redis_last_error_category"]["enum"]) == {
        "timeout",
        "connection",
        "authentication",
        "tls",
        "publication",
        "dedupe_conflict",
        "protocol",
        None,
    }
    assert runtime_status["properties"]["vector_generation_age_seconds"]["type"] == [
        "number",
        "null",
    ]
    assert (
        "quarantined"
        in document["components"]["schemas"]["AgentJob"]["properties"]["status"]["enum"]
    )
    assert not {
        "redis_url",
        "password",
        "credential",
        "bearer_token",
        "grant_token",
        "lease_token",
    }.intersection(runtime_status["properties"])
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            security = operation.get("security", document.get("security", []))
            if security in ([{"bearerAuth": []}], [{"agentBearer": []}]):
                assert "426" in operation["responses"], f"{method.upper()} {path}"

    tool_proposal = document["paths"]["/tasks/{task_id}/tool-calls"]["post"]
    assert "409" in tool_proposal["responses"]
    assert document["components"]["schemas"]["PairingCandidateResponse"]["properties"][
        "candidate_token"
    ] == {
        "type": "string",
        "minLength": 32,
        "readOnly": True,
    }
    assert document["components"]["schemas"]["AgentRegistration"]["allOf"][1]["properties"][
        "credential"
    ] == {"type": "string", "readOnly": True}


def test_goal_openapi_contract_is_strict_and_accepts_live_public_projection(
    client: TestClient,
    paired_headers: dict[str, str],
) -> None:
    document = yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    created = client.post(
        "/goals",
        headers=paired_headers,
        json={
            "objective": "Verify the public goal contract",
            "autonomy_profile": "assisted",
            "completion_criteria": ["The public response validates"],
        },
    )

    assert created.status_code == 201
    validate_openapi_component("GoalDetail", created.json())
    validate_openapi_component("GoalStartRequest", {})
    with pytest.raises(ValidationError):
        validate_openapi_component("GoalStartRequest", {"planner_source": "manual"})
    with pytest.raises(ValidationError):
        validate_openapi_component(
            "GoalCreateRequest",
            {"objective": "No hidden autonomy", "unknown_authority": True},
        )

    expected_operations = {
        ("/goals", "get"),
        ("/goals", "post"),
        ("/goals/{goal_id}", "get"),
        ("/goals/{goal_id}/start", "post"),
        ("/goals/{goal_id}/cancel", "post"),
        ("/goals/{goal_id}/replan", "post"),
        ("/goals/{goal_id}/nodes", "get"),
        ("/goals/{goal_id}/result", "get"),
        ("/goals/{goal_id}/feedback", "post"),
    }
    assert expected_operations.issubset(
        {
            (path, method)
            for path, item in document["paths"].items()
            for method in item
            if method in {"get", "post"}
        }
    )
    for name in (
        "GoalRecord",
        "PlanNode",
        "GoalResult",
        "GoalDetail",
        "GoalCreateRequest",
        "GoalStartRequest",
        "GoalReplanRequest",
        "GoalCancelRequest",
        "GoalFeedbackRequest",
        "GoalFeedbackRecord",
    ):
        assert document["components"]["schemas"][name]["additionalProperties"] is False


def test_live_goal_response_models_are_closed_at_every_public_boundary(test_app) -> None:
    """FastAPI must enforce the strict records, not only the checked-in spec."""

    document = test_app.openapi()
    schemas = document["components"]["schemas"]
    for name in (
        "GoalRecord",
        "PlanNode",
        "GoalResult",
        "GoalDetail",
        "GoalFeedbackRecord",
    ):
        assert schemas[name]["additionalProperties"] is False

    assert schemas["GoalDetail"]["properties"]["goal"] == {
        "$ref": "#/components/schemas/GoalRecord"
    }
    assert schemas["GoalDetail"]["properties"]["nodes"]["items"] == {
        "$ref": "#/components/schemas/PlanNode"
    }
    assert {
        variant.get("$ref") for variant in schemas["GoalDetail"]["properties"]["result"]["anyOf"]
    } == {"#/components/schemas/GoalResult", None}

    paths = document["paths"]
    detail_operations = (
        ("/goals", "post", "201"),
        ("/goals/{goal_id}", "get", "200"),
        ("/goals/{goal_id}/start", "post", "200"),
        ("/goals/{goal_id}/cancel", "post", "200"),
        ("/goals/{goal_id}/replan", "post", "200"),
    )
    for path, method, status_code in detail_operations:
        response_schema = paths[path][method]["responses"][status_code]["content"][
            "application/json"
        ]["schema"]
        assert response_schema == {"$ref": "#/components/schemas/GoalDetail"}

    listed_schema = paths["/goals"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert listed_schema["items"] == {"$ref": "#/components/schemas/GoalRecord"}
    node_schema = paths["/goals/{goal_id}/nodes"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert node_schema["items"] == {"$ref": "#/components/schemas/PlanNode"}
    feedback_schema = paths["/goals/{goal_id}/feedback"]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"]
    assert feedback_schema == {"$ref": "#/components/schemas/GoalFeedbackRecord"}

    result_variants = paths["/goals/{goal_id}/result"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["anyOf"]
    assert {variant.get("$ref") for variant in result_variants} == {
        "#/components/schemas/GoalResult",
        None,
    }


def test_goal_websocket_openapi_contract_describes_refetch_invalidations() -> None:
    document = yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    payloads = document["x-websocket-endpoints"]["/ws"]["goalPayloads"]
    cases = (
        (
            "goal.updated",
            "GoalUpdatedNotification",
            {
                "id": "goal_contract",
                "root_task_id": "tsk_contract",
                "status": "running",
                "current_phase": "evaluation",
                "updated_at": "2030-01-01T00:00:00+00:00",
                "completed_at": None,
                "objective": "must remain behind authenticated REST",
            },
        ),
        (
            "plan.node.updated",
            "PlanNodeUpdatedNotification",
            {
                "id": "node_contract",
                "goal_run_id": "goal_contract",
                "status": "completed",
                "updated_at": "2030-01-01T00:01:00+00:00",
                "completed_at": "2030-01-01T00:01:00+00:00",
                "result_summary": "must remain behind authenticated REST",
            },
        ),
        (
            "goal.result.updated",
            "GoalResultUpdatedNotification",
            {
                "goal_run_id": "goal_contract",
                "root_task_id": "tsk_contract",
                "status": "completed",
                "completed_at": "2030-01-01T00:02:00+00:00",
                "answer": "must remain behind authenticated REST",
            },
        ),
    )

    for event_type, component, source_payload in cases:
        assert payloads[event_type] == {"$ref": f"#/components/schemas/{component}"}
        projected = safe_websocket_event({"type": event_type, "payload": source_payload})
        validate_openapi_component(component, projected["payload"])
        assert projected["payload"]["refetch_required"] is True
        assert "must remain behind authenticated REST" not in json.dumps(projected)


def test_agent_endpoint_openapi_boundaries_match_fastapi(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    prefix = "https://example.test/"
    maximum_endpoint = prefix + ("a" * (2_083 - len(prefix)))
    validate_openapi_component(
        "AgentCreate",
        {
            "name": "Maximum URL",
            "endpoint": maximum_endpoint,
            "skills": ["workspace.list_dir"],
        },
    )
    accepted = client.post(
        "/agents/register",
        headers=paired_headers,
        json={
            "name": "Maximum URL",
            "endpoint": maximum_endpoint,
            "skills": ["workspace.list_dir"],
        },
    )
    assert accepted.status_code == 201

    too_long = client.post(
        "/agents/register",
        headers=paired_headers,
        json={"name": "Too long", "endpoint": f"{maximum_endpoint}a"},
    )
    assert too_long.status_code == 422
    assert (
        client.post(
            "/agents/register",
            headers=paired_headers,
            json={"name": "Wrong scheme", "endpoint": "ftp://example.test/agent"},
        ).status_code
        == 422
    )
