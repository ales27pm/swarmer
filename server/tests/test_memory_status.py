"""Read-only provider configuration status on the isolated API release."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.services.embedding_service import HttpEmbeddingService
from app.settings import Settings
from tests.conftest import OPERATOR_TOKEN, REPO_ROOT


@pytest.fixture(params=[False, True], ids=["lexical", "configured"])
def test_app(tmp_path: Path, request: pytest.FixtureRequest) -> FastAPI:
    return create_app(
        Settings(
            _env_file=None,
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
            permissions_path=REPO_ROOT / "configs/permissions.yaml",
            pairing_bootstrap_token=SecretStr(OPERATOR_TOKEN),
            project_embedding_base_url="http://127.0.0.1:1/v1" if request.param else None,
            project_embedding_model="fixture-embedding" if request.param else None,
            project_embedding_model_revision="a" * 64 if request.param else None,
        )
    )


def test_memory_status_requires_paired_device(client: TestClient) -> None:
    assert client.get("/memory/status").status_code == 401


def test_memory_status_reports_configuration_without_probing_or_enabling_features(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_inference(self: HttpEmbeddingService, texts: list[str]) -> list[list[float]]:
        pytest.fail("status must not call embeddings")

    monkeypatch.setattr(HttpEmbeddingService, "embed", no_inference)
    response = client.get("/memory/status", headers=paired_headers)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    configured = bool(test_app.state.settings.project_embedding_model)
    assert body["embedding_configured"] is configured
    assert body["embedding_model"] == test_app.state.settings.project_embedding_model
    assert body["embedding_revision"] == test_app.state.settings.project_embedding_model_revision
    assert body["provider_fingerprint"] == test_app.state.project_memory.provider_identity
    assert body["readiness"] == ("configured_not_probed" if configured else "lexical_fallback")
    assert body["context_enabled"] is False
    assert body["compaction_enabled"] is False
    assert body["hybrid_enabled"] is False
    assert "context_token_count_method" not in body
    assert not hasattr(test_app.state, "project_context")
    assert not hasattr(test_app.state, "project_compaction")
