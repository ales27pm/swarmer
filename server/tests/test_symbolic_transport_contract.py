"""Public schemas must describe the exact registered worker cohort."""

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.services.agent_card import validate_agent_card_manifest

ROOT = Path(__file__).resolve().parents[2]
CARDS = sorted((ROOT / "workers").glob("*-worker/agent-card*.json"))


@pytest.mark.parametrize("path", CARDS, ids=lambda path: str(path.relative_to(ROOT)))
def test_registered_worker_matches_public_json_and_openapi_contracts(
    path: Path, client: TestClient, paired_headers: dict[str, str]
) -> None:
    manifest = json.loads(path.read_text())
    policy = validate_agent_card_manifest(manifest)
    created = client.post(
        "/agents/register",
        headers=paired_headers,
        json={
            "name": manifest["name"],
            "version": manifest["version"],
            "endpoint": "http://127.0.0.1:9200",
            "skills": list(policy.skills),
            "capacity": dict(policy.capability_metadata),
        },
    )
    assert created.status_code == 201, created.text
    resource = client.get(f"/agents/{created.json()['id']}", headers=paired_headers)
    assert resource.status_code == 200, resource.text
    data = resource.json()
    assert data["capacity"]["symbolic_context_version"] == 1
    assert data["agent_card"]["capabilities"]["symbolic_context_version"] == 1
    assert "credential" not in data and "auth_token_hash" not in data
    Draft202012Validator(
        json.loads((ROOT / "schemas/agent-card.schema.json").read_text())
    ).validate(data)
    spec = yaml.safe_load((ROOT / "api/openapi.yaml").read_text())
    Draft202012Validator({**spec, "$ref": "#/components/schemas/Agent"}).validate(data)


@pytest.mark.parametrize("value", [True, False, "1", 0, 2, None])
def test_published_capability_refuses_invalid_version(value: object) -> None:
    schema = json.loads((ROOT / "schemas/agent-card.schema.json").read_text())
    invalid = {"symbolic_context_version": value}
    assert not Draft202012Validator(schema["$defs"]["capabilities"]).is_valid(invalid)


@pytest.mark.parametrize(
    "protocols,valid",
    [
        ([], True),
        (["symbolic-v1"], True),
        (["symbolic-v2"], False),
        (["symbolic-v1", "symbolic-v1"], False),
        (None, False),
    ],
)
def test_published_claim_protocol_contract(protocols: object, valid: bool) -> None:
    spec = yaml.safe_load((ROOT / "api/openapi.yaml").read_text())
    schema = spec["components"]["schemas"]["AgentJobClaim"]
    assert Draft202012Validator(schema).is_valid({"context_protocols": protocols}) is valid
