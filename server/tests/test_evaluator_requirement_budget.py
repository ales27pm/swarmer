from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from app.services.context_builder import ContextBuilder, bound_evaluation_context
from app.services.swarm_contracts import GoalEvaluationContext
from tests.test_context_builder import _seed_goal
from tests.test_evaluator import evaluation_context


def _required_context() -> GoalEvaluationContext:
    raw = evaluation_context().model_dump(mode="json")
    raw["objective"] = (
        "Produce a deployment guide. "
        + "Explain the environment. " * 135
        + "Required: cover rollback verification."
    )
    raw["completion_criteria"] = [
        "Validate the deployment. " * 16 + "Never publish without explicit permission.",
        "Cite the actual check receipts and retain the source identifiers.",
    ]
    base = raw["node_results"][0]
    raw["node_results"] = [
        {
            **base,
            "node_id": f"node_{index}",
            "result_summary": "Observed deployment results. " * 130,
        }
        for index in range(20)
    ]
    raw["known_node_ids"] = [node["node_id"] for node in raw["node_results"]]
    return GoalEvaluationContext.model_validate(raw)


def test_default_budget_preserves_complete_requirements_before_result_details() -> None:
    raw = _required_context()

    bounded = bound_evaluation_context(raw, max_tokens=8_192)

    assert bounded.objective == raw.objective
    assert bounded.completion_criteria == raw.completion_criteria
    assert bounded.known_node_ids == raw.known_node_ids
    assert bounded.conversation_revision == raw.conversation_revision
    assert bounded.state_fingerprint == raw.state_fingerprint
    assert len(bounded.node_results) == len(raw.node_results)
    assert bounded.node_results[0].result_summary != raw.node_results[0].result_summary
    encoded = json.dumps(bounded.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= 8_192 * 4


def test_long_completion_criterion_is_whole_under_result_pressure() -> None:
    raw = _required_context()
    raw.objective = "Produce a deployment guide."

    bounded = bound_evaluation_context(raw, max_tokens=2_048)

    assert bounded.completion_criteria == raw.completion_criteria
    assert bounded.objective == raw.objective


@pytest.mark.parametrize("field", ["objective", "completion_criteria"])
def test_required_text_that_cannot_fit_fails_instead_of_returning_prefix(field: str) -> None:
    raw = evaluation_context()
    if field == "objective":
        raw.objective = _required_context().objective
    else:
        raw.completion_criteria = [
            "Required verification evidence. " * 12 + f"Final condition {index}."
            for index in range(8)
        ]

    with pytest.raises(ValueError, match="cannot fit the configured token budget"):
        bound_evaluation_context(raw, max_tokens=512)


def test_required_text_redacts_secrets_without_dropping_its_final_clause() -> None:
    raw = _required_context()
    raw.objective = "password=objective-secret; " + raw.objective
    raw.completion_criteria[0] = "token=criterion-secret; " + raw.completion_criteria[0]

    bounded = bound_evaluation_context(raw, max_tokens=8_192)

    assert "objective-secret" not in bounded.objective
    assert "criterion-secret" not in bounded.completion_criteria[0]
    assert bounded.objective.endswith("Required: cover rollback verification.")
    assert bounded.completion_criteria[0].endswith("Never publish without explicit permission.")


@pytest.mark.asyncio
async def test_oversized_requirements_fail_before_context_is_recorded(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, _ = await _seed_goal(db_path)
    builder = ContextBuilder(db_path, max_tokens=512)
    await builder.initialize()
    raw = _required_context()
    raw.goal_run_id = goal_id

    with pytest.raises(ValueError, match="cannot fit the configured token budget"):
        await builder.build_evaluation_context(raw)

    async with aiosqlite.connect(db_path) as db:
        count = await (await db.execute("SELECT COUNT(*) FROM goal_contexts")).fetchone()
    assert count is not None and count[0] == 0
