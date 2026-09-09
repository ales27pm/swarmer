from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.models import TaskCreate, TaskRecord
from app.services.embedding_service import DeterministicEmbeddingService, EmbeddingServiceError
from app.services.episode_memory import EpisodeConflict, EpisodeMemoryService, EpisodeStepInput
from app.services.state_service import StateService


class FailingEmbeddingService:
    provider_name = "failing-test"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise EmbeddingServiceError("unavailable")


async def _seed_goal_run(db_path: Path, suffix: str) -> tuple[str, str]:
    state = StateService(db_path)
    await state.initialize()
    root = await state.create_task(
        TaskRecord.new(TaskCreate(input=f"Goal {suffix}"), source="test")
    )
    now = datetime.now(UTC).isoformat()
    goal_id = f"goal_{suffix}"
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
                root.id,
                f"Goal objective {suffix}",
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
        await db.commit()
    return goal_id, root.id


@pytest.mark.asyncio
async def test_episode_records_only_redacted_summaries_steps_and_embedding(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    goal_id, root_id = await _seed_goal_run(db_path, "redacted")
    service = EpisodeMemoryService(db_path, DeterministicEmbeddingService(dimensions=12))
    await service.initialize()

    episode = await service.record_episode(
        goal_run_id=goal_id,
        root_task_id=root_id,
        objective_summary="Inspect password=hunter2 under /home/user/private",
        plan_summary="1. Read token=secret-token 2. Return a safe summary",
        outcome="completed",
        score=0.9,
        duration_ms=123,
        worker_types=("file-worker",),
        steps=(
            EpisodeStepInput(
                node_type="worker",
                skill="workspace.read_text",
                agent_id="agent_file",
                input_summary="Read Bearer abcdefghijklmnop",
                output_summary="Result from /root/private",
                result_status="completed",
                latency_ms=50,
            ),
        ),
        user_feedback_score=4.5,
    )

    assert episode.goal_run_id == goal_id
    assert episode.steps[0].sequence == 1
    encoded = json.dumps(episode.as_dict(), sort_keys=True)
    for forbidden in (
        "hunter2",
        "secret-token",
        "abcdefghijklmnop",
        "/home/user",
        "/root/private",
    ):
        assert forbidden not in encoded
    assert "<redacted-secret>" in encoded
    async with aiosqlite.connect(db_path) as db:
        embedding = await (
            await db.execute(
                "SELECT dimensions,vector_json FROM episode_embeddings WHERE episode_id=?",
                (episode.id,),
            )
        ).fetchone()
    assert embedding is not None
    assert embedding[0] == 12
    assert len(json.loads(embedding[1])) == 12


@pytest.mark.asyncio
async def test_episode_search_combines_semantics_skill_outcome_and_failure_metadata(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    failed_goal, failed_root = await _seed_goal_run(db_path, "failed")
    success_goal, success_root = await _seed_goal_run(db_path, "success")
    now = datetime.now(UTC)
    service = EpisodeMemoryService(
        db_path,
        DeterministicEmbeddingService(dimensions=64),
        clock=lambda: now,
    )
    await service.initialize()
    failed = await service.record_episode(
        goal_run_id=failed_goal,
        root_task_id=failed_root,
        objective_summary="Research redis reconnect timeout",
        plan_summary="Fetch bounded redis documentation",
        outcome="failed",
        score=0.2,
        duration_ms=2_000,
        worker_types=("research-worker",),
        failure_tags=("network-timeout",),
        steps=(
            EpisodeStepInput(
                node_type="worker",
                skill="research.fetch_https",
                input_summary="redis reconnect",
                output_summary="request timed out",
                result_status="failed",
            ),
        ),
        user_feedback_score=1.0,
        created_at=now - timedelta(days=1),
    )
    await service.record_episode(
        goal_run_id=success_goal,
        root_task_id=success_root,
        objective_summary="Review a local source tree",
        plan_summary="Inspect repository metadata",
        outcome="completed",
        score=1.0,
        duration_ms=100,
        worker_types=("code-review-worker",),
        steps=(
            EpisodeStepInput(
                node_type="worker",
                skill="code_review.git_status",
                input_summary="repository status",
                output_summary="clean worktree",
                result_status="completed",
            ),
        ),
        user_feedback_score=5.0,
        created_at=now,
    )

    results = await service.search(
        "redis reconnect timeout",
        skills=("research.fetch_https",),
        preferred_outcome="failed",
        limit=2,
    )

    assert results[0].episode.id == failed.id
    assert results[0].search_kind == "hybrid"
    assert results[0].components["skill_overlap"] == 1.0
    assert results[0].components["outcome"] == 1.0


@pytest.mark.asyncio
async def test_embedding_failure_preserves_episode_and_uses_lexical_fallback(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, root_id = await _seed_goal_run(db_path, "fallback")
    service = EpisodeMemoryService(db_path, FailingEmbeddingService())
    await service.initialize()
    episode = await service.record_episode(
        goal_run_id=goal_id,
        root_task_id=root_id,
        objective_summary="Inspect canoe route",
        plan_summary="Summarize lake access",
        outcome="completed",
        duration_ms=10,
    )

    results = await service.search("canoe route")

    assert results[0].episode.id == episode.id
    assert results[0].search_kind == "lexical"
    async with aiosqlite.connect(db_path) as db:
        count = await (
            await db.execute(
                "SELECT COUNT(*) FROM episode_embeddings WHERE episode_id=?",
                (episode.id,),
            )
        ).fetchone()
    assert count is not None and count[0] == 0


@pytest.mark.asyncio
async def test_episode_recording_is_idempotent_per_goal_and_rejects_semantic_rewrite(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, root_id = await _seed_goal_run(db_path, "idempotent")
    service = EpisodeMemoryService(db_path)
    await service.initialize()
    arguments = {
        "goal_run_id": goal_id,
        "root_task_id": root_id,
        "objective_summary": "List project files",
        "plan_summary": "Use the read-only listing skill",
        "outcome": "completed",
        "duration_ms": 25,
        "worker_types": ("file-worker",),
    }

    first = await service.record_episode(**arguments)
    replay = await service.record_episode(**arguments)

    assert replay.id == first.id
    with pytest.raises(EpisodeConflict, match="different episode"):
        await service.record_episode(**{**arguments, "plan_summary": "Different summary"})
    async with aiosqlite.connect(db_path) as db:
        count = await (
            await db.execute("SELECT COUNT(*) FROM episodes WHERE goal_run_id=?", (goal_id,))
        ).fetchone()
    assert count is not None and count[0] == 1

    updated = await service.set_user_feedback_score(goal_id, 4.75)
    assert updated is not None
    assert updated.user_feedback_score == 4.75
