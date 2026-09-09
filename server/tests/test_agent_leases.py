from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.agent_lease_reaper import AgentLeaseReaper
from app.services.message_board import MessageBoardService
from app.services.state_service import SCHEMA_VERSION, StateService


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


async def set_agent_seen(database: Path, agent_id: str, at: datetime) -> None:
    timestamp = at.isoformat()
    async with aiosqlite.connect(database) as db:
        await db.execute(
            "UPDATE agents SET last_seen_at=?,last_heartbeat_at=? WHERE id=?",
            (timestamp, timestamp, agent_id),
        )
        await db.commit()


async def setup_runtime(
    database: Path, clock: MutableClock, *, max_attempts: int = 3
) -> tuple[StateService, AgentDispatcher, AgentLeaseReaper, dict[str, Any], str]:
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="list root"), source="phone"))
    agent = await state.register_agent(
        AgentCreate(
            name="reader-one",
            endpoint="http://127.0.0.1:9001",
            skills=["workspace.list_dir"],
        ),
        "phone",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    await set_agent_seen(database, str(agent["id"]), clock.value)
    board = MessageBoardService(database)
    dispatcher = AgentDispatcher(
        database, board, lease_seconds=60, max_attempts=max_attempts, clock=clock
    )
    reaper = AgentLeaseReaper(database, board, clock=clock)
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
    return state, dispatcher, reaper, agent, task.id


@pytest.mark.asyncio
async def test_claim_heartbeat_and_result_require_current_lease(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    _, dispatcher, _, agent, _ = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    assert claimed["lease_id"].startswith("lease_")
    assert claimed["lease_generation"] == 1

    renewed = await dispatcher.heartbeat(
        agent["id"],
        claimed["id"],
        claimed["claim_token"],
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
    )
    assert renewed["status"] == "running"

    with pytest.raises(AgentDispatchConflict, match="stale"):
        await dispatcher.submit_result(
            agent["id"],
            claimed["id"],
            claimed["claim_token"],
            lease_id="lease_wrong",
            lease_generation=claimed["lease_generation"],
            status="completed",
            result={},
            error=None,
        )


@pytest.mark.asyncio
async def test_expired_lease_is_requeued_and_old_holder_is_fenced(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, reaper, first, _ = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(first["id"])
    assert claimed is not None
    second = await state.register_agent(
        AgentCreate(
            name="reader-two",
            endpoint="http://127.0.0.1:9002",
            skills=["workspace.list_dir"],
        ),
        "phone",
    )
    assert await state.heartbeat_agent(second["id"], "online", second["credential"])
    await set_agent_seen(state.db_path, str(second["id"]), clock.value)

    clock.advance(61)
    assert await reaper.reap_expired() == {
        "expired": 1,
        "requeued": 1,
        "dead_lettered": 0,
        "cancelled": 0,
        "quarantined": 0,
    }
    reclaimed = await dispatcher.claim(second["id"])
    assert reclaimed is not None
    assert reclaimed["id"] == claimed["id"]
    assert reclaimed["lease_generation"] == claimed["lease_generation"] + 1

    with pytest.raises(AgentDispatchConflict, match="stale"):
        await dispatcher.submit_result(
            first["id"],
            claimed["id"],
            claimed["claim_token"],
            lease_id=claimed["lease_id"],
            lease_generation=claimed["lease_generation"],
            status="completed",
            result={},
            error=None,
        )


@pytest.mark.asyncio
async def test_heartbeat_prevents_expiry(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    _, dispatcher, reaper, agent, _ = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    clock.advance(40)
    await dispatcher.heartbeat(
        agent["id"],
        claimed["id"],
        claimed["claim_token"],
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
    )
    clock.advance(30)
    assert (await reaper.reap_expired())["expired"] == 0


@pytest.mark.asyncio
async def test_heartbeat_rechecks_lease_expiry_after_writer_lock_wait(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, _, agent, _ = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None

    blocker = await aiosqlite.connect(state.db_path)
    await blocker.execute("BEGIN IMMEDIATE")
    running = asyncio.create_task(
        dispatcher.heartbeat(
            agent["id"],
            claimed["id"],
            claimed["claim_token"],
            lease_id=claimed["lease_id"],
            lease_generation=claimed["lease_generation"],
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert not running.done()
        clock.advance(61)
        await blocker.commit()
    finally:
        await blocker.close()

    with pytest.raises(AgentDispatchConflict, match="stale"):
        await asyncio.wait_for(running, timeout=1)
    persisted = await dispatcher.get_job(claimed["id"])
    assert persisted is not None
    assert persisted["heartbeat_at"] == claimed["heartbeat_at"]
    assert persisted["lease_expires_at"] == claimed["lease_expires_at"]


@pytest.mark.asyncio
async def test_result_rechecks_lease_expiry_after_writer_lock_wait(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, _, agent, task_id = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None

    blocker = await aiosqlite.connect(state.db_path)
    await blocker.execute("BEGIN IMMEDIATE")
    running = asyncio.create_task(
        dispatcher.submit_result(
            agent["id"],
            claimed["id"],
            claimed["claim_token"],
            lease_id=claimed["lease_id"],
            lease_generation=claimed["lease_generation"],
            status="completed",
            result={"entries": []},
            error=None,
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert not running.done()
        clock.advance(61)
        await blocker.commit()
    finally:
        await blocker.close()

    with pytest.raises(AgentDispatchConflict, match="stale"):
        await asyncio.wait_for(running, timeout=1)
    persisted = await dispatcher.get_job(claimed["id"])
    task = await state.get_task(task_id)
    assert persisted is not None and persisted["status"] == "claimed"
    assert task is not None and task.status.value == "running"


@pytest.mark.asyncio
async def test_heartbeats_coalesce_publication_per_lease_generation(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, _, agent, _ = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None

    for _ in range(5):
        clock.advance(1)
        renewed = await dispatcher.heartbeat(
            agent["id"],
            claimed["id"],
            claimed["claim_token"],
            lease_id=claimed["lease_id"],
            lease_generation=claimed["lease_generation"],
        )

    assert renewed["heartbeat_at"] == clock().isoformat()
    async with aiosqlite.connect(state.db_path) as db:
        outbox_count = int(
            (
                await (
                    await db.execute(
                        """
                        SELECT COUNT(*) FROM outbox_events
                        WHERE aggregate_id=? AND event_type='heartbeat'
                        """,
                        (claimed["id"],),
                    )
                ).fetchone()
            )[0]
        )
        board_count = int(
            (
                await (
                    await db.execute(
                        """
                        SELECT COUNT(*) FROM message_board_events
                        WHERE message_id=? AND event_type='heartbeat'
                        """,
                        (claimed["id"],),
                    )
                ).fetchone()
            )[0]
        )
    assert outbox_count == 1
    assert board_count == 1


@pytest.mark.asyncio
async def test_queue_rejects_second_active_job_for_task(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    _, dispatcher, _, _, task_id = await setup_runtime(tmp_path / "state.db", clock)
    with pytest.raises(AgentDispatchConflict, match="status queued"):
        await dispatcher.queue_job(task_id, "workspace.list_dir", {"path": "."})


@pytest.mark.asyncio
async def test_parent_cancellation_fences_lease_and_publishes_new_generation(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, _, agent, task_id = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None

    cancelled = await state.cancel_task(task_id, actor_id="phone")

    assert cancelled is not None and cancelled.status.value == "cancelled"
    job = await dispatcher.get_job(claimed["id"])
    assert job is not None
    fenced_generation = claimed["lease_generation"] + 1
    assert job["status"] == "cancelled"
    assert job["lease_generation"] == fenced_generation
    assert job["claimed_by"] is None
    assert job["lease_id"] is None
    with pytest.raises(AgentDispatchConflict, match="stale"):
        await dispatcher.submit_result(
            agent["id"],
            claimed["id"],
            claimed["claim_token"],
            lease_id=claimed["lease_id"],
            lease_generation=claimed["lease_generation"],
            status="completed",
            result={},
            error=None,
        )
    async with aiosqlite.connect(state.db_path) as db:
        audit = await (
            await db.execute(
                """
                SELECT payload_json FROM audit_events
                WHERE event_type='agent.job.cancelled' AND task_id=?
                """,
                (task_id,),
            )
        ).fetchone()
        outbox = await (
            await db.execute(
                """
                SELECT payload_json,dedupe_key FROM outbox_events
                WHERE aggregate_id=? AND event_type='cancelled'
                """,
                (claimed["id"],),
            )
        ).fetchone()
    assert audit is not None
    assert json.loads(str(audit[0]))["lease_generation"] == fenced_generation
    assert outbox is not None
    assert json.loads(str(outbox[0])) == {
        "job_id": claimed["id"],
        "lease_generation": fenced_generation,
        "status": "cancelled",
    }
    assert outbox[1] == f"agent-job:{claimed['id']}:cancelled:{fenced_generation}"


@pytest.mark.asyncio
async def test_v09_migration_fences_duplicate_jobs_and_invalidates_plaintext_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="legacy read"), source="phone"))
    agent = await state.register_agent(
        AgentCreate(
            name="legacy-reader",
            endpoint="http://127.0.0.1:9001",
            skills=["workspace.list_dir"],
        ),
        "phone",
    )
    now = "2026-01-01T00:00:00+00:00"
    async with aiosqlite.connect(database) as db:
        await db.execute("DROP INDEX idx_agent_jobs_one_active_task")
        await db.execute("UPDATE tasks SET status='running' WHERE id=?", (task.id,))
        await db.execute(
            """
            INSERT INTO agent_jobs(
                id,task_id,required_skill,payload_json,status,claimed_by,claim_token,
                created_at,updated_at,claimed_at,heartbeat_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job_legacy_primary",
                task.id,
                "workspace.list_dir",
                '{"path":"."}',
                "running",
                agent["id"],
                "legacy-plaintext-claim",
                now,
                now,
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO agent_jobs(
                id,task_id,required_skill,payload_json,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "job_legacy_duplicate",
                task.id,
                "workspace.list_dir",
                '{"path":"."}',
                "queued",
                now,
                now,
            ),
        )
        await db.execute(
            "INSERT INTO devices(id,name,token,created_at) VALUES(?,?,?,?)",
            ("legacy-phone", "Legacy", "sha256:" + "b" * 64, now),
        )
        await db.execute(
            """
            INSERT INTO iphone_capability_requests(
                id,task_id,requesting_agent_id,requesting_job_id,lease_generation,
                device_id,capability_name,arguments_json,request_fingerprint,
                action_digest,status,approval_id,request_audit_id,created_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "iphreq_legacy",
                task.id,
                agent["id"],
                "job_legacy_primary",
                0,
                "legacy-phone",
                "iphone.location.current",
                "{}",
                "legacy-fingerprint",
                "sha256:" + "c" * 64,
                "waiting_approval",
                "icapr_legacy",
                1,
                now,
                "2099-01-01T00:00:00+00:00",
            ),
        )
        await db.commit()

    await state.initialize()

    async with aiosqlite.connect(database) as db:
        db.row_factory = aiosqlite.Row
        jobs = list(
            await (
                await db.execute(
                    "SELECT id,status,claim_token,lease_generation FROM agent_jobs ORDER BY id"
                )
            ).fetchall()
        )
        parent = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (task.id,))
        ).fetchone()
        index = await (
            await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_agent_jobs_one_active_task'"
            )
        ).fetchone()
        capability = await (
            await db.execute(
                "SELECT status FROM iphone_capability_requests WHERE id='iphreq_legacy'"
            )
        ).fetchone()
    by_id = {str(row["id"]): row for row in jobs}
    assert by_id["job_legacy_primary"]["status"] == "queued"
    assert by_id["job_legacy_primary"]["claim_token"] is None
    assert int(by_id["job_legacy_primary"]["lease_generation"]) == 1
    assert by_id["job_legacy_duplicate"]["status"] == "cancelled"
    assert parent is not None and parent[0] == "queued"
    assert capability is not None and capability[0] == "cancelled"
    assert index is not None and "WHERE status IN" in str(index[0])


@pytest.mark.asyncio
async def test_v09_migration_preserves_queued_job_for_queued_parent_with_leased_duplicate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="legacy read"), source="phone"))
    agent = await state.register_agent(
        AgentCreate(
            name="legacy-reader",
            endpoint="http://127.0.0.1:9001",
            skills=["workspace.list_dir"],
        ),
        "phone",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    now = "2026-01-01T00:00:00+00:00"
    async with aiosqlite.connect(database) as db:
        await db.execute("DROP INDEX idx_agent_jobs_one_active_task")
        await db.execute("UPDATE tasks SET status='queued' WHERE id=?", (task.id,))
        await db.execute(
            """
            INSERT INTO agent_jobs(
                id,task_id,required_skill,payload_json,status,claimed_by,
                lease_id,lease_token_hash,lease_expires_at,lease_generation,
                created_at,updated_at,claimed_at,heartbeat_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job_leased_duplicate",
                task.id,
                "workspace.list_dir",
                '{"path":"."}',
                "claimed",
                agent["id"],
                "lease_valid_123",
                "sha256:" + "a" * 64,
                "2099-01-01T00:00:00+00:00",
                1,
                now,
                now,
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO agent_jobs(
                id,task_id,required_skill,payload_json,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "job_queued_survivor",
                task.id,
                "workspace.list_dir",
                '{"path":"."}',
                "queued",
                now,
                now,
            ),
        )
        await db.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
        await db.commit()

    await state.initialize()

    async with aiosqlite.connect(database) as db:
        jobs = await (
            await db.execute("SELECT id,status,lease_generation FROM agent_jobs ORDER BY id")
        ).fetchall()
        parent = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (task.id,))
        ).fetchone()
    assert jobs == [
        ("job_leased_duplicate", "cancelled", 2),
        ("job_queued_survivor", "queued", 0),
    ]
    assert parent == ("queued",)
    claimed = await AgentDispatcher(database, MessageBoardService(database)).claim(agent["id"])
    assert claimed is not None
    assert claimed["id"] == "job_queued_survivor"


@pytest.mark.parametrize(
    ("task_status", "job_status", "expected_task_status"),
    [
        ("blocked", "queued", "blocked"),
        ("waiting_permission", "claimed", "waiting_permission"),
        ("running", "queued", "failed"),
        ("queued", "claimed", "failed"),
    ],
)
@pytest.mark.asyncio
async def test_migration_fences_active_job_with_incompatible_task_state(
    tmp_path: Path,
    task_status: str,
    job_status: str,
    expected_task_status: str,
) -> None:
    database = tmp_path / f"{task_status}-{job_status}.db"
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="legacy incompatible job"), source="phone")
    )
    agent = await state.register_agent(
        AgentCreate(
            name="legacy-reader",
            endpoint="http://127.0.0.1:9001",
            skills=["workspace.list_dir"],
        ),
        "phone",
    )
    now = "2026-01-01T00:00:00+00:00"
    async with aiosqlite.connect(database) as db:
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (task_status, task.id))
        await db.execute(
            """
            INSERT INTO agent_jobs(
                id,task_id,required_skill,payload_json,status,claimed_by,lease_id,
                lease_token_hash,lease_expires_at,lease_generation,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job_incompatible",
                task.id,
                "workspace.list_dir",
                '{"path":"."}',
                job_status,
                agent["id"] if job_status == "claimed" else None,
                "lease_incompatible" if job_status == "claimed" else None,
                "sha256:" + "a" * 64 if job_status == "claimed" else None,
                "2099-01-01T00:00:00+00:00" if job_status == "claimed" else None,
                1 if job_status == "claimed" else 0,
                now,
                now,
            ),
        )
        await db.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
        await db.commit()

    await state.initialize()

    async with aiosqlite.connect(database) as db:
        job = await (
            await db.execute(
                "SELECT status,lease_generation,lease_id FROM agent_jobs WHERE id=?",
                ("job_incompatible",),
            )
        ).fetchone()
        parent = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (task.id,))
        ).fetchone()
    assert job is not None
    assert job[0] == "cancelled"
    assert int(job[1]) == (2 if job_status == "claimed" else 1)
    assert job[2] is None
    assert parent is not None and parent[0] == expected_task_status
