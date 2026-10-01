from pathlib import Path

import aiosqlite
import pytest

from app.services.goal_project import GoalProjectService
from app.services.planner_provider import DeterministicSwarmPlannerProvider
from app.services.project_context import ProjectContextConflict, ProjectContextService
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest
from tests.test_agent_capsule import capsule
from tests.test_goal_code_application import _coding_manager
from tests.test_goal_writing import OBJECTIVE, plan


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "invalid", "overflow"])
async def test_writing_context_failure_blocks_before_job_and_writer_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    manager, engine = await _coding_manager(tmp_path)
    manager.planner = DeterministicSwarmPlannerProvider(plan())
    manager.project_applications = GoalProjectService(manager.db_path, engine)
    context = ProjectContextService(manager.db_path)
    manager.project_applications.context = context
    goal = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    worker_payload = manager._worker_payload

    async def dispatch_with_changed_context(goal, node):
        if failure == "unavailable":

            async def unavailable(goal_id):
                raise ProjectContextConflict("private source details must not escape")

            monkeypatch.setattr(context, "refresh", unavailable)
        else:
            value = capsule()
            if failure == "invalid":
                value["requirements"][0]["source_id"] = "invalid source id"
            else:
                value["requirements"][0]["text"] = "Required text. " * 3_000
            monkeypatch.setattr(context, "prompt_state", lambda state: value)
        return await worker_payload(goal, node)

    monkeypatch.setattr(manager, "_worker_payload", dispatch_with_changed_context)
    detail = await manager.start_goal(goal["id"], GoalStartRequest())
    node = detail["nodes"][0]
    assert node["status"] == "blocked"
    assert node["worker_job_id"] is None
    assert (
        node["error_summary"]
        == "Writing context is unavailable or exceeds its required input budget."
    )
    assert detail["goal"]["model_call_count"] == 1  # Planner only.
    assert detail["goal"]["step_count"] == 0
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone() == (0,)
