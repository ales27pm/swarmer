from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from pydantic import ValidationError

from app.services.project_contracts import ProjectPayload
from app.services.swarm_contracts import GoalCreateRequest
from tests.test_goal_project_runtime import _project, _result


async def accounting(db_path: Path, goal_id: str) -> tuple[Any, ...]:
    async with aiosqlite.connect(db_path) as db:
        goal = await (await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_id,))).fetchone()
        calls = await (await db.execute("SELECT COUNT(*) FROM goal_model_calls")).fetchone()
        jobs = await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone()
        revisions = await (await db.execute("SELECT COUNT(*) FROM project_revisions")).fetchone()
    return goal, calls, jobs, revisions


@pytest.mark.asyncio
@pytest.mark.parametrize("continued_goal", [False, True])
async def test_lifetime_revision_100_preserves_base_with_goal_scoped_iteration(
    tmp_path: Path, continued_goal: bool
) -> None:
    # The deterministic test planner and submitted inert CRM fixture do not
    # invoke a model or execute the generated project's source/check commands.
    manager, detail, agent = await _project(tmp_path)
    parent_goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="complete")
    service = manager.project_applications
    assert service is not None
    async with aiosqlite.connect(manager.db_path) as db:
        # A high project lifetime number is independent of this goal's single
        # receipt. Use a real captured revision, keeping its job/node provenance.
        await db.execute(
            "UPDATE project_revisions SET revision=100 WHERE goal_run_id=?", (parent_goal_id,)
        )
        await db.commit()
    latest = await service._latest(parent_goal_id)
    assert latest is not None and latest["revision"] == 100
    snapshot = json.loads(latest["snapshot_json"])
    parent_before = await accounting(manager.db_path, parent_goal_id)

    goal_id = parent_goal_id
    if continued_goal:
        goal = await manager.create_goal(
            GoalCreateRequest(objective="Add contact search to the CRM", max_model_calls=5),
            actor_id="test-phone",
        )
        goal_id = goal["id"]
        await service.inherit_project(parent_goal_id, goal_id)
    goal_before = await accounting(manager.db_path, goal_id)
    conversation = [{"role": "user", "content": "Keep the existing contacts and add search."}]
    value = await service.payload(
        goal_id,
        {"id": "node_contact_search", "objective": "Add CRM contact search"},
        conversation,
    )

    assert value["iteration"] == (1 if continued_goal else 2)
    assert value["base_revision_id"] == latest["id"]
    assert value["base_sha256"] == latest["sha256"]
    assert value["conversation"] == conversation
    for field in ("files", "checks", "plan", "focus_paths"):
        assert value[field] == snapshot[field]
    assert await service._latest(goal_id) == latest
    assert await accounting(manager.db_path, goal_id) == goal_before
    if continued_goal:
        # Creating the new goal is intentional; reading its payload must not
        # refund or otherwise mutate the parent's already charged call budget.
        parent_after = await accounting(manager.db_path, parent_goal_id)
        assert parent_after[0] == parent_before[0]
        assert parent_after[1:] == parent_before[1:]

    # Demonstrate the old formula's failure without changing shared app code:
    # its only changed payload field is 100 + 1, rejected by the same contract.
    with pytest.raises(ValidationError) as error:
        ProjectPayload.model_validate({**value, "iteration": int(latest["revision"]) + 1})
    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        (("iteration",), "less_than_equal")
    ]
