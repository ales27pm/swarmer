"""Exercise structured reads with the real dispatcher, file reader and handoff."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from app.models import AgentCreate
from app.services.agent_dispatcher import AgentDispatchConflict
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest, SwarmPlanProposal
from app.services.worker_context import read_worker_context
from tests.test_goal_manager import _manager

OBJECTIVE = "Read notes.txt from the reader workspace and summarize its contents."
FILE_TEXT = "Application notes: use SQLite for customer records.\n"


def file_worker() -> Any:
    path = Path(__file__).resolve().parents[2] / "workers/file-worker/file_worker.py"
    spec = importlib.util.spec_from_file_location("workspace_read_file_worker", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def started(tmp_path: Path) -> tuple[Any, dict[str, Any], list[str], dict[str, Any]]:
    proposal = SwarmPlanProposal.model_validate(
        {
            "schema_version": "1.0",
            "objective": OBJECTIVE,
            "rationale_summary": "Read the actual file before summarizing it.",
            "completion_criteria": ["The summary is grounded in the file contents."],
            "max_parallelism": 1,
            "nodes": [
                {
                    "temporary_id": "read",
                    "node_type": "worker",
                    "title": "Read notes",
                    "objective": "Read notes.txt from the reader workspace.",
                    "required_skill": "workspace.read_text",
                    "worker_arguments": {"path": "notes.txt"},
                    "dependencies": [],
                    "expected_output": "The actual file contents.",
                    "priority": 10,
                },
                {
                    "temporary_id": "write",
                    "node_type": "worker",
                    "title": "Summarize notes",
                    "objective": OBJECTIVE,
                    "required_skill": "writing.draft",
                    "dependencies": ["read"],
                    "expected_output": "A summary of the file contents.",
                    "priority": 5,
                },
            ],
        }
    )
    manager = await _manager(tmp_path, proposal)
    agents = []
    for skill in ("workspace.read_text", "writing.draft"):
        registration = await manager.state_service.register_agent(
            AgentCreate(name=skill, endpoint="https://worker.invalid", skills=[skill]), "phone"
        )
        await manager.state_service.heartbeat_agent(
            registration["id"], "online", registration["credential"]
        )
        agents.append(registration["id"])
    goal = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agents[0])
    assert job and job["payload"] == {"path": "notes.txt"}
    await manager.on_job_claimed(job)
    assert await manager.agent_dispatcher.claim(agents[1]) is None
    return manager, goal, agents, job


async def submit(
    manager: Any,
    agent: str,
    job: dict[str, Any],
    value: dict[str, Any] | None,
    *,
    status: str = "completed",
) -> None:
    completed, changed = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status=status,
        result=value,
        error="file unavailable" if status == "failed" else None,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    assert changed
    await manager.on_job_result(completed)


@pytest.mark.asyncio
async def test_actual_workspace_read_reaches_dependent_writer(tmp_path: Path) -> None:
    workspace = tmp_path / "reader-root"
    workspace.mkdir()
    (workspace / "notes.txt").write_text(FILE_TEXT, encoding="utf-8")
    manager, goal, agents, job = await started(tmp_path)
    nodes = await manager.graph.list_nodes(goal["id"])
    reader = next(node for node in nodes if node["required_skill"] == "workspace.read_text")
    writer = next(node for node in nodes if node["required_skill"] == "writing.draft")
    assert reader["planner_metadata"]["worker_arguments"] == {"path": "notes.txt"}
    with pytest.raises(ValueError, match="required dependency is incomplete"):
        await read_worker_context(manager.db_path, goal["id"], writer["id"])

    result = file_worker().execute(workspace, job)
    assert result == {"content": FILE_TEXT}
    await submit(manager, agents[0], job, result)
    downstream = await manager.agent_dispatcher.claim(agents[1])
    assert downstream is not None
    context = downstream["payload"]["dependency_context"]
    assert len(context) == 1
    assert context[0]["node_id"] == reader["id"]
    assert context[0]["worker_job_id"] == job["id"]
    assert context[0]["required_skill"] == "workspace.read_text"
    assert context[0]["content_trust"] == "untrusted"
    assert FILE_TEXT.strip() in context[0]["summary"]
    assert (await read_worker_context(manager.db_path, goal["id"], writer["id"]))[0] == context


@pytest.mark.asyncio
async def test_draft_only_file_does_not_resolve_in_reader_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "reader-root"
    workspace.mkdir()
    draft = tmp_path / "project-draft"
    draft.mkdir()
    (draft / "notes.txt").write_text(FILE_TEXT, encoding="utf-8")
    manager, goal, agents, job = await started(tmp_path)
    with pytest.raises(ValueError, match="unavailable paths"):
        file_worker().execute(workspace, job)
    await submit(manager, agents[0], job, None, status="failed")
    assert await manager.agent_dispatcher.claim(agents[1]) is None
    reader = next(
        node
        for node in await manager.graph.list_nodes(goal["id"])
        if node["required_skill"] == "workspace.read_text"
    )
    assert reader["status"] == "failed"
    assert (draft / "notes.txt").read_text(encoding="utf-8") == FILE_TEXT


@pytest.mark.asyncio
async def test_announced_read_does_not_satisfy_dependent_writer(tmp_path: Path) -> None:
    manager, goal, agents, job = await started(tmp_path)
    await submit(manager, agents[0], job, {"summary": "I will read notes.txt."})
    assert await manager.agent_dispatcher.claim(agents[1]) is None
    reader = next(
        node
        for node in await manager.graph.list_nodes(goal["id"])
        if node["required_skill"] == "workspace.read_text"
    )
    assert reader["status"] == "failed"
    assert reader["error_summary"] == "worker completed without valid skill evidence"


@pytest.mark.asyncio
async def test_wrong_lease_read_result_cannot_release_writer(tmp_path: Path) -> None:
    manager, _, agents, job = await started(tmp_path)
    wrong_lease = {**job, "lease_generation": job["lease_generation"] + 1}
    with pytest.raises(AgentDispatchConflict, match="lease is stale"):
        await submit(manager, agents[0], wrong_lease, {"content": FILE_TEXT})
    assert await manager.agent_dispatcher.claim(agents[1]) is None
    await submit(manager, agents[0], job, {"content": FILE_TEXT})
    assert await manager.agent_dispatcher.claim(agents[1]) is not None
