from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.execution_engine import ExecutionError


@pytest.mark.parametrize("diagnostic", ["unknown_tool", "invalid_arguments", "policy_denied"])
def test_execution_error_diagnostic_is_fixed_safe_metadata(diagnostic: str) -> None:
    error = ExecutionError("private exception detail", diagnostic=diagnostic)
    assert error.diagnostic == diagnostic
    assert str(error) == "private exception detail"


def test_execution_error_rejects_arbitrary_diagnostic_text() -> None:
    with pytest.raises(ValueError):
        ExecutionError("detail", diagnostic="private-user-input")


@pytest.mark.parametrize("route", ["direct", "planned"])
@pytest.mark.parametrize("defect", ["unknown_tool", "invalid_arguments", "policy_denied"])
def test_tool_validation_codes_are_public_without_model_values(
    client: TestClient,
    test_app: FastAPI,
    paired_headers: dict[str, str],
    route: str,
    defect: str,
) -> None:
    hidden = "private-model-value-do-not-publish"
    proposal = {"tool_name": "workspace.list_dir", "arguments": {"path": "."}, "summary": hidden}
    if defect == "unknown_tool":
        proposal["tool_name"] = "code.swift." + hidden
    elif defect == "invalid_arguments":
        proposal["arguments"]["source_sha256"] = hidden
    else:
        # Protected paths are also a policy refusal, before any public proposal event.
        proposal["arguments"]["path"] = ".env." + hidden
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "Validate a bounded harmless proposal"}
    ).json()
    if route == "planned":
        test_app.state.orchestrator_service.plan = AsyncMock(return_value=proposal)
        response = client.post(f"/tasks/{task['id']}/plan", headers=paired_headers)
    else:
        response = client.post(
            f"/tasks/{task['id']}/tool-calls", headers=paired_headers, json=proposal
        )
    assert response.status_code == 400
    assert response.json() == {"detail": "tool proposal failed executor validation"}
    assert response.headers["X-MonGARS-Validation-Code"] == defect
    audit = client.get("/audit?limit=100", headers=paired_headers).json()
    assert hidden not in json.dumps(
        {"response": response.json(), "headers": dict(response.headers), "audit": audit}
    )
    if route == "planned":
        rejected = [event for event in audit if event["event_type"] == "orchestrator.rejected"]
        assert len(rejected) == 1
        assert rejected[0]["payload"]["diagnostic"] == defect
        assert not any(event["event_type"] == "orchestrator.proposed" for event in audit)
    assert client.get(f"/tasks/{task['id']}/tool-calls", headers=paired_headers).json() == []


def test_explicit_tool_policy_denial_has_its_own_code(
    client: TestClient,
    test_app: FastAPI,
    paired_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = test_app.state.execution_engine
    policy = engine.policy
    original = policy.evaluate_tool
    monkeypatch.setattr(
        policy, "evaluate_tool", lambda name: replace(original(name), decision="deny")
    )
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "Try denied read-only listing"}
    ).json()
    result = client.post(
        f"/tasks/{task['id']}/tool-calls",
        headers=paired_headers,
        json={
            "tool_name": "workspace.list_dir",
            "arguments": {"path": "."},
            "summary": "Proposed listing",
        },
    )
    assert result.status_code == 400
    assert result.headers["X-MonGARS-Validation-Code"] == "policy_denied"
    assert client.get(f"/tasks/{task['id']}/tool-calls", headers=paired_headers).json() == []
