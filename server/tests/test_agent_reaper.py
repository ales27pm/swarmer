from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.agent_lease_reaper import AgentLeaseReaper
from app.services.message_board import MessageBoardService
from app.services.state_service import StateService
from tests.test_agent_leases import MutableClock, setup_runtime


@pytest.mark.asyncio
async def test_cancelled_parent_prevents_expired_lease_requeue(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, reaper, agent, task_id = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    with sqlite3.connect(state.db_path) as db:
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
    clock.advance(61)

    counts = await reaper.reap_expired()

    assert counts == {"expired": 1, "requeued": 0, "dead_lettered": 0, "cancelled": 1}
    assert (await dispatcher.get_job(claimed["id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_retry_budget_dead_letters_read_only_job(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    _, dispatcher, reaper, agent, task_id = await setup_runtime(
        tmp_path / "state.db", clock, max_attempts=1
    )
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    clock.advance(61)

    counts = await reaper.reap_expired()

    assert counts["dead_lettered"] == 1
    assert (await dispatcher.get_job(claimed["id"]))["status"] == "failed"
    task = await _.get_task(task_id)
    assert task is not None and task.status.value == "failed"


@pytest.mark.asyncio
async def test_restart_preserves_remote_job_for_lease_recovery(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, reaper, agent, task_id = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    await state.initialize()
    task = await state.get_task(task_id)
    assert task is not None and task.status.value == "running"

    clock.advance(61)
    assert (await reaper.reap_expired())["requeued"] == 1


@pytest.mark.asyncio
async def test_result_after_expiry_before_reap_is_rejected(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    _, dispatcher, _, agent, _ = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    clock.advance(61)

    with pytest.raises(AgentDispatchConflict, match="expired"):
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


@pytest.mark.asyncio
async def test_expired_mutating_job_is_never_automatically_redistributed(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="write"), source="phone"))
    agent = await state.register_agent(
        AgentCreate(
            name="writer",
            endpoint="http://127.0.0.1:9001",
            skills=["workspace.write_text"],
        ),
        "phone",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    board = MessageBoardService(database)
    dispatcher = AgentDispatcher(database, board, lease_seconds=60, clock=clock)
    reaper = AgentLeaseReaper(database, board, clock=clock)
    await dispatcher.queue_job(task.id, "workspace.write_text", {"path": "x", "content": "y"})
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    clock.advance(61)

    counts = await reaper.reap_expired()

    assert counts["dead_lettered"] == 1
    assert counts["requeued"] == 0
    assert (await dispatcher.get_job(claimed["id"]))["status"] == "failed"


@pytest.mark.parametrize(
    ("capability_status", "expected_request_status"),
    [("waiting_approval", "cancelled"), ("consumed", "cancelled"), ("completed", "completed")],
)
@pytest.mark.asyncio
async def test_capability_history_makes_expired_read_job_outcome_uncertain(
    tmp_path: Path,
    capability_status: str,
    expected_request_status: str,
) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, reaper, agent, task_id = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    request_id = f"request-{capability_status}"
    now = clock().isoformat()
    with sqlite3.connect(state.db_path) as db:
        db.execute(
            "INSERT INTO devices(id,name,token,created_at) VALUES(?,?,?,?)",
            ("phone", "Phone", "sha256:" + "a" * 64, now),
        )
        db.execute(
            """
            INSERT INTO iphone_capability_requests(
                id,task_id,requesting_agent_id,requesting_job_id,lease_generation,
                device_id,capability_name,arguments_json,request_fingerprint,
                action_digest,status,approval_id,request_audit_id,created_at,
                expires_at,delivered_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                request_id,
                task_id,
                agent["id"],
                claimed["id"],
                claimed["lease_generation"],
                "phone",
                "iphone.location.current",
                "{}",
                f"fingerprint-{capability_status}",
                "sha256:" + "b" * 64,
                capability_status,
                f"approval-{capability_status}",
                1,
                now,
                "2099-01-01T00:00:00+00:00",
                now if capability_status in {"consumed", "completed"} else None,
                now if capability_status == "completed" else None,
            ),
        )
    clock.advance(61)

    counts = await reaper.reap_expired()

    assert counts == {"expired": 1, "requeued": 0, "dead_lettered": 1, "cancelled": 0}
    job = await dispatcher.get_job(claimed["id"])
    assert job is not None
    assert job["status"] == "failed"
    assert "outcome uncertain; not retried" in job["last_failure_reason"]
    assert "outcome uncertain; not retried" in job["error"]
    task = await state.get_task(task_id)
    assert task is not None and task.status.value == "failed"
    assert task.error_json is not None
    assert "outcome uncertain; not retried" in task.error_json["message"]
    with sqlite3.connect(state.db_path) as db:
        request_status = db.execute(
            "SELECT status FROM iphone_capability_requests WHERE id=?", (request_id,)
        ).fetchone()
    assert request_status == (expected_request_status,)
    assert await dispatcher.claim(agent["id"]) is None
