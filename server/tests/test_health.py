from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.main import create_app
from app.settings import Settings


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_control_plane_state_must_be_outside_tool_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    with pytest.raises(RuntimeError, match="db_path must be outside workspace_root"):
        create_app(Settings(db_path=workspace / "state.db", workspace_root=workspace))


def test_weak_pairing_bootstrap_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="high-entropy secret"):
        Settings(pairing_bootstrap_token=SecretStr("change-me"))
