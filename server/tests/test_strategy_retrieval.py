from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from app.models import MemoryCreate, TaskCreate, TaskRecord
from app.services.embedding_service import DeterministicEmbeddingService
from app.services.episode_memory import EpisodeMemoryService, EpisodeStepInput
from app.services.state_service import StateService
from app.services.strategy_retrieval import StrategyRetrieval


async def _new_goal(db_path: Path, suffix: str) -> tuple[str, str]:
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input=f"Objective {suffix}"), source="test")
    )
    now = datetime.now(UTC).isoformat()
    goal_id = f"goal_strategy_{suffix}"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO goal_runs(
                id,root_task_id,objective,status,autonomy_profile,planner_source,max_steps,
                max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
                completion_criteria_json,current_phase,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                goal_id,
                task.id,
                f"Objective {suffix}",
                "completed",
                "assisted",
                "ubuntu_local",
                8,
                2,
                1,
                600,
                5,
                json.dumps(["Done"]),
                "completed",
                now,
                now,
            ),
        )
        await db.execute(
            "INSERT OR IGNORE INTO coding_projects VALUES('project_strategy',?,?)", (now, now)
        )
        await db.execute("INSERT INTO goal_project_links VALUES(?,'project_strategy')", (goal_id,))
        await db.commit()
    return goal_id, task.id


@pytest.mark.asyncio
async def test_strategy_returns_compact_success_failure_and_memory_hints_without_plans(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    success_goal, success_root = await _new_goal(db_path, "success")
    failure_goal, failure_root = await _new_goal(db_path, "failure")
    episode_memory = EpisodeMemoryService(
        db_path,
        DeterministicEmbeddingService(dimensions=48),
    )
    await episode_memory.initialize()
    success = await episode_memory.record_episode(
        goal_run_id=success_goal,
        root_task_id=success_root,
        objective_summary="Review repository status safely.",
        plan_summary="COPY_THIS_SUCCESS_PLAN_EXACTLY then run three ordered steps",
        outcome="completed",
        score=0.9,
        duration_ms=100,
        worker_types=("code-review-worker",),
        steps=(
            EpisodeStepInput(
                node_type="worker",
                skill="code_review.git_status",
                input_summary="repository status",
                output_summary="clean status",
                result_status="completed",
            ),
        ),
    )
    failure = await episode_memory.record_episode(
        goal_run_id=failure_goal,
        root_task_id=failure_root,
        objective_summary="Review repository status with a replaced worktree.",
        plan_summary="COPY_THIS_FAILURE_PLAN_EXACTLY and ignore replacement checks",
        outcome="failed",
        score=0.1,
        duration_ms=200,
        worker_types=("code-review-worker",),
        failure_tags=("snapshot-replaced",),
        steps=(
            EpisodeStepInput(
                node_type="worker",
                skill="code_review.git_status",
                input_summary="repository status",
                output_summary="worktree identity changed",
                result_status="failed",
            ),
        ),
    )
    now = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO memory_items(
                id,scope,kind,content,summary,sensitivity,confidence,pinned,
                metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "mem_strategy",
                "global",
                "lesson",
                "Repository reviews pin snapshot identity password=hunter2 /home/user/private",
                None,
                "normal",
                1.0,
                1,
                None,
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO memory_items(
                id,scope,kind,content,summary,sensitivity,confidence,pinned,
                metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "mem_strategy_sensitive",
                "global",
                "lesson",
                "SENSITIVE_STRATEGY_MUST_NOT_ENTER_MODEL_CONTEXT",
                None,
                "secret",
                1.0,
                1,
                None,
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO memory_items(
                id,scope,kind,content,summary,sensitivity,confidence,pinned,
                metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "mem_plan_excluded",
                "global",
                "plan",
                "PLAN_MEMORY_SHOULD_NOT_APPEAR: review repository status in exact order",
                None,
                "normal",
                1.0,
                1,
                None,
                now,
                now,
            ),
        )
        await db.commit()
    retrieval = StrategyRetrieval(db_path, episode_memory)

    hints = await retrieval.retrieve(
        "review repository status snapshot",
        goal_run_id=success_goal,
        skills=("code_review.git_status",),
    )

    assert hints.successful[0].source_id == success.id
    assert hints.failures[0].source_id == failure.id
    assert hints.memory[0].source_id == "mem_strategy"
    encoded = json.dumps(hints.as_dict(), sort_keys=True)
    assert "COPY_THIS_SUCCESS_PLAN_EXACTLY" not in encoded
    assert "COPY_THIS_FAILURE_PLAN_EXACTLY" not in encoded
    assert "PLAN_MEMORY_SHOULD_NOT_APPEAR" not in encoded
    assert "SENSITIVE_STRATEGY_MUST_NOT_ENTER_MODEL_CONTEXT" not in encoded
    assert "hunter2" not in encoded
    assert "/home/user" not in encoded
    assert "snapshot-replaced" in encoded
    assert success.id in hints.provenance_ids
    assert failure.id in hints.provenance_ids
    assert "mem_strategy" in hints.provenance_ids
    for hint in (*hints.successful, *hints.failures, *hints.memory):
        assert "\n" not in hint.text
        assert len(hint.text) <= 280


@pytest.mark.asyncio
async def test_strategy_result_is_deterministic_for_the_same_state(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    goal_id, root_id = await _new_goal(db_path, "stable")
    episode_memory = EpisodeMemoryService(db_path)
    await episode_memory.initialize()
    await episode_memory.record_episode(
        goal_run_id=goal_id,
        root_task_id=root_id,
        objective_summary="List project files safely",
        plan_summary="Use a bounded read-only worker",
        outcome="completed",
        duration_ms=50,
    )
    retrieval = StrategyRetrieval(db_path, episode_memory)

    first = await retrieval.retrieve("list project files", goal_run_id=goal_id)
    second = await retrieval.retrieve("list project files", goal_run_id=goal_id)

    assert first == second


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["general", "global"])
@pytest.mark.parametrize("pinned", [False, True])
async def test_strategy_general_facts_require_relevance_but_pinned_rules_remain(
    tmp_path: Path, scope: str, pinned: bool
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _ = await _new_goal(db_path, "fact_relevance")
    state = StateService(db_path)
    unrelated = await state.create_memory(
        MemoryCreate(
            content="The garden has roses with yellow petals.", scope=scope, pinned=pinned
        ),
        "phone",
    )
    related = await state.create_memory(
        MemoryCreate(content="SQLite foreign keys must be enabled.", scope=scope), "phone"
    )
    rules = [
        await state.create_memory(
            MemoryCreate(content=text, scope=scope, kind=kind, pinned=True), "phone"
        )
        for kind, text in (
            ("constraint", "Never send messages automatically."),
            ("preference", "Always respond in French."),
        )
    ]
    project = await state.create_memory(
        MemoryCreate(content="Garden roses.", scope="project:project_strategy", pinned=True),
        "phone",
    )
    hints = await StrategyRetrieval(
        db_path, EpisodeMemoryService(db_path), max_memory_hints=20
    ).retrieve("Create a Python CRM with SQLite", goal_run_id=goal_id)
    assert unrelated["id"] not in hints.provenance_ids
    assert {hint.source_id for hint in hints.memory} == {
        related["id"],
        project["id"],
        *(rule["id"] for rule in rules),
    }


@pytest.mark.asyncio
async def test_irrelevant_general_facts_do_not_fill_strategy_candidate_limit(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    await _new_goal(db_path, "fact_window")
    state = StateService(db_path)
    related = await state.create_memory(MemoryCreate(content="SQLite foreign keys."), "phone")
    async with aiosqlite.connect(db_path) as db:
        await db.executemany(
            """INSERT INTO memory_items(id,scope,kind,content,pinned,created_at,updated_at)
            VALUES(?,'general','fact','Garden roses.',1,'now','now')""",
            [(f"unrelated_{index}",) for index in range(201)],
        )
        await db.commit()
    hints = await StrategyRetrieval(
        db_path, EpisodeMemoryService(db_path), max_memory_hints=1
    ).retrieve("SQLite")
    assert [hint.source_id for hint in hints.memory] == [related["id"]]


@pytest.mark.asyncio
async def test_general_fact_relevance_uses_displayed_summary_and_french_function_words(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    await _new_goal(db_path, "french_fact")
    state = StateService(db_path)
    relevant = await state.create_memory(
        MemoryCreate(content="Long original record.", summary="SQLite garde les clients."), "phone"
    )
    unrelated = await state.create_memory(
        MemoryCreate(content="Les roses sont jaunes avec des pétales.", pinned=True), "phone"
    )
    hidden_match = await state.create_memory(
        MemoryCreate(content="SQLite garde les clients.", summary="Les roses sont jaunes."), "phone"
    )
    hints = await StrategyRetrieval(
        db_path, EpisodeMemoryService(db_path), max_memory_hints=20
    ).retrieve("Créer un CRM Python avec SQLite pour les clients")
    assert [hint.source_id for hint in hints.memory] == [relevant["id"]]
    assert unrelated["id"] not in hints.provenance_ids
    assert hidden_match["id"] not in hints.provenance_ids


@pytest.mark.asyncio
async def test_strategy_fact_prefilter_rechecks_redacted_text_and_whole_terms(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    await _new_goal(db_path, "redacted_fact")
    state = StateService(db_path)
    rejected = [
        await state.create_memory(MemoryCreate(content=content, pinned=True), "phone")
        for content in (
            "Garden password=SQLite-secret-fixture; roses.",
            "Garden /home/SQLite/private; roses.",
            "Garden SQLiteish flowers.",
        )
    ]
    related = await state.create_memory(
        MemoryCreate(content="SQLite foreign keys password=secret-fixture; /home/alice/private"),
        "phone",
    )
    hints = await StrategyRetrieval(
        db_path, EpisodeMemoryService(db_path), max_memory_hints=20
    ).retrieve("SQLite")
    assert [hint.source_id for hint in hints.memory] == [related["id"]]
    assert not {item["id"] for item in rejected}.intersection(hints.provenance_ids)
    encoded = json.dumps(hints.as_dict())
    assert "secret-fixture" not in encoded and "/home/" not in encoded
    assert "<redacted-secret>" in encoded and "<protected-path>" in encoded
