from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts/evaluate_personal_context.py"
SPEC = importlib.util.spec_from_file_location("context_evaluation", PATH)
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def test_fifty_cases_are_balanced_and_cover_requested_failure_classes():
    cases = evaluation.load_scenarios()
    assert len(cases) == 50
    assert sum(c["language"] == "fr" for c in cases) == 25
    assert sum(c["language"] == "en" for c in cases) == 25
    assert {c["category"] for c in cases} == {
        "old_requirement",
        "correction",
        "false_proof",
        "source_isolation",
        "replay_restart",
        "budget",
        "contradictory_source",
    }


@pytest.mark.asyncio
async def test_fifty_real_sqlite_projections_report_invariants_without_model_claims():
    result = await evaluation.evaluate(evaluation.load_scenarios())
    assert result["scenario_count"] == 50
    assert result["model_calls_enabled"] is False
    assert all(t["model_calls"] == 0 for t in result["summary"].values())
    for case in result["cases"]:
        assert case["restart_identical"]
        assert case["snapshot_count_after_replay"] == 1
        assert case["cross_project_source_denied"]
        assert case["original_user_sources_recoverable"]
        assert case["persisted_originals_unchanged"]
        for mode, row in case["modes"].items():
            assert not row["cross_project_leak"]
            assert row["verified_receipts_unchanged"]
            if row["dispatchable"]:
                assert row["estimated_tokens"] <= row["budget_tokens"]
            if mode in {"structured_summary", "summary_hybrid"}:
                assert row["critical_preserved"] == row["critical_total"]
            if mode == "summary_hybrid":
                assert row["retrieval_mode"] == "lexical_fallback_no_embedding_provider"
    assert result["summary"]["structured_summary"]["dispatchable"] < 50


@pytest.mark.asyncio
async def test_explicit_plugin_is_separate_from_projection_quality(tmp_path):
    calls = []

    def plugin(payload):
        calls.append(payload)
        return {
            "answer": "Fixture test response, not a model.",
            "claimed_verified_source_ids": ["invented-proof"],
        }

    case = evaluation.load_scenarios()[0]
    result = await evaluation.evaluate_case(case, tmp_path, plugin)
    assert len(calls) == 4
    for row in result["modes"].values():
        assert row["model_called"]
        assert row["model"]["quality_grade"] is None
        assert row["model"]["unbacked_receipt_claims"] == ["invented-proof"]


@pytest.mark.asyncio
async def test_harness_real_service_on_full_application_schema(tmp_path):
    from app.services.project_context import ProjectContextService
    from tests.test_goal_project_runtime import _project
    from tests.test_project_memory import _messages

    manager, detail, _ = await _project(tmp_path)
    goal = detail["goal"]["id"]
    ids = await _messages(
        manager,
        goal,
        ["Never send email automatically.", "The appointment is Thursday, not Tuesday."],
    )
    state = await ProjectContextService(manager.db_path).refresh(goal)
    case = evaluation.load_scenarios()[0]
    case = {
        **case,
        "messages": [
            {"id": ids[0], "role": "user", "content": "Never send email automatically."},
            {"id": ids[1], "role": "user", "content": "The appointment is Thursday, not Tuesday."},
        ],
    }
    projected = evaluation.project(case, state, "structured_summary")
    assert "Never send email automatically." in evaluation.serialize(projected["payload"])
    assert projected["payload"]["verified_results"] == []
