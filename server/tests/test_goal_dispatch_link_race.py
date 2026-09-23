from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.goal_manager import GoalManager
from app.services.swarm_contracts import AutonomyProfile, GoalCreateRequest, GoalStartRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan


@asynccontextmanager
async def dispatch_paused_after_queue(
    tmp_path: Path,
) -> AsyncIterator[tuple[GoalManager, GoalManager, str, dict[str, Any]]]:
    path = tmp_path / "dispatch-link.db"
    first = await _manager(path, _worker_plan())
    recovering = await _manager(path, _worker_plan())
    goal = await first.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository", autonomy_profile=AutonomyProfile.AUTONOMOUS
        ),
        actor_id="phone",
    )
    queued, release = asyncio.Event(), asyncio.Event()
    original = first.agent_dispatcher.queue_job
    recorded: dict[str, Any] = {}

    async def paused_queue(*args: Any, **kwargs: Any) -> dict[str, Any]:
        job = await original(*args, **kwargs)
        recorded.update(job)
        queued.set()
        await release.wait()
        return job

    first.agent_dispatcher.queue_job = paused_queue  # type: ignore[method-assign]
    start = asyncio.create_task(first.start_goal(str(goal["id"]), GoalStartRequest()))
    try:
        await asyncio.wait_for(queued.wait(), timeout=5)
        yield first, recovering, str(goal["id"]), recorded
    finally:
        release.set()
        await asyncio.wait_for(start, timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_before_return", [False, True])
async def test_recovery_of_identical_active_job_does_not_cancel_dispatch(
    tmp_path: Path, claim_before_return: bool
) -> None:
    async with dispatch_paused_after_queue(tmp_path) as (first, recovering, goal_id, job):
        await recovering.reconcile()
        nodes = await recovering.graph.list_nodes(goal_id)
        assert len(nodes) == 1 and nodes[0]["worker_job_id"] == job["id"]
        if claim_before_return:
            agent = await recovering.state_service.register_agent(
                AgentCreate(
                    name="Read-only worker",
                    endpoint="https://worker.invalid",
                    skills=["workspace.list_dir"],
                ),
                "phone",
            )
            await recovering.state_service.heartbeat_agent(
                agent["id"], "online", agent["credential"]
            )
            claimed = await recovering.agent_dispatcher.claim(agent["id"])
            assert claimed and claimed["id"] == job["id"]
            await recovering.on_job_claimed(claimed)
        before = await recovering.agent_dispatcher.get_job(job["id"])
        assert before
    current = await first.agent_dispatcher.get_job(job["id"])
    assert current and current["status"] == ("claimed" if claim_before_return else "queued")
    assert current["lease_generation"] == before["lease_generation"]
    node = (await first.graph.list_nodes(goal_id))[0]
    assert node["status"] == ("running" if claim_before_return else "dispatched")
    assert node["task_id"] == job["task_id"] and node["worker_job_id"] == job["id"]
    task = await first.state_service.get_task(job["task_id"])
    assert task and task.status.value == ("running" if claim_before_return else "queued")
    async with aiosqlite.connect(first.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone() == (1,)
        assert await (
            await db.execute("SELECT step_count FROM goal_runs WHERE id=?", (goal_id,))
        ).fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["replacement", "terminal_node", "terminal_goal"])
async def test_conflicting_or_terminal_link_still_fences_original_child(
    tmp_path: Path, conflict: str
) -> None:
    replacement = None
    async with dispatch_paused_after_queue(tmp_path) as (first, recovering, goal_id, job):
        await recovering.reconcile()
        node = (await recovering.graph.list_nodes(goal_id))[0]
        if conflict == "replacement":
            task = TaskRecord.new(
                TaskCreate(input="Independent read-only replacement"), source="phone"
            )
            await recovering.state_service.create_task(task)
            replacement = await recovering.agent_dispatcher.queue_job(
                task.id, "workspace.list_dir", job["payload"]
            )
        async with aiosqlite.connect(first.db_path) as db:
            if replacement:
                await db.execute(
                    "UPDATE plan_nodes SET task_id=?,worker_job_id=? WHERE id=?",
                    (replacement["task_id"], replacement["id"], node["id"]),
                )
            elif conflict == "terminal_node":
                await db.execute(
                    "UPDATE plan_nodes SET status='cancelled' WHERE id=?", (node["id"],)
                )
            else:
                await db.execute("UPDATE goal_runs SET status='cancelled' WHERE id=?", (goal_id,))
            await db.commit()
    current = await first.agent_dispatcher.get_job(job["id"])
    assert current and current["status"] == "cancelled"
    task = await first.state_service.get_task(job["task_id"])
    assert task and task.status.value == "cancelled"
    if replacement:
        current_replacement = await first.agent_dispatcher.get_job(replacement["id"])
        assert current_replacement and current_replacement["status"] == "queued"
        node = (await first.graph.list_nodes(goal_id))[0]
        assert node["worker_job_id"] == replacement["id"]
