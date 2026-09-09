import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
import yaml

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatcher
from app.services.agent_scheduler import SchedulerSelection
from app.services.message_board import MessageBoardService
from app.services.permission_policy import PermissionPolicy, PermissionPolicyError
from app.services.state_service import StateService

REPO_ROOT = Path(__file__).resolve().parents[2]


def denied_worker_policy(skill: str) -> PermissionPolicy:
    base = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    worker_rules = dict(base.worker_skill_rules)
    worker_rules[skill] = replace(worker_rules[skill], decision="deny", auto_redistribute=False)
    return PermissionPolicy(
        protected_paths=base.protected_paths,
        process=base.process,
        tool_rules=base.tool_rules,
        capability_rules=base.capability_rules,
        worker_skill_rules=worker_rules,
    )


@pytest.mark.asyncio
async def test_only_one_agent_can_claim_one_job(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="device"))
    first = await state.register_agent(
        AgentCreate(
            name="first",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    second = await state.register_agent(
        AgentCreate(
            name="second",
            endpoint="http://127.0.0.1:2",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    assert await state.heartbeat_agent(first["id"], "online", first["credential"])
    assert await state.heartbeat_agent(second["id"], "online", second["credential"])
    dispatcher = AgentDispatcher(state.db_path, MessageBoardService(state.db_path))
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    claimed = await dispatcher.claim(first["id"])
    unavailable = await dispatcher.claim(second["id"])
    assert claimed is not None
    assert unavailable is None


@pytest.mark.asyncio
async def test_claim_lease_timestamp_is_fresh_after_writer_lock_wait(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="device"))
    agent = await state.register_agent(
        AgentCreate(
            name="fresh-lease-reader",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    current_time = [datetime.now(UTC)]

    def clock() -> datetime:
        return current_time[0]

    dispatcher = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        lease_seconds=10,
        agent_offline_timeout_seconds=300,
        clock=clock,
    )
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    blocker = await aiosqlite.connect(state.db_path)
    await blocker.execute("BEGIN IMMEDIATE")
    running = asyncio.create_task(dispatcher.claim(agent["id"]))
    try:
        await asyncio.sleep(0.05)
        assert not running.done()
        # Simulate contention longer than the configured lease TTL without
        # making the test sleep for ten seconds.
        current_time[0] += timedelta(seconds=20)
        await blocker.commit()
    finally:
        await blocker.close()

    claimed = await asyncio.wait_for(running, timeout=1)
    assert claimed is not None
    assert claimed["claimed_at"] == current_time[0].isoformat()
    assert claimed["lease_expires_at"] == (current_time[0] + timedelta(seconds=10)).isoformat()
    assert claimed["lease_expires_at"] > current_time[0].isoformat()


@pytest.mark.asyncio
async def test_policy_revocation_prevents_claiming_an_already_queued_job(
    tmp_path: Path,
) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="device"))
    agent = await state.register_agent(
        AgentCreate(
            name="reader",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    board = MessageBoardService(state.db_path)
    allowed = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    dispatcher = AgentDispatcher(state.db_path, board, permission_policy=allowed)
    job = await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    revoked_dispatcher = AgentDispatcher(
        state.db_path,
        board,
        permission_policy=denied_worker_policy("workspace.list_dir"),
    )
    assert await revoked_dispatcher.install_worker_skill_policy(
        denied_worker_policy("workspace.list_dir")
    )

    assert await revoked_dispatcher.claim(agent["id"]) is None
    persisted = await revoked_dispatcher.get_job(job["id"])
    assert persisted is not None and persisted["status"] == "quarantined"
    assert persisted["completed_at"] is not None
    assert persisted["last_failure_reason"] == "remote worker skill revoked by current policy"
    failed_task = await state.get_task(task.id)
    assert failed_task is not None and failed_task.status.value == "failed"
    with sqlite3.connect(state.db_path) as db:
        event_types = [
            str(row[0])
            for row in db.execute(
                "SELECT event_type FROM audit_events WHERE task_id=?", (task.id,)
            ).fetchall()
        ]
    assert "agent.job.skill_revoked" in event_types
    assert "agent.job.quarantined" in event_types


@pytest.mark.asyncio
async def test_running_job_may_finish_under_policy_snapshot_from_claim(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="device"))
    agent = await state.register_agent(
        AgentCreate(
            name="reader",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    board = MessageBoardService(state.db_path)
    allowed = AgentDispatcher(
        state.db_path,
        board,
        permission_policy=PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml"),
    )
    await allowed.queue_job(task.id, "workspace.list_dir", {"path": "."})
    claimed = await allowed.claim(agent["id"])
    assert claimed is not None
    revoked = AgentDispatcher(
        state.db_path,
        board,
        permission_policy=denied_worker_policy("workspace.list_dir"),
    )
    assert await revoked.install_worker_skill_policy(denied_worker_policy("workspace.list_dir"))

    completed, changed = await revoked.submit_result(
        agent["id"],
        claimed["id"],
        claimed["claim_token"],
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
        status="completed",
        result={"entries": []},
        error=None,
    )

    assert changed is True
    assert completed["status"] == "completed"


@pytest.mark.asyncio
async def test_atomic_policy_reload_quarantines_queued_job_and_retains_last_valid_rules(
    tmp_path: Path,
) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="device"))
    agent = await state.register_agent(
        AgentCreate(
            name="reader",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    raw_policy = yaml.safe_load(
        (REPO_ROOT / "configs" / "permissions.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(raw_policy, dict)
    policy_path = tmp_path / "permissions.yaml"
    policy_path.write_text(yaml.safe_dump(raw_policy), encoding="utf-8")
    policy = PermissionPolicy.from_yaml(policy_path)
    dispatcher = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=policy,
    )
    job = await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    raw_policy["worker_skill_rules"]["workspace.list_dir"].update(
        decision="deny",
        auto_redistribute=False,
    )
    policy_path.write_text(yaml.safe_dump(raw_policy), encoding="utf-8")
    assert await dispatcher.reload_worker_skill_policy(policy_path) is True
    assert await dispatcher.claim(agent["id"]) is None
    persisted = await dispatcher.get_job(job["id"])
    assert persisted is not None and persisted["status"] == "quarantined"

    policy_path.write_text("worker_skill_rules: [invalid]", encoding="utf-8")
    with pytest.raises(PermissionPolicyError):
        await dispatcher.reload_worker_skill_policy(policy_path)
    assert policy.evaluate_worker_skill("workspace.list_dir").decision == "deny"


@pytest.mark.asyncio
async def test_revoked_backlog_is_quarantined_in_writer_friendly_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    total_jobs = 40
    now = "2026-09-08T20:00:00+00:00"
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            """
            INSERT INTO tasks(
                id,title,input,mode,source,status,priority,created_at,updated_at
            ) VALUES(?,?,?,?,?,'queued',0,?,?)
            """,
            [
                (
                    f"task-revoked-{index:03d}",
                    "revoked backlog",
                    "read",
                    "normal",
                    "device",
                    now,
                    now,
                )
                for index in range(total_jobs)
            ],
        )
        await db.executemany(
            """
            INSERT INTO agent_jobs(
                id,task_id,required_skill,payload_json,status,max_attempts,
                created_at,updated_at
            ) VALUES(?,?,?,'{}','queued',3,?,?)
            """,
            [
                (
                    f"job-revoked-{index:03d}",
                    f"task-revoked-{index:03d}",
                    "workspace.list_dir",
                    now,
                    now,
                )
                for index in range(total_jobs)
            ],
        )
        await db.commit()

    dispatcher = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=denied_worker_policy("workspace.list_dir"),
    )
    dispatcher.QUARANTINE_BATCH_SIZE = 8
    first_batch_committed = asyncio.Event()
    allow_remaining_batches = asyncio.Event()
    yield_count = 0
    original_yield = dispatcher._yield_quarantine_batch

    async def pause_after_first_batch() -> None:
        nonlocal yield_count
        yield_count += 1
        if yield_count == 1:
            first_batch_committed.set()
            await allow_remaining_batches.wait()
        await original_yield()

    monkeypatch.setattr(dispatcher, "_yield_quarantine_batch", pause_after_first_batch)
    running = asyncio.create_task(dispatcher.quarantine_revoked_jobs())
    await asyncio.wait_for(first_batch_committed.wait(), timeout=1)

    async with aiosqlite.connect(state.db_path) as db:
        quarantined = await (
            await db.execute("SELECT COUNT(*) FROM agent_jobs WHERE status='quarantined'")
        ).fetchone()
        assert quarantined == (8,)
        # A separate authoritative writer can progress between quarantine
        # batches instead of waiting for the entire revoked backlog.
        writer = await db.execute(
            """
            UPDATE tasks SET priority=1,updated_at=?
            WHERE id='task-revoked-039' AND status='queued'
            """,
            (now,),
        )
        assert writer.rowcount == 1
        await db.commit()

    allow_remaining_batches.set()
    assert await asyncio.wait_for(running, timeout=3) == total_jobs
    async with aiosqlite.connect(state.db_path) as db:
        status_counts = await (
            await db.execute(
                "SELECT status,COUNT(*) FROM agent_jobs GROUP BY status ORDER BY status"
            )
        ).fetchall()
        audit_counts = await (
            await db.execute(
                """
                SELECT event_type,COUNT(*) FROM audit_events
                WHERE event_type IN ('agent.job.skill_revoked','agent.job.quarantined')
                GROUP BY event_type ORDER BY event_type
                """
            )
        ).fetchall()
    assert [tuple(row) for row in status_counts] == [("quarantined", total_jobs)]
    assert [tuple(row) for row in audit_counts] == [
        ("agent.job.quarantined", total_jobs),
        ("agent.job.skill_revoked", total_jobs),
    ]


@pytest.mark.asyncio
async def test_claim_inspects_only_a_bounded_candidate_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    agent = await state.register_agent(
        AgentCreate(
            name="bounded-reader",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    now = "2026-09-08T20:00:00+00:00"
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            """
            INSERT INTO tasks(
                id,title,input,mode,source,status,priority,created_at,updated_at
            ) VALUES(?,?,?,?,?,'queued',0,?,?)
            """,
            [
                (
                    f"task-candidate-{index:03d}",
                    "candidate",
                    "read",
                    "normal",
                    "device",
                    now,
                    now,
                )
                for index in range(12)
            ],
        )
        await db.executemany(
            """
            INSERT INTO agent_jobs(
                id,task_id,required_skill,payload_json,status,max_attempts,
                created_at,updated_at
            ) VALUES(?,?,?,'{}','queued',3,?,?)
            """,
            [
                (
                    f"job-candidate-{index:03d}",
                    f"task-candidate-{index:03d}",
                    "workspace.list_dir",
                    now,
                    now,
                )
                for index in range(12)
            ],
        )
        await db.commit()

    dispatcher = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml"),
    )
    dispatcher.CLAIM_CANDIDATE_LIMIT = 5
    inspected: list[str] = []

    async def no_selection(
        db: aiosqlite.Connection,
        job_id: str,
        *,
        now: object = None,
    ) -> None:
        del db, now
        inspected.append(job_id)

    monkeypatch.setattr(dispatcher.scheduler, "select_for_job_locked", no_selection)

    assert await dispatcher.claim(agent["id"]) is None
    assert inspected == [f"job-candidate-{index:03d}" for index in range(5)]


@pytest.mark.asyncio
async def test_claim_candidate_window_is_bounded_per_skill_without_cross_skill_starvation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    agent = await state.register_agent(
        AgentCreate(
            name="multi-skill-reader",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.list_dir", "workspace.read_text"],
        ),
        "device",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    now = "2026-09-08T20:00:00+00:00"
    list_job_count = AgentDispatcher.CLAIM_CANDIDATE_LIMIT + 1
    task_rows = [
        (
            f"task-list-starvation-{index:03d}",
            "list candidate",
            "list",
            "normal",
            "device",
            10,
            now,
            now,
        )
        for index in range(list_job_count)
    ]
    task_rows.append(
        (
            "task-read-not-starved",
            "read candidate",
            "read",
            "normal",
            "device",
            0,
            now,
            now,
        )
    )
    job_rows = [
        (
            f"job-list-starvation-{index:03d}",
            f"task-list-starvation-{index:03d}",
            "workspace.list_dir",
            now,
            now,
        )
        for index in range(list_job_count)
    ]
    job_rows.append(
        (
            "job-read-not-starved",
            "task-read-not-starved",
            "workspace.read_text",
            now,
            now,
        )
    )
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            """
            INSERT INTO tasks(
                id,title,input,mode,source,status,priority,created_at,updated_at
            ) VALUES(?,?,?,?,?,'queued',?,?,?)
            """,
            task_rows,
        )
        await db.executemany(
            """
            INSERT INTO agent_jobs(
                id,task_id,required_skill,payload_json,status,max_attempts,
                created_at,updated_at
            ) VALUES(?,?,?,'{}','queued',3,?,?)
            """,
            job_rows,
        )
        await db.commit()

    dispatcher = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml"),
    )
    inspected: list[str] = []

    async def select_only_read_job(
        db: aiosqlite.Connection,
        job_id: str,
        *,
        now: object = None,
    ) -> SchedulerSelection:
        del now
        inspected.append(job_id)
        required_skill = await (
            await db.execute("SELECT required_skill FROM agent_jobs WHERE id=?", (job_id,))
        ).fetchone()
        selected_agent_id = (
            agent["id"]
            if required_skill is not None and str(required_skill[0]) == "workspace.read_text"
            else "agt_better-list-worker"
        )
        return SchedulerSelection(
            selected_agent_id=selected_agent_id,
            candidates=({"agent_id": selected_agent_id},),
            scoring={"algorithm": "test-deterministic"},
        )

    monkeypatch.setattr(dispatcher.scheduler, "select_for_job_locked", select_only_read_job)

    claimed = await dispatcher.claim(agent["id"])

    assert claimed is not None
    assert claimed["id"] == "job-read-not-starved"
    assert inspected[-1] == "job-read-not-starved"
    assert len([job_id for job_id in inspected if job_id.startswith("job-list-")]) == 64
