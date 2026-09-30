"""Published operation boundaries match the installed API, including binary responses."""

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = importlib.util.spec_from_file_location(
    "published_openapi_validator", REPO_ROOT / "scripts" / "validate_openapi.py"
)
assert SCRIPT is not None and SCRIPT.loader is not None
validator = importlib.util.module_from_spec(SCRIPT)
SCRIPT.loader.exec_module(validator)

SCREENSHOT = "/website-projects/{project_id}/screenshots/{sha256}"
PROJECT_SOURCE = "/agents/{agent_id}/jobs/{job_id}/project-source"


@pytest.fixture(scope="module")
def published() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text())


def test_published_contract_covers_all_installed_operations(
    published: dict[str, Any], test_app: FastAPI
) -> None:
    generated = test_app.openapi()
    assert validator.operations(published) == validator.operations(generated)
    validator.validate_operation_contracts(published, generated)
    validator.validate_component_contracts(published, generated)


def test_new_public_models_preserve_runtime_constraints(
    published: dict[str, Any], test_app: FastAPI
) -> None:
    generated = test_app.openapi()
    for name in (
        "ActivityCoverage",
        "ActivityDetail",
        "ActivityItem",
        "ActivityPage",
        "ActivityScope",
        "CaptureLimits",
        "MemoryUsagePage",
        "MemoryUsageEntry",
        "MemoryUsageItem",
        "MemoryUsageRetrieval",
        "EvidenceCriterion",
        "EvidenceMapping",
        "EvidenceMappingRequest",
        "GraphCheck",
        "GraphCoverage",
        "GraphCriterion",
        "GraphDependency",
        "GraphEvaluation",
        "GraphFile",
        "GraphPlanningDecision",
        "GraphRevision",
        "ProjectGraph",
        "RequirementEvidenceView",
        "SwiftProjectValidationRequest",
        "WebsiteCommand",
        "WebsiteCreate",
        "WebsitePublish",
        "WebsiteReview",
    ):
        assert (
            published["components"]["schemas"][name] == generated["components"]["schemas"][name]
        ), name


@pytest.mark.parametrize(
    "content",
    [
        {},
        {"application/json": {"schema": {}}},
        {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        {"image/png": {"schema": {"type": "object"}}},
        {
            "image/png": {"schema": {"type": "string", "format": "binary"}},
            "application/json": {"schema": {}},
        },
    ],
)
def test_screenshot_contract_cannot_regress_to_json_or_untyped_bytes(
    published: dict[str, Any], test_app: FastAPI, content: dict[str, Any]
) -> None:
    broken = copy.deepcopy(published)
    broken["paths"][SCREENSHOT]["get"]["responses"]["200"]["content"] = content
    with pytest.raises(ValueError, match="screenshot response"):
        validator.validate_operation_contracts(broken, test_app.openapi())


def test_other_success_responses_still_require_json(
    published: dict[str, Any], test_app: FastAPI
) -> None:
    broken = copy.deepcopy(published)
    broken["paths"]["/goals/{goal_id}/activity"]["get"]["responses"]["200"]["content"] = {
        "image/png": {"schema": {"type": "string", "format": "binary"}}
    }
    with pytest.raises(ValueError, match="missing JSON response schema"):
        validator.validate_operation_contracts(broken, test_app.openapi())


def test_project_source_contract_requires_agent_authentication(
    published: dict[str, Any], test_app: FastAPI
) -> None:
    broken = copy.deepcopy(published)
    broken["paths"][PROJECT_SOURCE]["post"]["security"] = [{"bearerAuth": []}]
    with pytest.raises(ValueError, match="security declaration mismatch"):
        validator.validate_operation_contracts(broken, test_app.openapi())


def test_project_source_runtime_rejects_device_authority_and_invalid_agent_lease(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    registered = client.post(
        "/agents/register",
        headers=paired_headers,
        json={
            "name": "Native source contract probe",
            "endpoint": "http://127.0.0.1:9001",
            "skills": ["code.swift.build"],
        },
    )
    assert registered.status_code == 201
    agent = registered.json()
    route = PROJECT_SOURCE.format(agent_id=agent["id"], job_id="job_missing")
    proof = {
        "claim_token": "claim_contract_missing_lease",
        "lease_id": "lease_contract_missing",
        "lease_generation": 1,
    }
    assert client.post(route, json=proof).status_code == 401
    assert client.post(route, headers=paired_headers, json=proof).status_code == 401
    response = client.post(
        route,
        headers={"Authorization": f"Bearer {agent['credential']}"},
        json=proof,
    )
    assert response.status_code == 409
    assert "current authenticated lease" in response.json()["detail"]
