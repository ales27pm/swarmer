from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from app.services.result_aggregator import (
    ResultAggregator,
    summarize_untrusted_worker_output,
    validate_worker_evidence,
)
from app.services.state_service import StateService

CREATED_AT = "2026-09-08T12:00:00+00:00"


@pytest.mark.parametrize(
    ("skill", "result", "valid"),
    [
        ("workspace.list_dir", None, False),
        ("workspace.list_dir", {}, False),
        ("workspace.list_dir", {"entries": []}, True),
        ("workspace.read_text", {"content": ""}, True),
        ("research.query", {"results": []}, False),
        (
            "research.query",
            {"content_trust": "untrusted", "results": []},
            True,
        ),
        (
            "code_review.git_status",
            {
                "content_trust": "untrusted",
                "entries": [],
                "protected_entries_omitted": 0,
                "truncated": False,
            },
            True,
        ),
        (
            "code_review.git_diff",
            {
                "content_trust": "untrusted",
                "output": "",
                "exit_code": 0,
                "protected_entries_omitted": 0,
                "truncated": False,
            },
            True,
        ),
        ("unknown.skill", {"evidence": "claimed"}, False),
    ],
)
def test_worker_evidence_requires_each_bounded_skill_contract(
    skill: str,
    result: object,
    valid: bool,
) -> None:
    assert validate_worker_evidence(skill, result) is valid


async def _seed_goal(db_path: Path) -> None:
    await StateService(db_path).initialize()
    async with aiosqlite.connect(db_path) as db:
        for task_id, status in (("tsk_root", "running"), ("tsk_child", "completed")):
            await db.execute(
                """
                INSERT INTO tasks(
                    id,title,input,mode,source,status,priority,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    "Goal task",
                    "Inspect the repository",
                    "normal",
                    "test",
                    status,
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
                current_phase,plan_fingerprint,evaluation_fingerprint,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "goal_result",
                "tsk_root",
                "Inspect the repository",
                "running",
                "assisted",
                "test",
                20,
                3,
                3,
                1_800,
                30,
                1,
                0,
                1,
                '["Produce a grounded summary"]',
                "aggregation",
                "a" * 64,
                "b" * 64,
                CREATED_AT,
                CREATED_AT,
            ),
        )
        raw_worker_result = {
            "entries": [
                "README.md",
                "/home/user/project/.env",
                {"password": "do-not-export", "safe": "server"},
            ],
            "authorization": "Bearer secret-secret-secret",
            "summary": "Found token=abc12345 under /Users/alice/private/file.txt",
        }
        await db.execute(
            """
            INSERT INTO agent_jobs(
                id,task_id,required_skill,payload_json,status,result_json,
                created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "job_worker",
                "tsk_child",
                "workspace.list_dir",
                '{"path":"."}',
                "completed",
                json.dumps(raw_worker_result),
                CREATED_AT,
                CREATED_AT,
                CREATED_AT,
            ),
        )
        await db.execute(
            """
            INSERT INTO plan_nodes(
                id,goal_run_id,task_id,node_type,title,objective,required_skill,status,
                priority,depends_on_json,assigned_agent_id,worker_job_id,expected_output,
                planner_metadata_json,created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "node_worker",
                "goal_result",
                "tsk_child",
                "worker",
                "Inspect files",
                "Inspect files",
                "workspace.list_dir",
                "completed",
                100,
                "[]",
                "agent_safe",
                "job_worker",
                "A safe file inventory",
                "{}",
                CREATED_AT,
                CREATED_AT,
                CREATED_AT,
            ),
        )
        await db.execute(
            """
            INSERT INTO plan_nodes(
                id,goal_run_id,node_type,title,objective,required_skill,status,
                priority,depends_on_json,expected_output,result_summary,
                planner_metadata_json,created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "node_synthesis",
                "goal_result",
                "synthesis",
                "Synthesize",
                "Summarize verified evidence",
                None,
                "completed",
                1,
                '["node_worker"]',
                "A final answer",
                "Repository evidence was summarized without raw worker payloads.",
                "{}",
                CREATED_AT,
                CREATED_AT,
                CREATED_AT,
            ),
        )
        await db.execute(
            """INSERT INTO goal_contexts(
                id,goal_run_id,root_task_id,node_id,purpose,context_json,
                provenance_json,approx_token_count,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "ctx_result",
                "goal_result",
                "tsk_root",
                None,
                "planner",
                '{"cards":[]}',
                '{"source_ids":["tsk_root","mem_safe","ep_safe"],"card_ids":[]}',
                0,
                CREATED_AT,
            ),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_aggregate_goal_persists_only_bounded_safe_summaries_and_provenance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    await _seed_goal(db_path)
    aggregator = ResultAggregator(db_path)

    result = await aggregator.aggregate_goal("goal_result")
    encoded = json.dumps(result, sort_keys=True)

    assert result["schema_version"] == "1.0"
    assert result["provenance"] == {
        "source": "sqlite_authoritative_summaries",
        "root_task_id": "tsk_root",
        "plan_fingerprint": "a" * 64,
        "evaluation_fingerprint": "b" * 64,
        "node_ids": ["node_worker", "node_synthesis"],
    }
    assert result["counts"] == {
        "total": 2,
        "completed": 2,
        "failed": 0,
        "blocked": 0,
        "cancelled": 0,
        "skipped": 0,
    }
    assert result["memory_ids"] == ["mem_safe"]
    assert result["episode_ids"] == ["ep_safe"]
    assert "do-not-export" not in encoded
    assert "secret-secret-secret" not in encoded
    assert "abc12345" not in encoded
    assert "/home/user" not in encoded
    assert "/Users/alice" not in encoded
    assert "worker_result_json" not in encoded
    assert "authorization" not in encoded.casefold()
    worker = result["nodes"][0]
    assert worker["provenance"]["result_digest"]
    assert len(worker["output_summary"]) <= 1_200

    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT result_json,created_at,updated_at FROM goal_results WHERE goal_run_id=?",
                ("goal_result",),
            )
        ).fetchone()
        assert row is not None
        first_timestamps = (row[1], row[2])
        assert json.loads(str(row[0])) == result

    repeated = await aggregator.aggregate_goal("goal_result")
    assert repeated == result
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT created_at,updated_at FROM goal_results WHERE goal_run_id=?",
                ("goal_result",),
            )
        ).fetchone()
        assert row == first_timestamps


def test_worker_output_normalization_is_deterministic_redacted_and_bounded() -> None:
    first = {
        "safe": "hello",
        "nested": {"token": "never", "path": "/root/private/key.pem"},
    }
    second = {
        "nested": {"path": "/root/private/key.pem", "token": "never"},
        "safe": "hello",
    }
    first_summary = summarize_untrusted_worker_output(first, max_chars=80)
    second_summary = summarize_untrusted_worker_output(second, max_chars=80)
    assert first_summary == second_summary
    assert len(first_summary) <= 80
    assert "never" not in first_summary
    assert "/root" not in first_summary


@pytest.mark.asyncio
async def test_malformed_worker_json_is_omitted_instead_of_echoed(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    await _seed_goal(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE agent_jobs SET result_json=? WHERE id='job_worker'",
            ('{"password":"leak-me"',),
        )
        await db.commit()

    result = await ResultAggregator(db_path).aggregate_goal("goal_result")
    encoded = json.dumps(result)
    assert "leak-me" not in encoded
    assert result["nodes"][0]["output_summary"] == (
        "Worker result could not be parsed and was omitted."
    )
