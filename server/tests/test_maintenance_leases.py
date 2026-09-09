from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.services.control_plane_instance import ControlPlaneInstanceService
from app.services.maintenance_lease import (
    MaintenanceLeaseConflict,
    MaintenanceLeaseGuard,
    MaintenanceLeaseLost,
    MaintenanceLeaseRunner,
    MaintenanceLeaseService,
)
from app.services.state_service import StateService


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


async def start_instance(
    database: Path, instance_id: str, clock: MutableClock
) -> ControlPlaneInstanceService:
    service = ControlPlaneInstanceService(
        database,
        version="0.10.0",
        instance_id=instance_id,
        hostname="ubuntu-test",
        clock=clock,
    )
    await service.start()
    return service


@pytest.mark.asyncio
async def test_one_owner_acquires_and_renews_without_changing_generation(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner_a", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=30, clock=clock)

    first = await leases.acquire("agent-lease-reaper", "cp_owner_a")
    assert first is not None and first.generation == 1
    clock.advance(10)
    renewed = await leases.acquire("agent-lease-reaper", "cp_owner_a")

    assert renewed is not None
    assert renewed.generation == first.generation
    assert renewed.acquired_at == first.acquired_at
    assert renewed.renewed_at == clock().isoformat()
    assert renewed.expires_at > first.expires_at
    assert await leases.is_current(renewed.name, renewed.owner_instance_id, renewed.generation)


@pytest.mark.asyncio
async def test_unexpired_lease_excludes_other_control_plane_instance(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner_a", clock)
    await start_instance(database, "cp_owner_b", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=30, clock=clock)

    first = await leases.acquire("capability-expirer", "cp_owner_a")
    blocked = await leases.acquire("capability-expirer", "cp_owner_b")

    assert first is not None
    assert blocked is None
    assert (await leases.get("capability-expirer")) == first


@pytest.mark.asyncio
async def test_expired_takeover_increments_generation_and_fences_old_owner(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner_a", clock)
    await start_instance(database, "cp_owner_b", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=30, clock=clock)
    old = await leases.acquire("outbox-maintenance", "cp_owner_a")
    assert old is not None

    clock.advance(31)
    current = await leases.acquire("outbox-maintenance", "cp_owner_b")

    assert current is not None
    assert current.owner_instance_id == "cp_owner_b"
    assert current.generation == old.generation + 1
    assert await leases.renew(old.name, old.owner_instance_id, old.generation) is None
    assert not await leases.release(old.name, old.owner_instance_id, old.generation)
    assert not await leases.is_current(old.name, old.owner_instance_id, old.generation)
    with pytest.raises(MaintenanceLeaseConflict, match="stale"):
        await leases.assert_current(old.name, old.owner_instance_id, old.generation)
    assert await leases.is_current(current.name, current.owner_instance_id, current.generation)


@pytest.mark.asyncio
async def test_expired_same_owner_reacquisition_also_advances_fence(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=10, clock=clock)
    old = await leases.acquire("feedback-maintenance", "cp_owner")
    assert old is not None
    clock.advance(11)

    current = await leases.acquire("feedback-maintenance", "cp_owner")

    assert current is not None and current.generation == old.generation + 1


@pytest.mark.asyncio
async def test_release_expires_lease_and_next_owner_gets_new_generation(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner_a", clock)
    await start_instance(database, "cp_owner_b", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=30, clock=clock)
    first = await leases.acquire("agent-lease-reaper", "cp_owner_a")
    assert first is not None

    assert await leases.release(first.name, first.owner_instance_id, first.generation)
    assert not await leases.is_current(first.name, first.owner_instance_id, first.generation)
    second = await leases.acquire(first.name, "cp_owner_b")

    assert second is not None and second.generation == first.generation + 1


@pytest.mark.asyncio
async def test_stopped_or_unregistered_instance_cannot_own_or_renew(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    instance = await start_instance(database, "cp_owner", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=30, clock=clock)
    current = await leases.acquire("outbox-maintenance", instance.instance_id)
    assert current is not None
    await instance.stop()

    assert await leases.renew(current.name, current.owner_instance_id, current.generation) is None
    assert not await leases.is_current(current.name, current.owner_instance_id, current.generation)
    with pytest.raises(MaintenanceLeaseConflict, match="not an active instance"):
        await leases.acquire("capability-expirer", "cp_missing")


@pytest.mark.asyncio
async def test_concurrent_instances_have_exactly_one_lease_winner(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    owners = [f"cp_contender_{index}" for index in range(10)]
    for owner in owners:
        await start_instance(database, owner, clock)
    leases = MaintenanceLeaseService(database, lease_seconds=30, clock=clock)

    results = await asyncio.gather(
        *(leases.acquire("agent-lease-reaper", owner) for owner in owners)
    )

    winners = [lease for lease in results if lease is not None]
    assert len(winners) == 1
    persisted = await leases.get("agent-lease-reaper")
    assert persisted == winners[0]
    assert persisted is not None and persisted.generation == 1


@pytest.mark.asyncio
async def test_fence_can_be_checked_inside_authoritative_mutation_transaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner_a", clock)
    await start_instance(database, "cp_owner_b", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=10, clock=clock)
    old = await leases.acquire("capability-expirer", "cp_owner_a")
    assert old is not None
    clock.advance(11)
    current = await leases.acquire("capability-expirer", "cp_owner_b")
    assert current is not None

    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(MaintenanceLeaseConflict, match="stale"):
            await leases.require_current_locked(db, old.name, old.owner_instance_id, old.generation)
        await leases.require_current_locked(
            db, current.name, current.owner_instance_id, current.generation
        )
        await db.commit()


@pytest.mark.asyncio
async def test_restart_preserves_maintenance_owner_and_fence(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    state = StateService(database)
    await state.initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_preserved", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=30, clock=clock)
    before = await leases.acquire("outbox-maintenance", "cp_preserved")
    assert before is not None

    await state.initialize()

    assert await leases.get("outbox-maintenance") == before
    assert await leases.is_current(before.name, before.owner_instance_id, before.generation)


def test_maintenance_names_and_duration_are_bounded(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        MaintenanceLeaseService(tmp_path / "state.db", lease_seconds=0)
    leases = MaintenanceLeaseService(tmp_path / "state.db")
    with pytest.raises(ValueError, match="invalid format"):
        leases._validate_name("../../reaper")


@pytest.mark.asyncio
async def test_runner_fences_next_mutation_after_lease_is_lost_mid_operation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner_a", clock)
    await start_instance(database, "cp_owner_b", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=10, clock=clock)
    runner = MaintenanceLeaseRunner(
        leases,
        owner_instance_id="cp_owner_a",
        renewal_interval_seconds=0.01,
    )
    first_mutation_done = asyncio.Event()
    allow_second_mutation = asyncio.Event()
    mutations: list[str] = []

    async def long_operation(guard: MaintenanceLeaseGuard) -> None:
        async with aiosqlite.connect(database) as db:
            await db.execute("BEGIN IMMEDIATE")
            await guard.require_current_locked(db)
            mutations.append("generation-a")
            await db.commit()
        first_mutation_done.set()
        await allow_second_mutation.wait()
        async with aiosqlite.connect(database) as db:
            await db.execute("BEGIN IMMEDIATE")
            await guard.require_current_locked(db)
            mutations.append("stale-generation-a")
            await db.commit()

    operation = asyncio.create_task(
        runner.run("agent-lease-reaper", long_operation),
        name="maintenance-operation-under-test",
    )
    await asyncio.wait_for(first_mutation_done.wait(), timeout=1)
    clock.advance(11)
    takeover = await leases.acquire("agent-lease-reaper", "cp_owner_b")
    assert takeover is not None and takeover.generation == 2
    allow_second_mutation.set()

    with pytest.raises(MaintenanceLeaseLost):
        await asyncio.wait_for(operation, timeout=1)
    assert mutations == ["generation-a"]


@pytest.mark.asyncio
async def test_runner_renews_before_half_ttl_and_exposes_stable_generation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=10, clock=clock)
    runner = MaintenanceLeaseRunner(
        leases,
        owner_instance_id="cp_owner",
        renewal_interval_seconds=0.01,
    )
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def operation(guard: MaintenanceLeaseGuard) -> int:
        entered.set()
        await finish.wait()
        return guard.generation

    running = asyncio.create_task(runner.run("feedback-maintenance", operation))
    await asyncio.wait_for(entered.wait(), timeout=1)
    initial = await leases.get("feedback-maintenance")
    assert initial is not None
    clock.advance(4)
    for _ in range(50):
        renewed = await leases.get("feedback-maintenance")
        if renewed is not None and renewed.renewed_at == clock().isoformat():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("maintenance lease was not renewed")

    assert renewed.generation == initial.generation == 1
    assert renewed.expires_at == (clock() + timedelta(seconds=10)).isoformat()
    finish.set()
    assert await asyncio.wait_for(running, timeout=1) == 1
    assert not await leases.is_current("feedback-maintenance", "cp_owner", 1)


@pytest.mark.asyncio
async def test_runner_cancels_operation_when_delayed_renewal_misses_deadline(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=10, clock=clock)
    runner = MaintenanceLeaseRunner(
        leases,
        owner_instance_id="cp_owner",
        renewal_interval_seconds=0.01,
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def operation(guard: MaintenanceLeaseGuard) -> None:
        del guard
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    running = asyncio.create_task(runner.run("feedback-maintenance", operation))
    await asyncio.wait_for(entered.wait(), timeout=1)
    clock.advance(11)

    with pytest.raises(MaintenanceLeaseLost):
        await asyncio.wait_for(running, timeout=1)
    assert cancelled.is_set()
    assert runner.metrics == {"maintenance_lease_renewal_failures": 1}


@pytest.mark.asyncio
async def test_external_cancellation_cancels_callback_and_releases_live_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=10, clock=clock)
    runner = MaintenanceLeaseRunner(
        leases,
        owner_instance_id="cp_owner",
        renewal_interval_seconds=0.01,
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def operation(guard: MaintenanceLeaseGuard) -> None:
        del guard
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    running = asyncio.create_task(runner.run("outbox-maintenance", operation))
    await asyncio.wait_for(entered.wait(), timeout=1)
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert cancelled.is_set()
    assert not await leases.is_current("outbox-maintenance", "cp_owner", 1)


@pytest.mark.asyncio
async def test_runner_clears_active_registry_when_release_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=10, clock=clock)
    runner = MaintenanceLeaseRunner(
        leases,
        owner_instance_id="cp_owner",
        renewal_interval_seconds=0.01,
    )
    original_release = leases.release
    release_attempts = 0

    async def fail_first_release(name: str, owner_instance_id: str, generation: int) -> bool:
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            raise OSError("simulated release failure")
        return await original_release(name, owner_instance_id, generation)

    monkeypatch.setattr(leases, "release", fail_first_release)

    async def operation(guard: MaintenanceLeaseGuard) -> int:
        return guard.generation

    with pytest.raises(OSError, match="simulated release failure"):
        await runner.run("outbox-maintenance", operation)

    # Release failure must not strand the runner's process-local registry. The
    # durable lease remains safe: the same owner renews it and then releases it.
    assert await runner.run("outbox-maintenance", operation) == 1
    assert release_attempts == 2


@pytest.mark.asyncio
async def test_runner_shutdown_waits_for_cleanup_and_rejects_new_work(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner", clock)
    leases = MaintenanceLeaseService(database, lease_seconds=10, clock=clock)
    runner = MaintenanceLeaseRunner(
        leases,
        owner_instance_id="cp_owner",
        renewal_interval_seconds=0.01,
    )
    entered = asyncio.Event()

    async def operation(guard: MaintenanceLeaseGuard) -> None:
        del guard
        entered.set()
        await asyncio.Event().wait()

    running = asyncio.create_task(runner.run("capability-expirer", operation))
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.wait_for(runner.aclose(), timeout=1)

    with pytest.raises(asyncio.CancelledError):
        await running
    assert not await leases.is_current("capability-expirer", "cp_owner", 1)
    with pytest.raises(RuntimeError, match="closed"):
        await runner.run("capability-expirer", operation)


def test_runner_rejects_renewal_interval_at_or_after_half_ttl(tmp_path: Path) -> None:
    leases = MaintenanceLeaseService(tmp_path / "state.db", lease_seconds=10)
    with pytest.raises(ValueError, match="before half"):
        MaintenanceLeaseRunner(
            leases,
            owner_instance_id="cp_owner",
            renewal_interval_seconds=5,
        )


@pytest.mark.asyncio
async def test_guarded_mutation_rolls_back_when_work_itself_exceeds_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    await start_instance(database, "cp_owner", clock)
    async with aiosqlite.connect(database) as db:
        await db.execute("CREATE TABLE guarded_effects(value TEXT NOT NULL)")
        await db.commit()
    leases = MaintenanceLeaseService(database, lease_seconds=10, clock=clock)
    runner = MaintenanceLeaseRunner(
        leases,
        owner_instance_id="cp_owner",
        renewal_interval_seconds=0.01,
    )

    async def operation(guard: MaintenanceLeaseGuard) -> None:
        async def mutation(db: aiosqlite.Connection) -> None:
            await db.execute("INSERT INTO guarded_effects(value) VALUES('must-rollback')")
            clock.advance(11)

        await guard.run_locked(mutation)

    with pytest.raises(MaintenanceLeaseLost):
        await runner.run("feedback-maintenance", operation)
    async with aiosqlite.connect(database) as db:
        count = await (await db.execute("SELECT COUNT(*) FROM guarded_effects")).fetchone()
    assert count == (0,)
