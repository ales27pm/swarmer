"""Recovery decoder accepts durable markers but never enables native authoring."""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from app.services.goal_project import GoalProjectConflict
from app.services.project_contracts import ProjectResult
from tests.test_goal_project_runtime import _project
from tests.test_project_publication import REQUESTER


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["authoring", "required"])
@pytest.mark.parametrize("swift_present", [True, False])
async def test_compatibility_recovery_preserves_marked_history_and_pauses(
    tmp_path: Path, marker: str, swift_present: bool
) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=12)
    goal_id = detail["goal"]["id"]
    job = await manager.agent_dispatcher.claim(agent)
    assert job
    files = [{"path": "README.md", "content": "Native qualification, not validated."}]
    if swift_present:
        files.append({"path": "Hello.swift", "content": 'print("Hello")\n'})
    value = {
        "schema_version": "1.0", "action": "continue", "native_validation": marker,
        "message": "Draft saved; native compilation remains separate.",
        "plan": ["Create Hello", "Validate native source"], "files": files,
        "checks": [], "runtime": "python", "run_instructions": "swift test",
        "focus_paths": [], "base_revision_id": job["payload"]["base_revision_id"],
        "base_sha256": job["payload"]["base_sha256"],
    }
    accepted, changed = await manager.agent_dispatcher.submit_result(
        agent, job["id"], job["claim_token"], status="completed", result=value,
        error=None, lease_id=job["lease_id"], lease_generation=job["lease_generation"],
    )
    assert changed
    await manager.on_job_result(accepted)
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["status"] == "waiting_permission"
    assert current["goal"]["current_phase"] == "needs_user"
    assert await manager.agent_dispatcher.claim(agent) is None
    assert manager.project_applications
    service = manager.project_applications
    async with aiosqlite.connect(manager.db_path) as db:
        stored = await (await db.execute("SELECT snapshot_json FROM project_revisions")).fetchone()
    assert stored
    ProjectResult.model_validate_json(stored[0])
    assert json.loads(stored[0])["native_validation"] == "required"
    preview = await service.get_project(goal_id)
    assert preview and preview["state"] == "needs_user" and preview["files"] == files
    with pytest.raises(GoalProjectConflict, match="Validation Swift/iOS"):
        await service.apply(goal_id, preview["revision_id"], requester=REQUESTER, sha256=preview["sha256"])
    payload = await service.payload(goal_id, detail["nodes"][0], [])
    assert "native_validation" not in payload
    async with aiosqlite.connect(manager.db_path) as db:
        unchanged = await (await db.execute("SELECT snapshot_json FROM project_revisions")).fetchone()
    assert unchanged == stored
    assert (await manager.conversation_messages(goal_id))["pending_question_id"] is None
