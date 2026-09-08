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


def test_remote_redis_requires_tls() -> None:
    with pytest.raises(ValidationError, match="TLS is required"):
        Settings(
            message_board_backend="redis",
            redis_url=SecretStr("redis://cache.example.invalid:6379/0"),
        )

    settings = Settings(
        message_board_backend="redis",
        redis_url=SecretStr("rediss://cache.example.invalid:6380/0"),
    )
    assert settings.redis_url.get_secret_value().startswith("rediss://")


def test_agent_offline_timeout_must_exceed_heartbeat_interval() -> None:
    with pytest.raises(ValidationError, match="shorter than the offline timeout"):
        Settings(agent_heartbeat_seconds=20, agent_offline_timeout_seconds=20)

    with pytest.raises(ValidationError, match="Redis URL is invalid"):
        Settings(
            message_board_backend="redis",
            redis_url=SecretStr("rediss://cache.example.invalid:6380/0?ssl_cert_reqs=none"),
        )
