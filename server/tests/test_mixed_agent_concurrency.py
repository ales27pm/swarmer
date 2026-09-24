"""Mixed DAGs retain dependency and conversation fences across worker overlap."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.execution_engine import ExecutionEngine
from app.services.goal_manager import GoalManager
from app.services.goal_project import GoalProjectService
from app.services.swarm_contracts import GoalCreateRequest, GoalMessageRequest, GoalStartRequest
from tests.test_goal_project_runtime import _project, _result
from tests.test_goal_runtime_recovery import _manager
from tests.test_project_research_continuation import Planner, agent, node


async def mixed_goal(
    tmp_path: Path, nodes: list[dict[str, Any]]
) -> tuple[GoalManager, str, dict[str, str]]:
    planner = Planner(nodes, parallel=2)
    manager = await _manager(tmp_path / "mixed.db", planner.plan)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager.project_applications = GoalProjectService(
        manager.db_path, ExecutionEngine(manager.db_path, workspace, manager.permission_policy)
    )
    workers = {
        skill: await agent(manager, skill)
        for skill in {str(item["required_skill"]) for item in nodes}
    }
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Build a web CRM",
            max_parallelism=2,
            max_model_calls=12,
            autonomy_profile="assisted",
        ),
        actor_id="phone",
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    return manager, str(goal["id"]), workers


@pytest.mark.asyncio
@pytest.mark.parametrize("optional", [False, True])
async def test_partial_project_rewires_downstream_while_independent_worker_is_running(
    tmp_path: Path, optional: bool
) -> None:
    project = node("implementation", "code.build_project")
    project["priority"] = 30
    research = node("independent_research", "research.query")
    research["priority"] = 20
    review = node("review_implementation", "writing.draft", ("implementation",))
    if optional:
        review["optional_dependencies"] = review["dependencies"]
        review["dependencies"] = []
    manager, goal_id, workers = await mixed_goal(tmp_path, [project, research, review])
    research_job = await manager.agent_dispatcher.claim(workers["research.query"])
    assert research_job is not None
    await manager.on_job_claimed(research_job)
    result, _ = await _result(manager, workers["code.build_project"], action="continue")

    # A partial project cannot release its consumer merely because its worker job
    # completed. Its next iteration and both DAG representations move atomically.
    assert await manager.agent_dispatcher.claim(workers["writing.draft"]) is None
    nodes = await manager.graph.list_nodes(goal_id)
    previous = next(item for item in nodes if item["worker_job_id"] == result["id"])
    successor = next(item for item in nodes if item["parent_node_id"] == previous["id"])
    consumer = next(item for item in nodes if item["title"] == "review_implementation")
    assert previous["status"] == "completed"
    assert successor["status"] == "dispatched"
    assert consumer["status"] == "planned"
    assert consumer["depends_on"] == [successor["id"]]
    async with aiosqlite.connect(manager.db_path) as db:
        edges = await (
            await db.execute(
                "SELECT from_node_id,dependency_type FROM plan_edges WHERE to_node_id=?",
                (consumer["id"],),
            )
        ).fetchall()
        assert edges == [(successor["id"], "optional" if optional else "hard")]
        assert await (await db.execute("SELECT COUNT(*) FROM project_revisions")).fetchone() == (1,)
        assert await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone() == (0,)
    still_running = await manager.agent_dispatcher.get_job(research_job["id"])
    assert still_running is not None and still_running["status"] == "claimed"
    next_code = await manager.agent_dispatcher.claim(workers["code.build_project"])
    assert next_code is not None and next_code["id"] == successor["worker_job_id"]
    assert next_code["payload"]["files"] == result["result"]["files"]
    assert sum(item["status"] in {"dispatched", "running"} for item in nodes) == 2
    assert await manager.agent_dispatcher.claim(workers["code.build_project"]) is None
    before = [(item["id"], item["depends_on"]) for item in nodes]
    await manager.on_job_result(result)
    assert [
        (item["id"], item["depends_on"]) for item in await manager.graph.list_nodes(goal_id)
    ] == before


@pytest.mark.asyncio
async def test_independent_agents_can_claim_two_jobs_but_third_waits_for_capacity(
    tmp_path: Path,
) -> None:
    first = node("first_search", "research.query")
    first["priority"] = 30
    second = node("second_search", "research.query")
    second["priority"] = 20
    writer = node("independent_writing", "writing.draft")
    writer["objective"] = "Explain the CRM customer lifecycle in French."
    manager, goal_id, workers = await mixed_goal(tmp_path, [first, second, writer])
    second_agent = await agent(manager, "research.query")
    job_a = await manager.agent_dispatcher.claim(workers["research.query"])
    job_b = await manager.agent_dispatcher.claim(second_agent)
    assert job_a is not None and job_b is not None and job_a["id"] != job_b["id"]
    assert await manager.agent_dispatcher.claim(workers["writing.draft"]) is None
    assert (
        sum(
            item["status"] in {"dispatched", "running"}
            for item in await manager.graph.list_nodes(goal_id)
        )
        == 2
    )
    completed, _ = await manager.agent_dispatcher.submit_result(
        workers["research.query"],
        job_a["id"],
        job_a["claim_token"],
        status="completed",
        error=None,
        lease_id=job_a["lease_id"],
        lease_generation=job_a["lease_generation"],
        result={"content_trust": "untrusted", "results": []},
    )
    await manager.on_job_result(completed)
    writing_job = await manager.agent_dispatcher.claim(workers["writing.draft"])
    assert writing_job is not None
    assert writing_job["payload"]["objective"] == "Build a web CRM"
    assert writing_job["payload"]["step_objective"] == writer["objective"]
    assert (
        sum(
            item["status"] in {"dispatched", "running"}
            for item in await manager.graph.list_nodes(goal_id)
        )
        == 2
    )


@pytest.mark.asyncio
async def test_reply_between_resume_read_and_lock_is_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, detail, coder = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await _result(manager, coder, action="clarify")
    # Emulate the recoverable historical gap between recording a partial result
    # and creating its successor, without running any worker or altering files.
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET status='running',current_phase='project_continue' WHERE id=?",
            (goal_id,),
        )
        before = await (
            await db.execute("SELECT id,snapshot_json FROM project_revisions ORDER BY id")
        ).fetchall()
        await db.commit()
    original = manager.graph.list_nodes
    injected = False

    async def after_read(identifier: str) -> list[dict[str, Any]]:
        nonlocal injected
        rows = await original(identifier)
        if not injected:
            injected = True
            await manager.reply_goal(
                goal_id,
                GoalMessageRequest(
                    message="Write an explanation first; keep the project files.",
                    client_message_id="racing-latest-request",
                ),
                actor_id="phone",
            )
        return rows

    monkeypatch.setattr(manager.graph, "list_nodes", after_read)
    await manager._resume_pending_conversation(goal_id)
    monkeypatch.setattr(manager.graph, "list_nodes", original)
    current = await manager.graph.get_goal(goal_id)
    assert injected and current is not None
    assert current["pending_message_revision"] == current["conversation_revision"] > 0
    assert len(await manager.graph.list_nodes(goal_id)) == 1
    assert await manager.agent_dispatcher.claim(coder) is None
    async with aiosqlite.connect(manager.db_path) as db:
        after = await (
            await db.execute("SELECT id,snapshot_json FROM project_revisions ORDER BY id")
        ).fetchall()
    assert after == before
    history = await manager.conversation_messages(goal_id)
    assert (
        history["messages"][-1]["content"] == "Write an explanation first; keep the project files."
    )
