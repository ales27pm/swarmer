from pathlib import Path

import pytest
from pydantic import ValidationError

from app.main import create_app
from app.settings import Settings


@pytest.mark.parametrize("configured,expected", [(None, 24_000), ("64000", 64_000)])
def test_project_budget_reaches_admission_without_changing_other_model_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured: str | None, expected: int
) -> None:
    monkeypatch.delenv("MONGARS_PROJECT_CONTEXT_BUDGET_TOKENS", raising=False)
    if configured is not None:
        monkeypatch.setenv("MONGARS_PROJECT_CONTEXT_BUDGET_TOKENS", configured)
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "state.db",
        workspace_root=tmp_path / "workspace",
    )
    app = create_app(settings)
    service = app.state.project_compaction

    assert settings.project_context_budget_tokens == expected
    assert service.context_tokens == expected
    assert service.output_tokens == 2_000
    assert service.overhead_tokens == 8_000
    budget = service._budget({})
    assert budget["available_input_tokens"] == expected - 10_000
    assert budget["counter"] == "conservative_utf8_bytes"
    assert settings.goal_context_max_tokens == 8_192
    assert settings.project_context_enabled is False
    assert settings.project_compaction_enabled is False


@pytest.mark.parametrize("value", ["-1", "0", "10000", "262145", "nan", "1.5"])
def test_project_budget_rejects_invalid_environment_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MONGARS_PROJECT_CONTEXT_BUDGET_TOKENS", value)
    with pytest.raises(ValidationError, match="project_context_budget_tokens"):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", [10_001, 262_144])
def test_project_budget_accepts_documented_bounds(value: int) -> None:
    assert (
        Settings(_env_file=None, project_context_budget_tokens=value).project_context_budget_tokens
        == value
    )
