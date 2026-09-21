from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from app.main import create_app
from app.services.evaluator_provider import UbuntuEvaluatorProvider
from app.services.permission_policy import PermissionPolicy
from app.services.swarm_contracts import (
    EvaluationDecision,
    EvaluationNodeResult,
    GoalEvaluationContext,
    ModelRole,
)
from app.settings import Settings
from tests.test_evaluator import _response_for, continue_decision, evaluation_context
from tests.test_goal_context_payloads import _CapturingEvaluator
from tests.test_goal_research_sources import researched
from tests.test_goal_runtime_recovery import _manager, _worker_plan


def research_context(**changes: Any) -> GoalEvaluationContext:
    node = EvaluationNodeResult.model_validate(
        {
            "node_id": "node_research",
            "title": "Recherche",
            "status": "completed",
            "expected_output": "Des sources pertinentes.",
            "result_summary": "Bibliothèque : https://example.org/activites",
            "node_type": "worker",
            "required_skill": "research.query",
            **changes,
        }
    )
    return evaluation_context().model_copy(
        update={"node_results": [node], "known_node_ids": [node.node_id]}
    )


def canonical_nodes(context: GoalEvaluationContext) -> list[dict[str, Any]]:
    return [{"id": node.node_id, **node.model_dump(mode="json")} for node in context.node_results]


@pytest.mark.parametrize("override", [None, "", " \t ", "local/research:9b"])
def test_research_override_is_optional_and_does_not_change_existing_roles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: str | None
) -> None:
    monkeypatch.delenv("MONGARS_RESEARCH_EVALUATOR_MODEL", raising=False)
    if override is not None:
        monkeypatch.setenv("MONGARS_RESEARCH_EVALUATOR_MODEL", override)
    app = create_app(
        Settings(
            _env_file=None,
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
            orchestrator_model="base-abliterated",
            planner_model="planner-abliterated",
            evaluator_model="code-evaluator:30b",
            goal_model_timeout_seconds=120,
            goal_model_call_lease_seconds=180,
        )
    )
    assert app.state.evaluator.model == "code-evaluator:30b"
    assert app.state.evaluator.reasoning_effort is None
    assert app.state.swarm_planner.model == "planner-abliterated"
    assert app.state.model_router.route_for(ModelRole.EVALUATOR).model_id == "code-evaluator:30b"
    assert app.state.goal_manager.evaluator is app.state.evaluator
    research = app.state.research_evaluator
    assert app.state.goal_manager.research_evaluator is research
    if override and override.strip():
        assert research.model == override
        assert research.reasoning_effort == "none"
        assert research.timeout_seconds == app.state.evaluator.timeout_seconds == 120
        assert research.base_url == app.state.evaluator.base_url
        assert research.policy is app.state.evaluator.policy
    else:
        assert research is None
        assert app.state.settings.research_evaluator_model is None


@pytest.mark.parametrize("model", ["model\ninvalid", "model?token=secret"])
def test_research_model_uses_existing_identifier_validation(model: str) -> None:
    with pytest.raises(ValueError, match="research_evaluator_model"):
        Settings(_env_file=None, research_evaluator_model=model)


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "none"])
async def test_reasoning_effort_is_sent_only_when_explicitly_configured(
    effort: Literal["none"] | None,
) -> None:
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:8711/v1",
        model="local-research",
        policy=PermissionPolicy.from_yaml(
            Path(__file__).resolve().parents[2] / "configs/permissions.yaml"
        ),
        reasoning_effort=effort,
    )
    post = AsyncMock(return_value=_response_for(json.dumps(continue_decision())))
    with patch("httpx.AsyncClient.post", post):
        await provider.evaluate(evaluation_context())
    payload = post.await_args.kwargs["json"]
    if effort is None:
        assert "reasoning_effort" not in payload
    else:
        assert payload["reasoning_effort"] == "none"
    assert payload["model"] == "local-research"
    assert payload["stream"] is False
    assert payload["response_format"]["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_completed_research_uses_override_only_when_configured(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    context = research_context()
    nodes = canonical_nodes(context)
    assert manager._evaluator_for_context(context, nodes) is manager.evaluator
    research = _CapturingEvaluator()
    manager.research_evaluator = research
    assert manager._evaluator_for_context(context, nodes) is research


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"status": "failed"},
        {"status": "skipped"},
        {"status": "running"},
        {"result_summary": None},
        {"required_skill": None},
        {"required_skill": "writing.draft"},
        {"node_type": None},
        {"node_type": "synthesis"},
    ],
)
async def test_incomplete_empty_or_legacy_research_evidence_keeps_base_evaluator(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    manager.research_evaluator = _CapturingEvaluator()
    context = research_context(**changes)
    assert manager._evaluator_for_context(context, canonical_nodes(context)) is manager.evaluator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill",
    [
        "code.build_project",
        "code.generate_python",
        "code_review.git_status",
        "workspace.read_text",
        None,
    ],
)
async def test_canonical_mixed_or_legacy_nodes_block_override_even_if_context_omits_them(
    tmp_path: Path, skill: str | None
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    manager.research_evaluator = _CapturingEvaluator()
    context = research_context()
    nodes = canonical_nodes(context) + [
        {"id": "node_omitted", "node_type": "worker", "required_skill": skill}
    ]
    assert manager._evaluator_for_context(context, nodes) is manager.evaluator


@pytest.mark.asyncio
async def test_context_must_retain_matching_canonical_research_evidence(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    manager.research_evaluator = _CapturingEvaluator()
    context = research_context()
    nodes = canonical_nodes(context)
    omitted = context.model_copy(update={"node_results": []})
    assert manager._evaluator_for_context(omitted, nodes) is manager.evaluator
    for change in (
        {"id": "other_node"},
        {"status": "failed"},
        {"result_summary": None},
        {"result_summary": " \t "},
    ):
        assert (
            manager._evaluator_for_context(context, [{**nodes[0], **change}]) is manager.evaluator
        )
    assert manager._evaluator_for_context(context, []) is manager.evaluator


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_canonical_research_writing_lifecycle_reserves_actual_model_before_call(
    tmp_path: Path, enabled: bool
) -> None:
    manager, goal, _, _, writer = await researched(tmp_path)

    class ReservedEvaluator(_CapturingEvaluator):
        def __init__(self, model: str) -> None:
            super().__init__()
            self.model = model

        async def evaluate(self, context: GoalEvaluationContext) -> EvaluationDecision:
            async with aiosqlite.connect(manager.db_path) as db:
                rows = await (
                    await db.execute(
                        "SELECT model_id,provider_source,owner_instance_id FROM goal_model_calls "
                        "WHERE goal_run_id=? AND role='evaluator' AND status='started'",
                        (goal["id"],),
                    )
                ).fetchall()
            assert rows == [(self.model, self.source.value, manager.instance_id)]
            return await super().evaluate(context)

    base = ReservedEvaluator("base-evaluator")
    research = ReservedEvaluator("qualified-research-evaluator")
    manager.evaluator = base
    if enabled:
        manager.research_evaluator = research
    result, changed = await manager.agent_dispatcher.submit_result(
        writer["claimed_by"],
        writer["id"],
        writer["claim_token"],
        status="completed",
        result={
            "schema_version": "1.0",
            "content_trust": "untrusted",
            "text": "La bibliothèque propose un atelier familial samedi : https://example.org/activites",
            "summary": "Comparaison à partir de la source disponible.",
        },
        error=None,
        lease_id=writer["lease_id"],
        lease_generation=writer["lease_generation"],
    )
    assert changed
    await manager.on_job_result(result)
    chosen, other = (research, base) if enabled else (base, research)
    assert len(chosen.contexts) == 1 and not other.contexts
    async with aiosqlite.connect(manager.db_path) as db:
        calls = await (
            await db.execute(
                "SELECT model_id,status FROM goal_model_calls WHERE goal_run_id=? AND role='evaluator'",
                (goal["id"],),
            )
        ).fetchall()
        assert calls == [(chosen.model, "completed")]
        count = await (
            await db.execute("SELECT model_call_count FROM goal_runs WHERE id=?", (goal["id"],))
        ).fetchone()
        assert count == (3,)
