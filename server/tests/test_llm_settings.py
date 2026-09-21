from pathlib import Path

import pytest
from pydantic import ValidationError

from app.main import create_app
from app.services.swarm_contracts import ModelRole
from app.settings import Settings


@pytest.fixture(autouse=True)
def clear_model_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONGARS_PLANNER_REASONING_EFFORT", raising=False)
    for name in (
        "ORCHESTRATOR",
        "PLANNER",
        "EVALUATOR",
        "RESEARCH_EVALUATOR",
        "SUMMARIZER",
        "SYNTHESIZER",
    ):
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
    assert app.state.research_evaluator is None
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


@pytest.mark.parametrize("timeout,lease,expected", [(60, 120, 60), (120, 180, 120), (120, 60, 50)])
def test_goal_provider_deadlines_follow_operator_config_inside_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, timeout: int, lease: int, expected: int
) -> None:
    monkeypatch.setenv("MONGARS_GOAL_MODEL_TIMEOUT_SECONDS", str(timeout))
    monkeypatch.setenv("MONGARS_GOAL_MODEL_CALL_LEASE_SECONDS", str(lease))
    monkeypatch.setenv("MONGARS_RESEARCH_EVALUATOR_MODEL", "research-evaluator")
    app = create_app(
        Settings(
            _env_file=None, db_path=tmp_path / "state.db", workspace_root=tmp_path / "workspace"
        )
    )
    assert app.state.swarm_planner.timeout_seconds == expected
    assert app.state.evaluator.timeout_seconds == expected
    assert app.state.research_evaluator.timeout_seconds == expected
    assert expected < app.state.settings.goal_model_call_lease_seconds
    assert app.state.settings.goal_max_model_calls == 30
    assert app.state.settings.goal_max_runtime_seconds == 1800


@pytest.mark.parametrize("timeout", [0, 121, float("nan"), float("inf")])
def test_unbounded_goal_provider_timeout_is_rejected(timeout: float) -> None:
    with pytest.raises(ValidationError, match="goal_model_timeout_seconds"):
        Settings(_env_file=None, goal_model_timeout_seconds=timeout)


@pytest.mark.parametrize("effort", [None, "", " \t ", "none"])
def test_planner_reasoning_is_optional_and_preserves_model_and_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, effort: str | None
) -> None:
    if effort is not None:
        monkeypatch.setenv("MONGARS_PLANNER_REASONING_EFFORT", effort)
    app = create_app(
        Settings(
            _env_file=None,
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
            goal_model_timeout_seconds=120,
            goal_model_call_lease_seconds=60,
        )
    )
    assert app.state.swarm_planner.reasoning_effort == ("none" if effort == "none" else None)
    assert app.state.swarm_planner.model == "Hermes-3-Llama-3.2-3B-abliterated"
    assert app.state.swarm_planner.timeout_seconds == 50
    assert app.state.evaluator.reasoning_effort is None
    assert app.state.research_evaluator is None


@pytest.mark.parametrize("effort", ["high", "low", "false", "None", True, 0])
def test_unsupported_planner_reasoning_is_rejected_before_app_start(effort: object) -> None:
    with pytest.raises(ValidationError, match="planner_reasoning_effort"):
        Settings(_env_file=None, planner_reasoning_effort=effort)
