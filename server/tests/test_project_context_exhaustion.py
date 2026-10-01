from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from app.services.evaluator_provider import NoopEvaluatorProvider
from app.services.maintenance_lease import (
    MaintenanceLeaseGuard,
    MaintenanceLeaseLost,
    MaintenanceLeaseService,
)
from app.services.project_compaction import ProjectContextBudgetExceeded
from tests.test_goal_project_runtime import _project, _result
from tests.test_maintenance_leases import MutableClock, start_instance

REASON = "project context exceeds input budget; pinned requirements preserved"


@pytest.mark.asyncio
async def test_irreducible_context_stops_without_evaluator_or_lost_revision(tmp_path, monkeypatch):
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    calls = []

    async def evaluate(context):
        calls.append(context)
        return await NoopEvaluatorProvider().evaluate(context)

    monkeypatch.setattr(manager.evaluator, "evaluate", evaluate)

    async def too_large(*args, **kwargs):
        raise ProjectContextBudgetExceeded(REASON)

    monkeypatch.setattr(manager.project_applications, "payload", too_large)
    await _result(manager, agent, action="continue")
    await manager.reconcile()
    current = await manager.get_goal(goal_id)
    assert current["goal"]["status"] == "failed"
    assert current["goal"]["failure_reason"] == REASON
    assert calls == []
    assert len(current["nodes"]) == 2
    assert sorted(node["status"] for node in current["nodes"]) == ["blocked", "completed"]
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT count(*) FROM project_revisions")).fetchone() == (1,)
        assert await (await db.execute("SELECT count(*) FROM agent_jobs")).fetchone() == (1,)
    before = current["goal"]["model_call_count"]
    await manager.reconcile()
    assert (await manager.get_goal(goal_id))["goal"]["model_call_count"] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["new_revision", "pending_reply", "other_error", "active_node"])
async def test_context_stop_fences_new_work_and_other_failures(tmp_path, change):
    manager, detail, _ = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    node_id = detail["nodes"][0]["id"]
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET status='blocked',error_summary=? WHERE id=?",
            (REASON, node_id),
        )
        await db.commit()
    observed = await manager.graph.get_goal(goal_id)
    nodes = await manager.graph.list_nodes(goal_id)
    async with aiosqlite.connect(manager.db_path) as db:
        if change == "new_revision":
            await db.execute(
                "UPDATE goal_runs SET conversation_revision=conversation_revision+1 WHERE id=?",
                (goal_id,),
            )
        elif change == "pending_reply":
            await db.execute(
                "UPDATE goal_runs SET pending_message_revision=1 WHERE id=?", (goal_id,)
            )
        elif change == "other_error":
            await db.execute(
                "UPDATE plan_nodes SET error_summary='Temporary dependency unavailable' WHERE id=?",
                (node_id,),
            )
        else:
            await db.execute("UPDATE plan_nodes SET status='running' WHERE id=?", (node_id,))
        await db.commit()
    assert not await manager._stop_unrecoverable_project_context(observed, nodes)
    assert (await manager.graph.get_goal(goal_id))["status"] == "running"


@pytest.mark.asyncio
async def test_context_stop_rolls_back_when_maintenance_lease_expires_before_commit(
    tmp_path, monkeypatch
):
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]

    async def too_large(*args, **kwargs):
        raise ProjectContextBudgetExceeded(REASON)

    monkeypatch.setattr(manager.project_applications, "payload", too_large)
    await _result(manager, agent, action="continue")
    observed = await manager.graph.get_goal(goal_id)
    nodes = await manager.graph.list_nodes(goal_id)

    clock = MutableClock(datetime.now(UTC))
    await start_instance(manager.db_path, "cp_context_expiry_test", clock)
    leases = MaintenanceLeaseService(manager.db_path, lease_seconds=30, clock=clock)
    lease = await leases.acquire("goal-reconcile", "cp_context_expiry_test")
    assert lease is not None
    guard = MaintenanceLeaseGuard(leases, lease)
    original_terminate = manager._terminate_goal_locked

    async def terminate_then_expire(*args, **kwargs):
        await original_terminate(*args, **kwargs)
        # The writer lock prevents renewal while this transaction remains open.
        clock.advance(31)

    monkeypatch.setattr(manager, "_terminate_goal_locked", terminate_then_expire)
    with pytest.raises(MaintenanceLeaseLost):
        await manager._stop_unrecoverable_project_context(
            observed, nodes, maintenance_guard=guard
        )

    current = await manager.graph.get_goal(goal_id)
    assert current["status"] == "running"
    assert current["failure_reason"] == observed["failure_reason"]
    assert await manager.graph.list_nodes(goal_id) == nodes
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT count(*) FROM project_revisions")).fetchone() == (1,)
        assert await (await db.execute("SELECT count(*) FROM agent_jobs")).fetchone() == (1,)
