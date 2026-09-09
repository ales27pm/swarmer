from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from app.services.feedback_dataset import FeedbackDatasetService, sanitize_dataset_value
from app.services.state_service import StateService

CREATED_AT = "2026-09-08T14:00:00+00:00"


async def _seed_goal_feedback(
    db_path: Path,
    suffix: str,
    *,
    score: float,
    reviewed: bool,
    status: str,
    planner_source: str = "ubuntu_local",
) -> None:
    task_id = f"tsk_{suffix}"
    goal_id = f"goal_{suffix}"
    node_id = f"node_{suffix}"
    job_id = f"job_{suffix}"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO tasks(
                id,title,input,mode,source,status,priority,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                task_id,
                "Goal",
                "Inspect password=never-export from /home/user/private/repo",
                "normal",
                "test",
                "completed" if status == "completed" else "failed",
                0,
                CREATED_AT,
                CREATED_AT,
            ),
        )
        await db.execute(
            """
            INSERT INTO goal_runs(
                id,root_task_id,objective,status,autonomy_profile,planner_source,
                max_steps,max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
                step_count,replan_count,model_call_count,completion_criteria_json,
                current_phase,plan_fingerprint,evaluation_fingerprint,
                created_at,updated_at,completed_at,failure_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                goal_id,
                task_id,
                "Inspect token=goal-secret at /Users/alice/project",
                status,
                "assisted",
                planner_source,
                20,
                3,
                3,
                1_800,
                30,
                1,
                0,
                3,
                '["Return safe evidence"]',
                "completed" if status == "completed" else "failed",
                "a" * 64,
                "b" * 64,
                CREATED_AT,
                CREATED_AT,
                CREATED_AT,
                None if status == "completed" else "token=failure-secret",
            ),
        )
        await db.execute(
            """
            INSERT INTO plan_nodes(
                id,goal_run_id,node_type,title,objective,required_skill,status,
                priority,depends_on_json,assigned_agent_id,worker_job_id,expected_output,
                result_summary,error_summary,planner_metadata_json,
                created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                node_id,
                goal_id,
                "worker",
                "Inspect",
                "Read safe files",
                "workspace.read_text",
                "completed" if status == "completed" else "failed",
                50,
                "[]",
                f"agent_{suffix}",
                job_id,
                "A bounded summary",
                "Found README.md and token=node-secret",
                None if status == "completed" else "password=node-failure",
                "{}",
                CREATED_AT,
                CREATED_AT,
                CREATED_AT,
            ),
        )
        await db.execute(
            """
            INSERT INTO goal_results(goal_run_id,result_json,created_at,updated_at)
            VALUES(?,?,?,?)
            """,
            (
                goal_id,
                json.dumps(
                    {
                        "summary": "Safe final summary",
                        "authorization": "Bearer goal-result-secret",
                    }
                ),
                CREATED_AT,
                CREATED_AT,
            ),
        )
        await db.execute(
            """
            INSERT INTO goal_evaluations(
                id,goal_run_id,sequence,status,reason_summary,decision_json,
                state_fingerprint,decision_fingerprint,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                f"evaluation_{suffix}",
                goal_id,
                1,
                "done" if status == "completed" else "failed",
                "Evidence is sufficient; api_key=evaluator-secret",
                json.dumps(
                    {
                        "status": "done" if status == "completed" else "failed",
                        "reason_summary": "Safe reason",
                        "nested": {"privateKey": "evaluator-private-secret"},
                    }
                ),
                "c" * 64,
                "d" * 64,
                CREATED_AT,
            ),
        )
        for index, role in enumerate(("planner", "evaluator", "synthesizer"), start=1):
            await db.execute(
                """
                INSERT INTO goal_model_calls(
                    id,goal_run_id,node_id,role,provider_source,model_id,
                    input_digest,output_digest,status,latency_ms,created_at,completed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"call_{suffix}_{index}",
                    goal_id,
                    node_id,
                    role,
                    planner_source,
                    "local-model",
                    "e" * 64,
                    "f" * 64,
                    "completed",
                    12 + index,
                    CREATED_AT,
                    CREATED_AT,
                ),
            )
        await db.execute(
            """
            INSERT INTO scheduler_decisions(
                id,job_id,candidates_json,selected_agent_id,scoring_json,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                f"schedule_{suffix}",
                job_id,
                json.dumps(
                    [
                        {
                            "agent_id": f"agent_{suffix}",
                            "token": "candidate-secret",
                        }
                    ]
                ),
                f"agent_{suffix}",
                json.dumps({"load": 0.1, "password": "scheduler-secret"}),
                CREATED_AT,
            ),
        )
        await db.execute(
            """
            INSERT INTO goal_feedback(
                id,goal_run_id,score,note,corrected_final_answer,
                corrected_plan_summary,reviewed,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                f"feedback_{suffix}",
                goal_id,
                score,
                "Reviewer note token=feedback-secret",
                "Correct final answer without password=answer-secret",
                "Correct plan without api_key=plan-secret",
                int(reviewed),
                CREATED_AT,
            ),
        )
        await db.commit()


def _rows(value: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in value.splitlines() if line]


@pytest.mark.asyncio
async def test_goal_exports_cover_each_trajectory_without_raw_secrets(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    await StateService(db_path).initialize()
    await _seed_goal_feedback(
        db_path,
        "qualified",
        score=5.0,
        reviewed=True,
        status="completed",
    )
    service = FeedbackDatasetService(db_path)

    expected_trajectory_keys = {
        "planner": {"completion_criteria", "nodes", "model_calls"},
        "evaluator": {"evaluations", "node_outcomes", "model_calls"},
        "synthesis": {"node_outcomes", "goal_result", "model_calls"},
        "routing": {"assignments", "scheduler_decisions"},
    }
    all_exports = ""
    for dataset_type, expected_keys in expected_trajectory_keys.items():
        exported = await service.export_goal_jsonl(dataset_type)  # type: ignore[arg-type]
        all_exports += exported
        rows = _rows(exported)
        assert len(rows) == 1
        row = rows[0]
        assert row["dataset_type"] == dataset_type
        assert set(row["trajectory"]) == expected_keys  # type: ignore[arg-type]
        if dataset_type == "routing":
            assert row["fine_tune_candidate"] is False
            assert "training_target" not in row
        else:
            assert row["fine_tune_candidate"] is True
            assert row["training_target"]

    lowered = all_exports.casefold()
    for secret in (
        "goal-secret",
        "node-secret",
        "failure-secret",
        "evaluator-secret",
        "evaluator-private-secret",
        "goal-result-secret",
        "candidate-secret",
        "scheduler-secret",
        "feedback-secret",
        "answer-secret",
        "plan-secret",
    ):
        assert secret not in lowered
    assert "/home/user" not in all_exports
    assert "/Users/alice" not in all_exports


@pytest.mark.asyncio
async def test_goal_dataset_filters_and_candidate_gate_are_independent(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    await StateService(db_path).initialize()
    await _seed_goal_feedback(db_path, "good", score=5, reviewed=True, status="completed")
    await _seed_goal_feedback(db_path, "unreviewed", score=5, reviewed=False, status="completed")
    await _seed_goal_feedback(db_path, "failed", score=5, reviewed=True, status="failed")
    await _seed_goal_feedback(db_path, "low", score=2, reviewed=True, status="completed")
    await _seed_goal_feedback(
        db_path,
        "manual",
        score=5,
        reviewed=True,
        status="completed",
        planner_source="manual",
    )
    service = FeedbackDatasetService(db_path)

    ubuntu_default = _rows(
        await service.export_goal_jsonl("planner", planner_source="ubuntu_local")
    )
    manual_default = _rows(await service.export_goal_jsonl("planner", planner_source="manual"))
    assert [row["goal_run_id"] for row in ubuntu_default] == ["goal_good"]
    assert [row["goal_run_id"] for row in manual_default] == ["goal_manual"]

    inclusive = _rows(
        await service.export_goal_jsonl(
            "planner",
            minimum_score=0,
            successful_only=False,
            reviewed_only=False,
        )
    )
    assert len(inclusive) == 5
    candidates = {str(row["goal_run_id"]): bool(row["fine_tune_candidate"]) for row in inclusive}
    assert candidates == {
        "goal_good": True,
        "goal_unreviewed": False,
        "goal_failed": False,
        "goal_low": False,
        "goal_manual": True,
    }
    for row in inclusive:
        assert ("training_target" in row) is bool(row["fine_tune_candidate"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"minimum_score": -1},
        {"minimum_score": float("nan")},
        {"successful_only": 1},
        {"reviewed_only": "true"},
        {"planner_source": "remote_untrusted"},
    ],
)
async def test_goal_dataset_rejects_invalid_filters(
    tmp_path: Path, kwargs: dict[str, object]
) -> None:
    db_path = tmp_path / "state.db"
    await StateService(db_path).initialize()
    service = FeedbackDatasetService(db_path)
    with pytest.raises(ValueError):
        await service.export_goal_jsonl("planner", **kwargs)  # type: ignore[arg-type]


def test_dataset_sanitizer_removes_nested_sensitive_keys_and_paths() -> None:
    value = {
        "safe": "keep",
        "apiKey": "remove",
        "nested": [
            {"private-key": "remove-too"},
            "password=embedded-secret /root/private/file",
        ],
        "tоken": "unicode-confusable-key",
    }
    sanitized = sanitize_dataset_value(value)
    encoded = json.dumps(sanitized, sort_keys=True)
    assert '"safe": "keep"' in encoded
    assert "remove" not in encoded
    assert "embedded-secret" not in encoded
    assert "unicode-confusable-key" not in encoded
    assert "/root/private" not in encoded
