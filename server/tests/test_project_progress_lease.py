from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.maintenance_lease import (
    MaintenanceLeaseGuard,
    MaintenanceLeaseLost,
    MaintenanceLeaseService,
)
from tests.test_goal_project_runtime import _project, _result
from tests.test_maintenance_leases import MutableClock, start_instance
from tests.test_project_stall_runtime import iteration


async def _projection_rows(database: Path, goal_id: str) -> dict[str, Any]:
    async with aiosqlite.connect(database) as db:
        return {
            "goal": await (
                await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_id,))
            ).fetchone(),
            "nodes": await (
                await db.execute(
                    "SELECT * FROM plan_nodes WHERE goal_run_id=? ORDER BY id", (goal_id,)
                )
            ).fetchall(),
            "messages": await (
                await db.execute(
                    "SELECT * FROM goal_messages WHERE goal_run_id=? ORDER BY id", (goal_id,)
                )
            ).fetchall(),
            "tasks": await (await db.execute("SELECT * FROM tasks ORDER BY id")).fetchall(),
            "jobs": await (await db.execute("SELECT * FROM agent_jobs ORDER BY id")).fetchall(),
            "calls": await (
                await db.execute("SELECT * FROM goal_model_calls ORDER BY id")
            ).fetchall(),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("read", [False, True])
async def test_expired_maintenance_lease_rolls_back_stall_projection_after_history_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read: bool
) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=30)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="continue")
    for _ in range(3 if read else 2):
        await iteration(manager, agent, read=read)
    job, _ = await iteration(manager, agent, read=read, receive=False)
    before = await _projection_rows(manager.db_path, goal_id)
    async with aiosqlite.connect(manager.db_path) as db:
        revisions_before = await (
            await db.execute("SELECT COUNT(*) FROM project_revisions")
        ).fetchone()
    assert revisions_before is not None

    clock = MutableClock(datetime.now(UTC))
    await start_instance(manager.db_path, "cp_stall_review", clock)
    leases = MaintenanceLeaseService(manager.db_path, lease_seconds=1, clock=clock)
    lease = await leases.acquire("project-stall-review", "cp_stall_review")
    assert lease is not None
    guard = MaintenanceLeaseGuard(leases, lease)
    original = manager._project_stalled_locked

    async def expire_after_scan(db: aiosqlite.Connection, goal: str, revision: int) -> str | None:
        stalled = await original(db, goal, revision)
        assert stalled
        clock.advance(2)
        return stalled

    monkeypatch.setattr(manager, "_project_stalled_locked", expire_after_scan)
    with pytest.raises(MaintenanceLeaseLost):
        await manager.on_job_result(job, maintenance_guard=guard)

    assert not await leases.is_current(lease.name, lease.owner_instance_id, lease.generation)
    # The accepted private receipt and independently committed snapshot survive;
    # none of the stale owner's conversation, status or dispatch projection does.
    assert await _projection_rows(manager.db_path, goal_id) == before
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM project_revisions")).fetchone() == (
            revisions_before[0] + 1,
        )
        assert await (
            await db.execute(
                "SELECT COUNT(*) FROM project_revisions WHERE worker_job_id=?", (job["id"],)
            )
        ).fetchone() == (1,)
