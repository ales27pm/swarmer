from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.services.agent_scoring import AgentScoreSnapshot, AgentScoringService
from app.services.control_plane_instance import ControlPlaneInstanceService
from app.services.maintenance_lease import (
    MaintenanceLeaseGuard,
    MaintenanceLeaseLost,
    MaintenanceLeaseRunner,
    MaintenanceLeaseService,
)
from app.services.state_service import StateService

FIXED_NOW = datetime(2026, 9, 8, 18, 0, tzinfo=UTC)


async def initialize(database: Path) -> AgentScoringService:
    await StateService(database).initialize()
    service = AgentScoringService(database, clock=lambda: FIXED_NOW)
    await service.initialize()
    await service.initialize()
    return service


async def insert_agent(db: aiosqlite.Connection, agent_id: str) -> None:
    now = FIXED_NOW.isoformat()
    await db.execute(
        """
        INSERT INTO agents(
            id,name,version,endpoint,status,skills_json,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (agent_id, agent_id, "1.0.0", "http://127.0.0.1:9000", "online", "[]", now, now),
    )


async def insert_job(
    db: aiosqlite.Connection,
    job_id: str,
    *,
    status: str,
    claimed_by: str | None,
    last_agent_id: str | None,
    latency_seconds: int | None = 10,
    result: dict[str, object] | None = None,
) -> None:
    claimed_at = FIXED_NOW - timedelta(minutes=10)
    completed_at = (
        claimed_at + timedelta(seconds=latency_seconds) if latency_seconds is not None else None
    )
    await db.execute(
        """
        INSERT INTO agent_jobs(
            id,task_id,required_skill,payload_json,status,claimed_by,last_agent_id,
            result_json,created_at,updated_at,claimed_at,completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            job_id,
            f"task-{job_id}",
            "workspace.list_dir",
            "{}",
            status,
            claimed_by,
            last_agent_id,
            json.dumps(result) if result is not None else None,
            claimed_at.isoformat(),
            completed_at.isoformat() if completed_at is not None else claimed_at.isoformat(),
            claimed_at.isoformat() if latency_seconds is not None else "not-a-timestamp",
            completed_at.isoformat() if completed_at is not None else "also-invalid",
        ),
    )


async def insert_feedback(
    db: aiosqlite.Connection,
    feedback_id: str,
    agent_id: str,
    score: float,
) -> None:
    await db.execute(
        """
        INSERT INTO feedback_events(
            id,agent_id,type,score,payload_json,created_at
        ) VALUES(?,?,'rating',?,'{}',?)
        """,
        (feedback_id, agent_id, score, FIXED_NOW.isoformat()),
    )


async def insert_lease_expiry(
    db: aiosqlite.Connection,
    *,
    payload: object,
) -> None:
    encoded = payload if isinstance(payload, str) else json.dumps(payload)
    await db.execute(
        """
        INSERT INTO audit_events(event_type,payload_json,created_at)
        VALUES('agent.job.lease_expired',?,?)
        """,
        (encoded, FIXED_NOW.isoformat()),
    )


@pytest.mark.asyncio
async def test_rebuild_computes_transparent_server_observed_metrics(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    service = await initialize(database)
    async with aiosqlite.connect(database) as db:
        await insert_agent(db, "agent-a")
        await insert_job(
            db,
            "completed-1",
            status="completed",
            claimed_by="agent-a",
            last_agent_id="agent-a",
            latency_seconds=10,
        )
        await insert_job(
            db,
            "completed-2",
            status="completed",
            claimed_by="agent-a",
            last_agent_id="agent-a",
            latency_seconds=30,
        )
        await insert_job(
            db,
            "failed-1",
            status="failed",
            claimed_by="agent-a",
            last_agent_id="agent-a",
            latency_seconds=20,
            result={"self_reported_success": True},
        )
        await insert_job(
            db,
            "cancelled-ignored",
            status="cancelled",
            claimed_by="agent-a",
            last_agent_id="agent-a",
        )
        await insert_feedback(db, "feedback-1", "agent-a", 5.0)
        await insert_feedback(db, "feedback-2", "agent-a", 3.0)
        await insert_lease_expiry(
            db,
            payload={"job_id": "expired-1", "agent_id": "agent-a", "lease_generation": 1},
        )
        await db.commit()

    snapshots = await service.rebuild()

    assert len(snapshots) == 1
    score = snapshots[0]
    assert score.agent_id == "agent-a"
    assert score.completed_jobs == 2
    assert score.failed_jobs == 1
    assert score.terminal_jobs == 3
    assert score.lease_expiry_count == 1
    assert score.observed_outcomes == 4
    assert score.completion_rate == pytest.approx(0.666667)
    assert score.failure_rate == pytest.approx(0.333333)
    assert score.timeout_rate == 0.25
    assert score.feedback_count == 2
    assert score.feedback_average == 4.0
    assert score.average_latency_seconds == 20.0
    assert score.composite_score == pytest.approx(0.71)
    assert score.formula_version == "server-observed-v1"
    assert score.rebuilt_at == FIXED_NOW.isoformat()


@pytest.mark.asyncio
async def test_last_agent_id_is_authoritative_over_claimed_by(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    service = await initialize(database)
    async with aiosqlite.connect(database) as db:
        await insert_agent(db, "agent-a")
        await insert_agent(db, "agent-b")
        await insert_job(
            db,
            "job-reassigned",
            status="completed",
            claimed_by="agent-b",
            last_agent_id="agent-a",
        )
        await db.commit()

    await service.rebuild()

    score_a = await service.get("agent-a")
    score_b = await service.get("agent-b")
    assert score_a is not None and score_a.completed_jobs == 1
    assert score_b is not None and score_b.terminal_jobs == 0


@pytest.mark.asyncio
async def test_lease_expiry_requires_server_audit_payload_agent_id(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    service = await initialize(database)
    async with aiosqlite.connect(database) as db:
        await insert_agent(db, "agent-a")
        await insert_lease_expiry(db, payload={"job_id": "missing-agent"})
        await insert_lease_expiry(db, payload="not-json")
        await db.execute(
            """
            INSERT INTO audit_events(event_type,actor_id,payload_json,created_at)
            VALUES('agent.job.lease_expired','agent-a','{}',?)
            """,
            (FIXED_NOW.isoformat(),),
        )
        await insert_lease_expiry(
            db,
            payload={"job_id": "observed", "agent_id": "agent-a", "lease_generation": 2},
        )
        await db.commit()

    await service.rebuild()
    score = await service.get("agent-a")

    assert score is not None
    assert score.lease_expiry_count == 1
    assert score.timeout_rate == 1.0


@pytest.mark.asyncio
async def test_missing_evidence_is_neutral_and_ranking_tie_is_stable(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    service = await initialize(database)
    async with aiosqlite.connect(database) as db:
        await insert_agent(db, "agent-z")
        await insert_agent(db, "agent-a")
        await db.commit()

    first = await service.rebuild()
    second = await service.rebuild()
    ranked = await service.list_ranked()

    assert first == second
    assert [score.agent_id for score in ranked] == ["agent-a", "agent-z"]
    assert {score.composite_score for score in ranked} == {0.5}
    assert all(score.completion_rate == 0.0 for score in ranked)
    assert all(score.failure_rate == 0.0 for score in ranked)
    assert all(score.timeout_rate == 0.0 for score in ranked)


@pytest.mark.asyncio
async def test_malformed_or_negative_timestamps_are_excluded_from_latency(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    service = await initialize(database)
    async with aiosqlite.connect(database) as db:
        await insert_agent(db, "agent-a")
        await insert_job(
            db,
            "invalid-time",
            status="failed",
            claimed_by="agent-a",
            last_agent_id="agent-a",
            latency_seconds=None,
        )
        claimed = FIXED_NOW.isoformat()
        completed = (FIXED_NOW - timedelta(seconds=5)).isoformat()
        await db.execute(
            """
            INSERT INTO agent_jobs(
                id,task_id,required_skill,payload_json,status,claimed_by,last_agent_id,
                created_at,updated_at,claimed_at,completed_at
            ) VALUES(?,?,'workspace.read_text','{}','completed',?,?,?, ?,?,?)
            """,
            (
                "negative-time",
                "task-negative-time",
                "agent-a",
                "agent-a",
                claimed,
                completed,
                claimed,
                completed,
            ),
        )
        await db.commit()

    await service.rebuild()
    score = await service.get("agent-a")

    assert score is not None
    assert score.terminal_jobs == 2
    assert score.average_latency_seconds is None


@pytest.mark.asyncio
async def test_rebuild_replaces_stale_projection_without_touching_source_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    service = await initialize(database)
    async with aiosqlite.connect(database) as db:
        await insert_agent(db, "agent-a")
        await insert_agent(db, "agent-stale")
        await insert_job(
            db,
            "job-a",
            status="completed",
            claimed_by="agent-a",
            last_agent_id="agent-a",
        )
        await db.commit()
    await service.rebuild()

    async with aiosqlite.connect(database) as db:
        await db.execute("DELETE FROM agents WHERE id='agent-stale'")
        await db.execute("UPDATE agent_jobs SET status='failed' WHERE id='job-a'")
        await db.commit()
    rebuilt = await service.rebuild()

    assert [score.agent_id for score in rebuilt] == ["agent-a"]
    assert await service.get("agent-stale") is None
    score = await service.get("agent-a")
    assert score is not None
    assert score.completed_jobs == 0
    assert score.failed_jobs == 1
    async with aiosqlite.connect(database) as db:
        source = await (
            await db.execute("SELECT status FROM agent_jobs WHERE id='job-a'")
        ).fetchone()
    assert source == ("failed",)


@pytest.mark.asyncio
async def test_slow_large_read_keeps_maintenance_lease_renewable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp_scoring_test",
        hostname="ubuntu-test",
    )
    await instance.start()
    leases = MaintenanceLeaseService(database, lease_seconds=2)
    runner = MaintenanceLeaseRunner(
        leases,
        owner_instance_id=instance.instance_id,
        renewal_interval_seconds=0.05,
    )
    service = AgentScoringService(
        database,
        maintenance_leases=leases,
        owner_instance_id=instance.instance_id,
    )
    await service.initialize()
    now = FIXED_NOW.isoformat()
    async with aiosqlite.connect(database) as db:
        await db.executemany(
            """
            INSERT INTO agents(
                id,name,version,endpoint,status,skills_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            [
                (
                    f"agent-{index:04d}",
                    f"agent-{index:04d}",
                    "1.0.0",
                    "http://127.0.0.1:9000",
                    "online",
                    "[]",
                    now,
                    now,
                )
                for index in range(512)
            ],
        )
        await db.commit()

    read_is_slow = asyncio.Event()
    finish_read = asyncio.Event()
    original_agent_ids = service._agent_ids_locked

    async def slow_agent_ids(db: aiosqlite.Connection) -> set[str]:
        agent_ids = await original_agent_ids(db)
        read_is_slow.set()
        await finish_read.wait()
        return agent_ids

    monkeypatch.setattr(service, "_agent_ids_locked", slow_agent_ids)

    async def rebuild(guard: MaintenanceLeaseGuard) -> list[AgentScoreSnapshot]:
        return await service.rebuild(maintenance_guard=guard)

    running = asyncio.create_task(runner.run("feedback-maintenance", rebuild))
    await asyncio.wait_for(read_is_slow.wait(), timeout=1)
    initial = await leases.get("feedback-maintenance")
    assert initial is not None

    renewed = None
    for _ in range(40):
        candidate = await leases.get("feedback-maintenance")
        if candidate is not None and candidate.renewed_at > initial.renewed_at:
            renewed = candidate
            break
        await asyncio.sleep(0.025)

    finish_read.set()
    snapshots = await asyncio.wait_for(running, timeout=2)

    assert renewed is not None
    assert renewed.generation == initial.generation
    assert snapshots is not None and len(snapshots) == 512


@pytest.mark.asyncio
async def test_stale_scoring_generation_cannot_replace_projection_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    initial_service = await initialize(database)
    async with aiosqlite.connect(database) as db:
        await insert_agent(db, "agent-old")
        await db.commit()
    await initial_service.rebuild()

    current_time = [datetime(2026, 9, 8, 19, 0, tzinfo=UTC)]

    def clock() -> datetime:
        return current_time[0]

    for instance_id in ("cp_scoring_a", "cp_scoring_b"):
        await ControlPlaneInstanceService(
            database,
            version="0.11.0",
            instance_id=instance_id,
            hostname="ubuntu-test",
            clock=clock,
        ).start()
    leases = MaintenanceLeaseService(database, lease_seconds=2, clock=clock)
    service = AgentScoringService(
        database,
        maintenance_leases=leases,
        owner_instance_id="cp_scoring_a",
        clock=clock,
    )
    lease = await leases.acquire("feedback-maintenance", "cp_scoring_a")
    assert lease is not None
    guard = MaintenanceLeaseGuard(leases, lease)

    async with aiosqlite.connect(database) as db:
        await db.execute("DELETE FROM agents WHERE id='agent-old'")
        await insert_agent(db, "agent-new")
        await db.commit()

    read_is_slow = asyncio.Event()
    finish_read = asyncio.Event()
    original_agent_ids = service._agent_ids_locked

    async def slow_agent_ids(db: aiosqlite.Connection) -> set[str]:
        agent_ids = await original_agent_ids(db)
        read_is_slow.set()
        await finish_read.wait()
        return agent_ids

    monkeypatch.setattr(service, "_agent_ids_locked", slow_agent_ids)
    running = asyncio.create_task(service.rebuild(maintenance_guard=guard))
    await asyncio.wait_for(read_is_slow.wait(), timeout=1)
    current_time[0] += timedelta(seconds=3)
    takeover = await leases.acquire("feedback-maintenance", "cp_scoring_b")
    assert takeover is not None and takeover.generation == lease.generation + 1
    finish_read.set()

    with pytest.raises(MaintenanceLeaseLost):
        await asyncio.wait_for(running, timeout=1)
    assert await initial_service.get("agent-old") is not None
    assert await initial_service.get("agent-new") is None
