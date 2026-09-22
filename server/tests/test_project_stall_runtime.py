from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.goal_manager import GoalManager
from app.services.swarm_contracts import GoalMessageRequest
from tests.test_goal_project_runtime import _project, _result


async def iteration(
    manager: GoalManager,
    agent: str,
    *,
    read: bool = False,
    changed: bool = False,
    improved_check: bool = False,
    receive: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None
    payload = job["payload"]
    files = copy.deepcopy(payload["files"])
    checks = copy.deepcopy(payload["checks"])
    if changed:
        files[0]["content"] += "\n# Customer search\n"
    if improved_check:
        checks.append(
            {
                "command": ["python3", "-m", "compileall", "."],
                "status": "passed",
                "exit_code": 0,
                "output": "Compilation passed",
                "duration_ms": 2,
            }
        )
    result = {
        "schema_version": "1.0",
        "action": "continue",
        "message": "All customer features are implemented and working.",
        "files": files,
        "checks": checks,
        "plan": payload["plan"],
        "run_instructions": "python3 app.py",
        "runtime": "python",
        "focus_paths": ["app.py"] if read else [],
        "base_revision_id": payload["base_revision_id"],
        "base_sha256": payload["base_sha256"],
    }
    accepted, updated = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="completed",
        result=result,
        error=None,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    assert updated
    if receive:
        await manager.on_job_result(accepted)
    return accepted, payload


@pytest.mark.asyncio
async def test_three_unchanged_attempts_pause_across_reads_without_forging_a_question(
    tmp_path: Path,
) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=30)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="continue")
    assert manager.project_applications is not None
    before = await manager.project_applications.get_project(goal_id)
    for _ in range(2):
        await iteration(manager, agent, read=True)
        await iteration(manager, agent)
    await iteration(manager, agent, read=True)
    final_job, _ = await iteration(manager, agent)
    stopped = await manager.get_goal(goal_id)
    assert stopped is not None
    assert stopped["goal"]["status"] == "waiting_permission"
    assert stopped["goal"]["current_phase"] == "needs_user"
    assert "trois tentatives" in stopped["goal"]["evaluator_summary"]
    assert len(stopped["nodes"]) == 7
    assert not any(n["status"] in {"ready", "running", "dispatched"} for n in stopped["nodes"])
    assert await manager.agent_dispatcher.claim(agent) is None
    conversation = await manager.conversation_messages(goal_id)
    assert conversation["pending_question_id"] is None
    assert "All customer features" not in str(conversation)
    after = await manager.project_applications.get_project(goal_id)
    assert before and after
    assert after["files"] == before["files"] and after["checks"] == before["checks"]
    # Recovery/repeated delivery must not create another charged iteration.
    await manager.on_job_result(final_job)
    await manager.reconcile()
    repeated = await manager.get_goal(goal_id)
    assert repeated is not None
    assert len(repeated["nodes"]) == len(stopped["nodes"])
    assert repeated["goal"]["model_call_count"] == stopped["goal"]["model_call_count"]
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("progress", ["files", "check"])
async def test_real_progress_resets_unproductive_streak(tmp_path: Path, progress: str) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=30)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="continue")
    await iteration(manager, agent)
    await iteration(manager, agent)
    await iteration(manager, agent, changed=progress == "files", improved_check=progress == "check")
    await iteration(manager, agent)
    current = await manager.get_goal(goal_id)
    assert current is not None and current["goal"]["status"] == "running"
    assert sum(n["status"] == "dispatched" for n in current["nodes"]) == 1


@pytest.mark.asyncio
async def test_user_resume_starts_new_streak_without_refunding_budget(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=30)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="continue")
    for _ in range(3):
        await iteration(manager, agent)
    paused = await manager.get_goal(goal_id)
    assert paused and paused["goal"]["status"] == "waiting_permission"
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(message="Add customer search", client_message_id="resume"),
        actor_id="phone",
    )
    await manager.reconcile()
    await iteration(manager, agent)
    resumed = await manager.get_goal(goal_id)
    assert resumed and resumed["goal"]["status"] == "running"
    assert resumed["goal"]["model_call_count"] > paused["goal"]["model_call_count"]


@pytest.mark.asyncio
async def test_pending_user_steering_is_not_paused_by_stale_third_result(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=30)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="continue")
    await iteration(manager, agent)
    await iteration(manager, agent)
    final_job, _ = await iteration(manager, agent, receive=False)
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(message="Add a customer list", client_message_id="steering"),
        actor_id="phone",
    )
    await manager.on_job_result(final_job)
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["status"] == "running"
    assert sum(n["status"] == "dispatched" for n in current["nodes"]) == 1


@pytest.mark.asyncio
async def test_valid_check_only_completion_still_reaches_review(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=30)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="continue")
    await iteration(manager, agent)
    await iteration(manager, agent)
    await _result(manager, agent, action="complete")
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["current_phase"] == "project_ready"
