from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services import agent_lease_reaper as reaper_module
from app.services import iphone_capability_service as capability_module
from app.services import outbox as outbox_module
from app.services.agent_lease_reaper import AgentLeaseReaper
from app.services.control_plane_instance import ControlPlaneInstanceService
from app.services.iphone_capability_service import IPhoneCapabilityService
from app.services.maintenance_lease import (
    MaintenanceLeaseGuard,
    MaintenanceLeaseRunner,
    MaintenanceLeaseService,
)
from app.services.message_board import MessageBoardService
from app.services.outbox import OutboxService
from app.services.permission_policy import PermissionPolicy
from app.services.state_service import StateService

REPO_ROOT = Path(__file__).resolve().parents[2]


async def _run_past_one_lease_ttl[ResultT](
    database: Path,
    name: str,
    operation: Callable[[MaintenanceLeaseGuard], Awaitable[ResultT]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ResultT, int, float]:
    instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id=f"cp-batched-{name}",
        hostname="ubuntu-test",
    )
    await instance.start()
    leases = MaintenanceLeaseService(database, lease_seconds=2)
    renew_calls = 0
    original_renew = leases.renew

    async def counting_renew(
        lease_name: str,
        owner_instance_id: str,
        generation: int,
    ):
        nonlocal renew_calls
        renew_calls += 1
        return await original_renew(lease_name, owner_instance_id, generation)

    monkeypatch.setattr(leases, "renew", counting_renew)
    runner = MaintenanceLeaseRunner(
        leases,
        owner_instance_id=instance.instance_id,
        renewal_interval_seconds=0.3,
    )
    started = time.monotonic()
    result = await runner.run(name, operation)
    elapsed = time.monotonic() - started
    assert result is not None
    assert elapsed > 2
    assert renew_calls >= 2
    assert runner.metrics["maintenance_lease_renewal_failures"] == 0
    return result, renew_calls, elapsed


@pytest.mark.asyncio
async def test_maintenance_lease_timestamps_start_after_writer_lock_wait(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    initial = datetime(2026, 1, 1, tzinfo=UTC)

    class MutableClock:
        def __init__(self) -> None:
            self.value = initial

        def __call__(self) -> datetime:
            return self.value

    clock = MutableClock()
    instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp-lock-wait",
        hostname="ubuntu-test",
        clock=clock,
    )
    await instance.start()
    leases = MaintenanceLeaseService(database, lease_seconds=30, clock=clock)

    async with aiosqlite.connect(database) as blocker:
        await blocker.execute("BEGIN IMMEDIATE")
        acquiring = asyncio.create_task(leases.acquire("outbox-maintenance", instance.instance_id))
        await asyncio.sleep(0.05)
        clock.value += timedelta(seconds=20)
        await blocker.commit()
    acquired = await acquiring
    assert acquired is not None
    assert acquired.acquired_at == clock.value.isoformat()
    assert acquired.expires_at == (clock.value + timedelta(seconds=30)).isoformat()

    async with aiosqlite.connect(database) as blocker:
        await blocker.execute("BEGIN IMMEDIATE")
        renewing = asyncio.create_task(
            leases.renew(acquired.name, acquired.owner_instance_id, acquired.generation)
        )
        await asyncio.sleep(0.05)
        clock.value += timedelta(seconds=20)
        await blocker.commit()
    renewed = await renewing
    assert renewed is not None
    assert renewed.renewed_at == clock.value.isoformat()
    assert renewed.expires_at == (clock.value + timedelta(seconds=30)).isoformat()


@pytest.mark.asyncio
async def test_acquire_returns_its_exact_generation_after_post_commit_takeover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    current = [datetime(2026, 1, 1, tzinfo=UTC)]

    def clock() -> datetime:
        return current[0]

    first_instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp-acquire-first",
        hostname="ubuntu-a",
        clock=clock,
    )
    second_instance = ControlPlaneInstanceService(
        database,
        version="0.11.0",
        instance_id="cp-acquire-second",
        hostname="ubuntu-b",
        clock=clock,
    )
    await first_instance.start()
    await second_instance.start()
    leases = MaintenanceLeaseService(database, lease_seconds=10, clock=clock)
    original_commit = aiosqlite.Connection.commit
    first_commit_finished = asyncio.Event()
    resume_first_acquirer = asyncio.Event()
    pause_next_commit = True

    async def pause_after_first_commit(connection: aiosqlite.Connection) -> None:
        nonlocal pause_next_commit
        await original_commit(connection)
        if pause_next_commit:
            pause_next_commit = False
            first_commit_finished.set()
            await resume_first_acquirer.wait()

    monkeypatch.setattr(aiosqlite.Connection, "commit", pause_after_first_commit)
    first_acquire = asyncio.create_task(
        leases.acquire("outbox-maintenance", first_instance.instance_id)
    )
    await asyncio.wait_for(first_commit_finished.wait(), timeout=1)
    current[0] += timedelta(seconds=11)
    takeover = await leases.acquire("outbox-maintenance", second_instance.instance_id)
    resume_first_acquirer.set()
    acquired = await asyncio.wait_for(first_acquire, timeout=1)

    assert acquired is not None
    assert acquired.owner_instance_id == first_instance.instance_id
    assert acquired.generation == 1
    assert takeover is not None
    assert takeover.owner_instance_id == second_instance.instance_id
    assert takeover.generation == 2
    assert not await leases.is_current(
        acquired.name,
        acquired.owner_instance_id,
        acquired.generation,
    )


@pytest.mark.asyncio
async def test_outbox_default_claim_timestamp_starts_after_writer_lock_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    outbox = OutboxService(
        database,
        MessageBoardService(database),
        instance_id="claim-after-lock",
        publication_lease_seconds=5,
    )
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="task",
            aggregate_id="task-after-lock",
            topic="tasks.status",
            event_type="published",
            payload={"task_id": "task-after-lock", "status": "queued"},
            dedupe_key="task-after-lock:queued",
            task_id="task-after-lock",
        )
        await db.commit()

    current = datetime(2026, 1, 1, tzinfo=UTC)
    observed_times: list[datetime] = []

    def controlled_utc(value: datetime | None = None) -> datetime:
        selected = OutboxService._utc(value) if value is not None else current
        observed_times.append(selected)
        return selected

    monkeypatch.setattr(outbox, "_utc", controlled_utc)
    async with aiosqlite.connect(database) as blocker:
        await blocker.execute("BEGIN IMMEDIATE")
        claiming = asyncio.create_task(outbox.claim_batch(limit=1))
        await asyncio.sleep(0.05)
        assert not claiming.done()
        current += timedelta(seconds=20)
        await blocker.commit()
    claimed = await claiming

    assert len(claimed) == 1
    assert observed_times == [current]
    assert claimed[0]["publishing_started_at"] == current.isoformat()
    assert claimed[0]["publishing_lease_expires_at"] == (current + timedelta(seconds=5)).isoformat()


@pytest.mark.asyncio
async def test_outbox_default_mark_time_cannot_ignore_expiry_during_lock_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    outbox = OutboxService(
        database,
        MessageBoardService(database),
        instance_id="mark-after-lock",
        publication_lease_seconds=5,
    )
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="task",
            aggregate_id="task-mark-after-lock",
            topic="tasks.status",
            event_type="published",
            payload={"task_id": "task-mark-after-lock", "status": "queued"},
            dedupe_key="task-mark-after-lock:queued",
            task_id="task-mark-after-lock",
        )
        await db.commit()
    lease_started = datetime(2026, 1, 1, tzinfo=UTC)
    claimed = (await outbox.claim_batch(limit=1, now=lease_started))[0]
    current = lease_started + timedelta(seconds=1)
    observed_times: list[datetime] = []

    def controlled_utc(value: datetime | None = None) -> datetime:
        selected = OutboxService._utc(value) if value is not None else current
        observed_times.append(selected)
        return selected

    monkeypatch.setattr(outbox, "_utc", controlled_utc)
    async with aiosqlite.connect(database) as blocker:
        await blocker.execute("BEGIN IMMEDIATE")
        marking = asyncio.create_task(
            outbox.mark_published(
                int(claimed["id"]),
                owner_instance_id=outbox.instance_id,
                publish_generation=int(claimed["publish_generation"]),
            )
        )
        await asyncio.sleep(0.05)
        assert not marking.done()
        current = lease_started + timedelta(seconds=6)
        await blocker.commit()

    assert await marking is False
    assert observed_times == [current]
    async with aiosqlite.connect(database) as db:
        row = await (
            await db.execute(
                "SELECT published_at,publishing_owner FROM outbox_events WHERE id=?",
                (int(claimed["id"]),),
            )
        ).fetchone()
    assert row == (None, outbox.instance_id)


async def _seed_expired_outbox_claims(database: Path, count: int) -> OutboxService:
    board = MessageBoardService(database)
    outbox = OutboxService(
        database,
        board,
        instance_id="expired-publisher",
        publication_lease_seconds=5,
    )
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        for index in range(count):
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="task",
                aggregate_id=f"task-{index}",
                topic="tasks.status",
                event_type="published",
                payload={"task_id": f"task-{index}", "status": "queued"},
                dedupe_key=f"task-{index}:queued",
                task_id=f"task-{index}",
            )
        await db.commit()
    claimed = await outbox.claim_batch(
        limit=count,
        now=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert len(claimed) == count
    return outbox


@pytest.mark.asyncio
async def test_outbox_recovery_renews_during_slow_batched_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    outbox = await _seed_expired_outbox_claims(database, 120)
    original_record = outbox._record_claim_expired_locked

    async def slow_record(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0.018)
        await original_record(*args, **kwargs)

    monkeypatch.setattr(outbox, "_record_claim_expired_locked", slow_record)
    guard_checks = 0

    async def operation(guard: MaintenanceLeaseGuard) -> int:
        nonlocal guard_checks
        original_require = guard.require_current_locked

        async def counting_require(db: aiosqlite.Connection) -> None:
            nonlocal guard_checks
            guard_checks += 1
            await original_require(db)

        monkeypatch.setattr(guard, "require_current_locked", counting_require)
        return await outbox.recover_expired_claims(maintenance_guard=guard)

    recovered, _, _ = await _run_past_one_lease_ttl(
        database,
        "outbox-maintenance",
        operation,
        monkeypatch,
    )

    assert recovered == 120
    assert guard_checks >= 6
    assert guard_checks % 2 == 0
    assert await outbox.pending_count() == 120


@pytest.mark.asyncio
async def test_outbox_recovery_limits_each_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    outbox = await _seed_expired_outbox_claims(database, 5)
    monkeypatch.setattr(outbox_module, "_RECOVERY_BATCH_SIZE", 2)
    monkeypatch.setattr(outbox_module, "_MAX_RECOVERIES_PER_INVOCATION", 3)

    assert await outbox.recover_expired_claims() == 3
    assert await outbox.recover_expired_claims() == 2


async def _seed_expired_agent_jobs(database: Path, count: int) -> None:
    old = datetime(2000, 1, 1, tzinfo=UTC).isoformat()
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        for index in range(count):
            task_id = f"task-{index}"
            job_id = f"job-{index}"
            await db.execute(
                """
                INSERT INTO tasks(
                    id,title,input,mode,source,status,priority,created_at,updated_at
                ) VALUES(?,?,?,?,?,'running',0,?,?)
                """,
                (task_id, task_id, "read", "normal", "phone", old, old),
            )
            await db.execute(
                """
                INSERT INTO agent_jobs(
                    id,task_id,required_skill,payload_json,status,claimed_by,
                    lease_id,lease_token_hash,lease_expires_at,lease_generation,
                    created_at,updated_at,claimed_at,heartbeat_at,attempt_count,max_attempts
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    task_id,
                    "workspace.list_dir",
                    '{"path":"."}',
                    "running",
                    "agent-old",
                    f"lease-{index}",
                    f"sha256:{index:064x}",
                    old,
                    1,
                    old,
                    old,
                    old,
                    old,
                    1,
                    3,
                ),
            )
        await db.commit()


@pytest.mark.asyncio
async def test_agent_reaper_renews_during_slow_batched_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    await _seed_expired_agent_jobs(database, 24)
    reaper = AgentLeaseReaper(database, MessageBoardService(database))
    original_reap = reaper._reap_row_locked

    async def slow_reap(*args: Any, **kwargs: Any) -> str:
        await asyncio.sleep(0.09)
        return await original_reap(*args, **kwargs)

    monkeypatch.setattr(reaper, "_reap_row_locked", slow_reap)
    guard_checks = 0

    async def operation(guard: MaintenanceLeaseGuard) -> dict[str, int]:
        nonlocal guard_checks
        original_require = guard.require_current_locked

        async def counting_require(db: aiosqlite.Connection) -> None:
            nonlocal guard_checks
            guard_checks += 1
            await original_require(db)

        monkeypatch.setattr(guard, "require_current_locked", counting_require)
        return await reaper.reap_expired(maintenance_guard=guard)

    counts, _, _ = await _run_past_one_lease_ttl(
        database,
        "agent-lease-reaper",
        operation,
        monkeypatch,
    )

    assert counts == {
        "expired": 24,
        "requeued": 24,
        "dead_lettered": 0,
        "cancelled": 0,
        "quarantined": 0,
    }
    assert guard_checks >= 8


@pytest.mark.asyncio
async def test_agent_reaper_limits_each_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    await _seed_expired_agent_jobs(database, 5)
    reaper = AgentLeaseReaper(database, MessageBoardService(database))
    monkeypatch.setattr(reaper_module, "_REAP_BATCH_SIZE", 2)
    monkeypatch.setattr(reaper_module, "_MAX_REAPS_PER_INVOCATION", 3)

    assert (await reaper.reap_expired())["expired"] == 3
    assert (await reaper.reap_expired())["expired"] == 2


async def _seed_expired_capability_requests(database: Path, count: int) -> None:
    old = datetime(2000, 1, 1, tzinfo=UTC).isoformat()
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        for index in range(count):
            await db.execute(
                """
                INSERT INTO iphone_capability_requests(
                    id,task_id,requesting_agent_id,requesting_job_id,lease_generation,
                    device_id,capability_name,arguments_json,request_fingerprint,
                    action_digest,status,approval_id,request_audit_id,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"request-{index}",
                    f"task-{index}",
                    "agent-old",
                    f"job-{index}",
                    1,
                    "phone",
                    "iphone.location.current",
                    "{}",
                    f"fingerprint-{index}",
                    f"sha256:{index:064x}",
                    "waiting_approval",
                    f"approval-{index}",
                    index + 1,
                    old,
                    old,
                ),
            )
        await db.commit()


@pytest.mark.asyncio
async def test_capability_expirer_renews_during_slow_batched_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    await _seed_expired_capability_requests(database, 75)
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    service = IPhoneCapabilityService(database, MessageBoardService(database), policy)
    original_expire = service._expire_locked

    async def slow_expire(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0.03)
        await original_expire(*args, **kwargs)

    monkeypatch.setattr(service, "_expire_locked", slow_expire)
    guard_checks = 0

    async def operation(guard: MaintenanceLeaseGuard) -> int:
        nonlocal guard_checks
        original_require = guard.require_current_locked

        async def counting_require(db: aiosqlite.Connection) -> None:
            nonlocal guard_checks
            guard_checks += 1
            await original_require(db)

        monkeypatch.setattr(guard, "require_current_locked", counting_require)
        return await service.expire_requests(maintenance_guard=guard)

    expired, _, _ = await _run_past_one_lease_ttl(
        database,
        "capability-expirer",
        operation,
        monkeypatch,
    )

    assert expired == 75
    assert guard_checks >= 8
    async with aiosqlite.connect(database) as db:
        row = await (
            await db.execute(
                "SELECT COUNT(*) FROM iphone_capability_requests WHERE status='expired'"
            )
        ).fetchone()
    assert row == (75,)


@pytest.mark.asyncio
async def test_capability_expirer_limits_each_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    await _seed_expired_capability_requests(database, 5)
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    service = IPhoneCapabilityService(database, MessageBoardService(database), policy)
    monkeypatch.setattr(capability_module, "_EXPIRY_BATCH_SIZE", 2)
    monkeypatch.setattr(capability_module, "_MAX_EXPIRATIONS_PER_INVOCATION", 3)

    assert await service.expire_requests() == 3
    assert await service.expire_requests() == 2
