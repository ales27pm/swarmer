from pathlib import Path

import pytest
from pydantic import ValidationError

from app.main import create_app
from app.services.swarm_contracts import ModelRole
from app.settings import Settings


@pytest.fixture(autouse=True)
def clear_model_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ORCHESTRATOR", "PLANNER", "EVALUATOR", "SUMMARIZER", "SYNTHESIZER"):
        monkeypatch.delenv(f"MONGARS_{name}_MODEL", raising=False)


def test_swarm_defaults_bind_abliterated_model_to_inference_providers(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
        )
    )

    expected = "Hermes-3-Llama-3.2-3B-abliterated"
    assert app.state.orchestrator_service.model == expected
    assert app.state.swarm_planner.model == expected
    assert app.state.evaluator.model == expected
    for role in ModelRole:
        assert app.state.model_router.route_for(role).model_id == expected


def test_role_overrides_preserve_orchestrator_inheritance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MONGARS_ORCHESTRATOR_MODEL", "  local/custom-abliterated:Q4_K_M  ")
    monkeypatch.setenv("MONGARS_PLANNER_MODEL", "  operator/planner:latest  ")
    monkeypatch.setenv("MONGARS_EVALUATOR_MODEL", "operator/evaluator:Q5_K_M")
    monkeypatch.setenv("MONGARS_SUMMARIZER_MODEL", " \t ")
    monkeypatch.setenv("MONGARS_SYNTHESIZER_MODEL", "")
    app = create_app(
        Settings(
            _env_file=None,
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
        )
    )

    assert app.state.orchestrator_service.model == "local/custom-abliterated:Q4_K_M"
    assert app.state.swarm_planner.model == "operator/planner:latest"
    assert app.state.evaluator.model == "operator/evaluator:Q5_K_M"
    for role in (ModelRole.SUMMARIZER, ModelRole.SYNTHESIZER):
        assert app.state.model_router.route_for(role).model_id == "local/custom-abliterated:Q4_K_M"
    assert app.state.settings.summarizer_model is None
    assert app.state.settings.synthesizer_model is None


@pytest.mark.parametrize("model", ["", "   ", "model\ninvalid", "model?token=secret"])
def test_invalid_orchestrator_model_is_rejected_at_settings_boundary(model: str) -> None:
    with pytest.raises(ValidationError, match="orchestrator_model"):
        Settings(_env_file=None, orchestrator_model=model)
