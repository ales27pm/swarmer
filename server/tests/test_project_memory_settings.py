from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.settings import Settings


@pytest.mark.parametrize(
    "missing",
    ["project_embedding_base_url", "project_embedding_model", "project_embedding_model_revision"],
)
def test_project_embeddings_require_complete_pinned_configuration(missing: str) -> None:
    configuration = {
        "project_embedding_base_url": "http://127.0.0.1:11434/v1",
        "project_embedding_model": "project-embedding:model-digest",
        "project_embedding_model_revision": "a" * 64,
    }
    configuration.pop(missing)
    with pytest.raises(ValidationError, match="pinned model SHA-256"):
        Settings(_env_file=None, **configuration)


def test_project_provider_does_not_enable_general_memory_embeddings() -> None:
    settings = Settings(
        _env_file=None,
        project_embedding_base_url="http://127.0.0.1:11434/v1",
        project_embedding_model="project-embedding:model-digest",
        project_embedding_model_revision="a" * 64,
    )
    assert settings.embedding_base_url is None
    assert settings.embedding_model is None
