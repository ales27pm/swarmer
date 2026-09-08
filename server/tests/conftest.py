from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATOR_TOKEN = "test-operator-token-with-sufficient-entropy"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires explicitly configured external infrastructure",
    )


@pytest.fixture
def test_app(tmp_path: Path) -> FastAPI:
    workspace = tmp_path / "workspace"
    return create_app(
        Settings(
            db_path=tmp_path / "state.db",
            workspace_root=workspace,
            permissions_path=REPO_ROOT / "configs" / "permissions.yaml",
            pairing_bootstrap_token=SecretStr(OPERATOR_TOKEN),
            pairing_code_ttl_seconds=600,
            pairing_max_attempts=3,
        )
    )


@pytest.fixture
def client(test_app: FastAPI):
    with TestClient(test_app, client=("127.0.0.1", 50_000)) as value:
        yield value


@pytest.fixture
def paired_headers(client: TestClient) -> dict[str, str]:
    code_response = client.post(
        "/pairing/code", headers={"X-Mongars-Operator-Token": OPERATOR_TOKEN}
    )
    assert code_response.status_code == 200
    candidate_response = client.post(
        "/pairing/complete",
        json={"code": code_response.json()["code"], "device_id": "test-phone", "name": "pytest"},
    )
    assert candidate_response.status_code == 200
    candidate = candidate_response.json()
    headers = {"Authorization": f"Bearer {candidate['candidate_token']}"}
    finalize_response = client.post(
        "/pairing/finalize",
        headers=headers,
        json={"pairing_id": candidate["pairing_id"], "device_id": candidate["device_id"]},
    )
    assert finalize_response.status_code == 200
    assert client.get("/sync/bootstrap", headers=headers).status_code == 200
    return headers
