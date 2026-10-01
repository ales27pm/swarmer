from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.swarm_contracts import GoalCreateRequest, GoalMessageRequest, GoalStartRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan


async def queued_goal(tmp_path: Path, skill: str = "workspace.list_dir", arguments=None):
    plan = _worker_plan()
    plan = plan.model_copy(
        update={
            "nodes": [
                plan.nodes[0].model_copy(
                    update={"required_skill": skill, "worker_arguments": arguments}
                )
            ]
        }
    )
    manager = await _manager(tmp_path / "state.db", plan)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=plan.objective, max_replans=1), actor_id="phone"
    )
    started = await manager.start_goal(goal["id"], GoalStartRequest())
    agent = await manager.state_service.register_agent(
        AgentCreate(name="reader", endpoint="http://127.0.0.1:1", skills=[skill]), "phone"
    )
    await manager.state_service.heartbeat_agent(agent["id"], "online", agent["credential"])
    return manager, goal, started["nodes"][0], agent


async def steer(manager, goal):
    await manager.reply_goal(
        goal["id"],
        GoalMessageRequest(message="Use only the revised instructions", client_message_id="steer"),
        actor_id="phone",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill,arguments",
    [
        ("workspace.list_dir", {"path": "."}),
        ("workspace.read_text", {"path": "README.md"}),
        ("code_review.git_status", {}),
        (
            "research.collect",
            {
                "focus": "Official documentation",
                "queries": ["official documentation"],
                "max_results_per_query": 2,
                "max_pages": 2,
            },
        ),
    ],
)
async def test_claim_retires_stale_unclaimed_work_without_attempt_or_budget_charge(
    tmp_path: Path, skill: str, arguments: dict
) -> None:
    manager, goal, node, agent = await queued_goal(tmp_path, skill, arguments)
    await steer(manager, goal)
    before = await manager.graph.get_goal(goal["id"])
    assert await manager.agent_dispatcher.claim(agent["id"]) is None
    job = await manager.agent_dispatcher.get_job(node["worker_job_id"])
    assert job["status"] == "cancelled"
    assert job["attempt_count"] == job["lease_generation"] == 0
    assert job["claimed_by"] is None
    assert job["error"] == "goal_conversation_changed_before_claim"
    assert (await manager.graph.get_node(node["id"]))["status"] == "cancelled"
    assert (await manager.state_service.get_task(node["task_id"])).status.value == "cancelled"
    after = await manager.graph.get_goal(goal["id"])
    for key in ("step_count", "model_call_count", "replan_count", "pending_message_revision"):
        assert after[key] == before[key]
    assert await manager.agent_dispatcher.cancel_stale_goal_jobs(goal["id"]) == 0
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await (
                await db.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE event_type='agent.job.cancelled' AND task_id=?",
                    (node["task_id"],),
                )
            ).fetchone()
        )[0] == 1


@pytest.mark.asyncio
async def test_pending_reply_progresses_without_an_online_worker(tmp_path: Path) -> None:
    manager, goal, node, agent = await queued_goal(tmp_path)
    await manager.state_service.heartbeat_agent(agent["id"], "offline", agent["credential"])
    await steer(manager, goal)
    await manager.reconcile()
    current = await manager.graph.get_goal(goal["id"])
    assert current["pending_message_revision"] == 0
    assert current["replan_count"] == 1
    assert current["model_call_count"] == 2  # Initial + deterministic test replan only.
    old_job = await manager.agent_dispatcher.get_job(node["worker_job_id"])
    assert old_job["status"] == "cancelled" and old_job["attempt_count"] == 0
    new_nodes = [
        item for item in await manager.graph.list_nodes(goal["id"]) if item["id"] != node["id"]
    ]
    assert len(new_nodes) == 1 and new_nodes[0]["conversation_revision"] == 1
    assert new_nodes[0]["worker_job_id"] != node["worker_job_id"]


@pytest.mark.asyncio
async def test_claimed_job_keeps_lease_and_may_finish_after_reply(tmp_path: Path) -> None:
    manager, goal, _node, agent = await queued_goal(tmp_path)
    claimed = await manager.agent_dispatcher.claim(agent["id"])
    await steer(manager, goal)
    assert await manager.agent_dispatcher.cancel_stale_goal_jobs(goal["id"]) == 0
    renewed = await manager.agent_dispatcher.heartbeat(
        agent["id"],
        claimed["id"],
        claimed["claim_token"],
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
    )
    assert renewed["lease_generation"] == claimed["lease_generation"]
    finished, changed = await manager.agent_dispatcher.submit_result(
        agent["id"],
        claimed["id"],
        claimed["claim_token"],
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
        status="completed",
        result={"entries": []},
        error=None,
    )
    assert changed and finished["status"] == "completed"
    assert finished["attempt_count"] == 1


@pytest.mark.asyncio
async def test_null_job_binding_publication_window_is_fenced(tmp_path: Path) -> None:
    manager, goal, node, agent = await queued_goal(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE plan_nodes SET worker_job_id=NULL WHERE id=?", (node["id"],))
        await db.commit()
    await steer(manager, goal)
    assert await manager.agent_dispatcher.claim(agent["id"]) is None
    assert (await manager.agent_dispatcher.get_job(node["worker_job_id"]))["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["device", "goal:another_project_goal"])
async def test_foreign_task_source_cannot_borrow_a_goal_revision(
    tmp_path: Path, source: str
) -> None:
    manager, goal, node, agent = await queued_goal(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE tasks SET source=? WHERE id=?", (source, node["task_id"]))
        await db.commit()
    await steer(manager, goal)
    assert await manager.agent_dispatcher.cancel_stale_goal_jobs(goal["id"]) == 0
    assert (await manager.agent_dispatcher.claim(agent["id"]))["id"] == node["worker_job_id"]


@pytest.mark.asyncio
async def test_shared_non_goal_job_is_claimed_after_stale_goal_job_is_retired(
    tmp_path: Path,
) -> None:
    manager, goal, node, agent = await queued_goal(tmp_path)
    task = await manager.state_service.create_task(
        TaskRecord.new(TaskCreate(input="shared read"), source="device")
    )
    shared = await manager.agent_dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
    await steer(manager, goal)
    assert (await manager.agent_dispatcher.claim(agent["id"]))["id"] == shared["id"]
    assert (await manager.agent_dispatcher.get_job(node["worker_job_id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_retirement_rolls_back_job_task_and_node_together(
    tmp_path: Path, monkeypatch
) -> None:
    manager, goal, node, agent = await queued_goal(tmp_path)
    await steer(manager, goal)

    async def fail_outbox(*args, **kwargs):
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(manager.agent_dispatcher.outbox, "enqueue_locked", fail_outbox)
    with pytest.raises(RuntimeError, match="injected outbox failure"):
        await manager.agent_dispatcher.claim(agent["id"])
    assert (await manager.agent_dispatcher.get_job(node["worker_job_id"]))["status"] == "queued"
    assert (await manager.state_service.get_task(node["task_id"])).status.value == "queued"
    assert (await manager.graph.get_node(node["id"]))["status"] == "dispatched"
