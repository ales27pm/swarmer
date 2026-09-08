from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatcher
from app.services.agent_scheduler import SchedulerService
from app.services.message_board import SQLiteMessageBoard
from app.services.state_service import StateService


async def _online_agent(
    state: StateService, name: str, *, score: float | None = None
) -> dict[str, object]:
    agent = await state.register_agent(
        AgentCreate(
            name=name,
            endpoint=f"http://127.0.0.1:{9100 + len(name)}",
            version="0.10.0",
            skills=["workspace.list_dir"],
            max_concurrency=2,
        ),
        "phone",
    )
    assert await state.heartbeat_agent(str(agent["id"]), "online", str(agent["credential"]))
    if score is not None:
        async with aiosqlite.connect(state.db_path) as db:
            await db.execute(
                """
                INSERT INTO agent_score_snapshots(
                    agent_id,completed_jobs,failed_jobs,terminal_jobs,lease_expiry_count,
                    observed_outcomes,completion_rate,failure_rate,timeout_rate,
                    feedback_count,feedback_average,average_latency_seconds,
                    composite_score,formula_version,rebuilt_at
                ) VALUES(?,3,0,3,0,3,1,0,0,0,NULL,10,?,'test','2026-01-01T00:00:00+00:00')
                """,
                (agent["id"], score),
            )
            await db.commit()
    return agent


@pytest.mark.asyncio
async def test_same_scheduler_state_selects_same_agent(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    lower = await _online_agent(state, "lower", score=0.2)
    higher = await _online_agent(state, "higher", score=0.9)
    task = await state.create_task(TaskRecord.new(TaskCreate(input="list"), source="phone"))
    dispatcher = AgentDispatcher(state.db_path, SQLiteMessageBoard(state.db_path))
    job = await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    async with aiosqlite.connect(state.db_path) as db:
        scheduler = SchedulerService(state.db_path)
        first = await scheduler.select_for_job_locked(db, str(job["id"]))
        second = await scheduler.select_for_job_locked(db, str(job["id"]))

    assert first == second
    assert first is not None
    assert first.selected_agent_id == higher["id"]
    assert first.selected_agent_id != lower["id"]


@pytest.mark.asyncio
async def test_claim_persists_safe_scheduler_decision_evidence(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    agent = await _online_agent(state, "reader")
    task = await state.create_task(TaskRecord.new(TaskCreate(input="list"), source="phone"))
    dispatcher = AgentDispatcher(state.db_path, SQLiteMessageBoard(state.db_path))
    job = await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    claimed = await dispatcher.claim(str(agent["id"]))

    assert claimed is not None and claimed["id"] == job["id"]
    async with aiosqlite.connect(state.db_path) as db:
        row = await (
            await db.execute(
                """SELECT candidates_json,selected_agent_id,scoring_json
                FROM scheduler_decisions WHERE job_id=?""",
                (job["id"],),
            )
        ).fetchone()
    assert row is not None and row[1] == agent["id"]
    encoded = f"{row[0]} {row[2]}".casefold()
    assert not any(
        forbidden in encoded
        for forbidden in ("credential", "claim_token", "lease_token", "bearer", "payload")
    )
    assert json.loads(str(row[2]))["algorithm"] == "deterministic-v2"


@pytest.mark.asyncio
async def test_scheduler_prefers_an_alternative_after_lease_requeue(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    first = await _online_agent(state, "first")
    second = await _online_agent(state, "second")
    task = await state.create_task(TaskRecord.new(TaskCreate(input="list"), source="phone"))
    dispatcher = AgentDispatcher(state.db_path, SQLiteMessageBoard(state.db_path))
    job = await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE agent_jobs SET last_agent_id=? WHERE id=?",
            (first["id"], job["id"]),
        )
        await db.commit()

    async with aiosqlite.connect(state.db_path) as db:
        selection = await SchedulerService(state.db_path).select_for_job_locked(db, str(job["id"]))

    assert selection is not None
    assert selection.selected_agent_id == second["id"]


@pytest.mark.asyncio
async def test_scheduler_excludes_stale_online_agent_and_selects_fresh_peer(
    tmp_path: Path,
) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    stale = await _online_agent(state, "stale")
    fresh = await _online_agent(state, "fresh")
    now = datetime.now(UTC)
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            "UPDATE agents SET last_seen_at=?,last_heartbeat_at=? WHERE id=?",
            [
                (
                    (now - timedelta(minutes=10)).isoformat(),
                    (now - timedelta(minutes=10)).isoformat(),
                    stale["id"],
                ),
                (
                    (now - timedelta(seconds=1)).isoformat(),
                    (now - timedelta(seconds=1)).isoformat(),
                    fresh["id"],
                ),
            ],
        )
        await db.commit()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="list"), source="phone"))
    dispatcher = AgentDispatcher(state.db_path, SQLiteMessageBoard(state.db_path))
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    claimed = await dispatcher.claim(str(fresh["id"]))

    assert claimed is not None
    assert claimed["claimed_by"] == fresh["id"]
