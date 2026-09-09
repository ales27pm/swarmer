from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.agent_lease_reaper import AgentLeaseReaper
from app.services.maintenance_lease import MaintenanceLeaseLost
from app.services.message_board import MessageBoardService
from app.services.permission_policy import PermissionPolicy
from app.services.state_service import StateService
from tests.test_agent_leases import MutableClock, setup_runtime

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
async def test_cancelled_parent_prevents_expired_lease_requeue(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, reaper, agent, task_id = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    with sqlite3.connect(state.db_path) as db:
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
    clock.advance(61)

    counts = await reaper.reap_expired()

    assert counts == {
        "expired": 1,
        "requeued": 0,
        "dead_lettered": 0,
        "cancelled": 1,
        "quarantined": 0,
    }
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
async def test_policy_revocation_prevents_expired_read_job_redistribution(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, _, agent, task_id = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    clock.advance(61)
    await dispatcher.install_worker_skill_policy(denied_worker_policy("workspace.list_dir"))
    revoked_reaper = AgentLeaseReaper(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=denied_worker_policy("workspace.list_dir"),
        clock=clock,
    )

    counts = await revoked_reaper.reap_expired()

    assert counts == {
        "expired": 1,
        "requeued": 0,
        "dead_lettered": 0,
        "cancelled": 0,
        "quarantined": 1,
    }
    job = await dispatcher.get_job(claimed["id"])
    assert job is not None and job["status"] == "quarantined"
    assert job["last_failure_reason"] == "remote worker skill revoked by current policy"
    task = await state.get_task(task_id)
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
async def test_reaper_status_metrics_are_persisted_without_audit_history_scan(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, reaper, agent, _ = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    clock.advance(61)
    assert (await reaper.reap_expired())["requeued"] == 1

    before = await reaper.metrics()
    assert before["expired_leases"] == 1
    assert before["retries"] == 1
    assert before["dead_letter_events"] == 0

    # Operational counters remain available even if audit retention changes;
    # /status must not depend on walking the append-only audit history.
    with sqlite3.connect(state.db_path) as db:
        db.execute("DELETE FROM audit_events")
    await state.initialize()

    after = await reaper.metrics()
    assert after["expired_leases"] == 1
    assert after["retries"] == 1
    assert after["dead_letter_events"] == 0

    # Prove the operational status query has no hidden dependency on the
    # historical table at all, rather than merely succeeding when it is empty.
    with sqlite3.connect(state.db_path) as db:
        db.execute("ALTER TABLE audit_events RENAME TO archived_audit_events")
    without_audit_table = await reaper.metrics()
    assert without_audit_table["expired_leases"] == 1
    assert without_audit_table["retries"] == 1


@pytest.mark.asyncio
async def test_reaper_metrics_roll_back_atomically_with_job_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state, dispatcher, reaper, agent, _ = await setup_runtime(tmp_path / "state.db", clock)
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    clock.advance(61)

    record_outcome = reaper._record_outcome_locked

    async def fail_after_recording(
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
        now: str,
        outcome: str,
        reason: str,
    ) -> None:
        await record_outcome(db, row, now, outcome, reason)
        raise RuntimeError("injected failure after operational counter update")

    monkeypatch.setattr(reaper, "_record_outcome_locked", fail_after_recording)
    with pytest.raises(RuntimeError, match="injected failure"):
        await reaper.reap_expired()

    persisted = await dispatcher.get_job(claimed["id"])
    assert persisted is not None and persisted["status"] == "claimed"
    with sqlite3.connect(state.db_path) as db:
        audit_count = db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type LIKE 'agent.job.%expired%'"
        ).fetchone()
        metrics = db.execute(
            """
            SELECT lease_expirations,retries,dead_letter_events
            FROM agent_job_operational_metrics WHERE singleton_id=1
            """
        ).fetchone()
    assert audit_count == (0,)
    assert metrics == (0, 0, 0)

    monkeypatch.setattr(reaper, "_record_outcome_locked", record_outcome)
    assert (await reaper.reap_expired())["requeued"] == 1
    assert (await reaper.metrics())["expired_leases"] == 1
    assert (await reaper.metrics())["retries"] == 1


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
    task = await state.create_task(TaskRecord.new(TaskCreate(input="research"), source="phone"))
    agent = await state.register_agent(
        AgentCreate(
            name="researcher",
            endpoint="http://127.0.0.1:9001",
            skills=["research.query"],
        ),
        "phone",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE agents SET last_seen_at=?,last_heartbeat_at=? WHERE id=?",
            (clock.value.isoformat(), clock.value.isoformat(), agent["id"]),
        )
    board = MessageBoardService(database)
    dispatcher = AgentDispatcher(database, board, lease_seconds=60, clock=clock)
    reaper = AgentLeaseReaper(database, board, clock=clock)
    await dispatcher.queue_job(task.id, "research.query", {"query": "bounded evidence"})
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    clock.advance(61)

    counts = await reaper.reap_expired()

    assert counts["dead_lettered"] == 1
    assert counts["requeued"] == 0
    job = await dispatcher.get_job(claimed["id"])
    assert job is not None and job["status"] == "failed"
    assert "automatic redistribution disabled by policy" in job["last_failure_reason"]
    assert "outcome uncertain; not retried" in job["error"]


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

    assert counts == {
        "expired": 1,
        "requeued": 0,
        "dead_lettered": 1,
        "cancelled": 0,
        "quarantined": 0,
    }
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


@pytest.mark.asyncio
async def test_reaper_does_not_swallow_maintenance_fence_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    reaper = AgentLeaseReaper(database, MessageBoardService(database))

    async def lose_fence(**_: object) -> dict[str, int]:
        raise MaintenanceLeaseLost("maintenance generation was fenced")

    monkeypatch.setattr(reaper.outbox, "drain", lose_fence)

    with pytest.raises(MaintenanceLeaseLost, match="fenced"):
        await reaper.reap_expired()
