from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.services.memory_normalization import MemoryNormalizationError
from app.settings import Settings


def _configuration() -> dict[str, str]:
    return {
        "memory_canonical_language": "en",
        "memory_normalization_base_url": "http://127.0.0.1:11434/v1",
        "memory_translator_model": "explicit-translator",
        "memory_reviewer_model": "explicit-reviewer",
    }


@pytest.mark.parametrize(
    "missing",
    ["memory_normalization_base_url", "memory_translator_model", "memory_reviewer_model"],
)
def test_english_policy_requires_explicit_provider_roles(missing: str) -> None:
    config = _configuration()
    config.pop(missing)
    with pytest.raises(ValidationError, match="explicit local URL"):
        Settings(_env_file=None, **config)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.org/v1",
        "http://user:password@localhost/v1",
        "http://localhost/v1?token=private",
        "http://localhost/v1#fragment",
        "file:///tmp/provider",
        "http://localhost:0/v1",
        "http://localhost:invalid/v1",
        "http://localhost/v1\n",
    ],
)
def test_memory_provider_stays_on_explicit_credential_free_local_endpoint(endpoint: str) -> None:
    config = {**_configuration(), "memory_normalization_base_url": endpoint}
    with pytest.raises(ValidationError, match="loopback URL"):
        Settings(_env_file=None, **config)


@pytest.mark.parametrize("timeout", [0, 61, float("inf"), float("nan")])
def test_memory_request_timeout_is_bounded(timeout: float) -> None:
    with pytest.raises(ValidationError, match="memory_normalization_timeout_seconds"):
        Settings(_env_file=None, **_configuration(), memory_normalization_timeout_seconds=timeout)


def test_configured_english_policy_does_not_inherit_orchestrator(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            **_configuration(),
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
            memory_normalization_timeout_seconds=37,
        )
    )
    state = app.state.state_service
    assert state.canonical_language == "en"
    assert state.memory_normalizer.translator_model == "explicit-translator"
    assert state.memory_normalizer.reviewer_model == "explicit-reviewer"
    assert state.memory_normalizer.timeout_seconds == 37
    assert state.memory_presenter.translator_model == "explicit-translator"
    assert state.memory_presenter.reviewer_model == "explicit-reviewer"
    assert state.memory_presenter.timeout_seconds == 37


@pytest.mark.parametrize(
    ("category", "status"),
    [("unavailable", 503), ("invalid", 422), ("uncertain", 422), ("source_conflict", 409)],
)
@pytest.mark.parametrize("method", ["post", "patch", "search"])
def test_normalization_errors_are_truthful_and_do_not_echo_source(
    client: TestClient,
    paired_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    status: int,
    method: str,
) -> None:
    async def fail(*args: object, **kwargs: object) -> None:
        raise MemoryNormalizationError(category, "private-source-must-not-appear")

    method_name, endpoint = {
        "post": ("create_memory", "/memory"),
        "patch": ("update_memory", "/memory/mem_fixture"),
        "search": ("search_memory", "/memory/search"),
    }[method]
    monkeypatch.setattr(client.app.state.state_service, method_name, fail)
    response = client.request(
        "post" if method == "search" else method,
        endpoint,
        headers=paired_headers,
        json={"query" if method == "search" else "content": "Ne pas envoyer automatiquement."},
    )
    assert response.status_code == status
    assert response.headers["X-Mongars-Memory-Normalization"] == category
    assert isinstance(response.json()["detail"], str)
    assert "private-source" not in response.text
    assert "Ne pas envoyer" not in response.text
    assert client.get("/memory", headers=paired_headers).json() == []
