from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import AgentCreate
from app.services.execution_engine import ExecutionEngine
from app.services.goal_manager import GoalManager
from app.services.goal_project import GoalProjectService
from app.services.swarm_contracts import GoalCreateRequest, GoalMessageRequest, GoalStartRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan

SOURCE = 'def add_customer(name):\n    return {"name": name}\n'


async def _project(
    tmp_path: Path, *, max_calls: int = 8, manual: bool = False
) -> tuple[GoalManager, dict[str, Any], str]:
    plan = _worker_plan(objective="Build a web CRM")
    plan.nodes[0] = plan.nodes[0].model_copy(
        update={
            "required_skill": "code.build_project",
            "objective": "Build a web CRM",
            "expected_output": "A project with working tests",
            "title": "Build project",
        }
    )
    manager = await _manager(tmp_path / "project.db", plan)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager.project_applications = GoalProjectService(
        manager.db_path, ExecutionEngine(manager.db_path, workspace, manager.permission_policy)
    )
    agent = await manager.state_service.register_agent(
        AgentCreate(
            name="Project worker", endpoint="https://worker.invalid", skills=["code.build_project"]
        ),
        "phone",
    )
    await manager.state_service.heartbeat_agent(agent["id"], "online", agent["credential"])
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Build a web CRM",
            max_model_calls=max_calls,
            autonomy_profile="manual" if manual else "assisted",
        ),
        actor_id="phone",
    )
    detail = await manager.start_goal(goal["id"], GoalStartRequest())
    assert detail["nodes"][0]["status"] == "dispatched"
    return manager, detail, agent["id"]


async def _result(
    manager: GoalManager,
    agent_id: str,
    *,
    action: str,
    message: str = "Implementing customer management",
    receive: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    claimed = await manager.agent_dispatcher.claim(agent_id)
    assert claimed is not None
    payload = claimed["payload"]
    job, changed = await manager.agent_dispatcher.submit_result(
        agent_id,
        claimed["id"],
        claimed["claim_token"],
        status="completed",
        error=None,
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
        result={
            "schema_version": "1.0",
            "action": action,
            "message": message,
            "plan": ["Implement customer management", "Test the interface"],
            "files": [
                {"path": "app.py", "content": SOURCE},
                {"path": "README.md", "content": "Run python3 app.py"},
                {
                    "path": "test_app.py",
                    "content": "from app import add_customer\ndef test_customer():\n    assert add_customer('A')['name'] == 'A'\n",
                },
            ],
            "checks": [
                {
                    "command": ["python3", "-m", "unittest"],
                    "status": "passed",
                    "exit_code": 0,
                    "output": "Ran 1 test",
                    "duration_ms": 30,
                }
            ],
            "run_instructions": "python3 app.py",
            "runtime": "python",
            "base_revision_id": payload["base_revision_id"],
            "base_sha256": payload["base_sha256"],
        },
    )
    assert changed
    if receive:
        await manager.on_job_result(job)
    return job, payload


@pytest.mark.asyncio
async def test_project_question_reply_resumes_with_snapshot_and_history(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    job, _ = await _result(
        manager, agent, action="clarify", message="Should customer records be shared by a team?"
    )
    waiting = await manager.get_goal(goal_id)
    assert waiting is not None and waiting["goal"]["current_phase"] == "needs_user"
    assert waiting["nodes"][0]["status"] == "completed"
    assert SOURCE not in json.dumps(waiting)
    question = (await manager.conversation_messages(goal_id))["pending_question_id"]
    assert question
    assert await manager.reconcile() == 0
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(
            message="Yes, shared team records",
            client_message_id="team-answer",
            reply_to_message_id=question,
        ),
        actor_id="phone",
    )
    await manager.reconcile()
    current = await manager.get_goal(goal_id)
    assert current is not None and len(current["nodes"]) == 2
    assert current["goal"]["model_call_count"] == 3
    _, payload = await _result(manager, agent, action="complete")
    assert payload["iteration"] == 2 and payload["base_revision_id"] is not None
    assert payload["files"][0]["content"] == SOURCE
    assert payload["conversation"][-1] == {"role": "user", "content": "Yes, shared team records"}
    ready = await manager.get_goal(goal_id)
    assert ready is not None and ready["goal"]["current_phase"] == "project_ready"
    assert sum(n["status"] == "waiting_permission" for n in ready["nodes"]) == 1
    assert SOURCE not in json.dumps(ready)
    assert await manager.on_job_result(job) == ready


@pytest.mark.asyncio
async def test_project_stale_completion_continues_queued_steering(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    job, _ = await _result(manager, agent, action="complete", receive=False)
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(message="Also add CSV export", client_message_id="export"),
        actor_id="phone",
    )
    await manager.on_job_result(job)
    current = await manager.get_goal(goal_id)
    assert current is not None and current["goal"]["status"] == "running"
    assert len(current["nodes"]) == 2
    assert all(n["status"] != "waiting_permission" for n in current["nodes"])
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone() == (0,)
    _, payload = await _result(manager, agent, action="clarify", message="Which CSV columns?")
    assert any(m["content"] == "Also add CSV export" for m in payload["conversation"])


@pytest.mark.asyncio
async def test_project_continue_respects_total_model_budget(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=2)
    await _result(manager, agent, action="continue")
    current = await manager.get_goal(detail["goal"]["id"])
    assert current is not None and current["goal"]["status"] == "budget_exhausted"
    assert current["goal"]["model_call_count"] == 2
    assert len(current["nodes"]) == 1
    assert await manager.agent_dispatcher.claim(agent) is None


@pytest.mark.asyncio
async def test_project_manual_continuation_waits_for_explicit_start(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path, manual=True)
    await _result(manager, agent, action="continue")
    current = await manager.get_goal(detail["goal"]["id"])
    assert current is not None and sum(n["status"] == "ready" for n in current["nodes"]) == 1
    assert current["goal"]["model_call_count"] == 2
    await manager.reconcile()
    assert await manager.agent_dispatcher.claim(agent) is None
    await manager.start_goal(detail["goal"]["id"], GoalStartRequest())
    claimed = await manager.agent_dispatcher.claim(agent)
    assert claimed is not None and claimed["payload"]["iteration"] == 2


@pytest.mark.asyncio
async def test_manual_reply_grants_one_durable_dispatch_and_replay_grants_none(
    tmp_path: Path,
) -> None:
    manager, detail, agent = await _project(tmp_path, manual=True)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="clarify", message="Which customer fields?")
    question = (await manager.conversation_messages(goal_id))["pending_question_id"]
    reply = GoalMessageRequest(
        message="Name and email", client_message_id="manual-answer", reply_to_message_id=question
    )
    await manager.reply_goal(goal_id, reply, actor_id="phone")
    await manager.reconcile()
    current = await manager.get_goal(goal_id)
    assert current is not None and sum(n["status"] == "dispatched" for n in current["nodes"]) == 1
    await _result(manager, agent, action="continue")
    await manager.reply_goal(goal_id, reply, actor_id="phone")
    await manager.reconcile()
    assert await manager.agent_dispatcher.claim(agent) is None
    current = await manager.graph.get_goal(goal_id)
    assert current is not None and current["reply_dispatch_credit"] == 0
    assert current["model_call_count"] == 3


@pytest.mark.asyncio
async def test_new_reply_supersedes_unprepared_project_but_does_not_apply_it(
    tmp_path: Path,
) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="complete")
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(message="Add customer search", client_message_id="search"),
        actor_id="phone",
    )
    await manager.reconcile()
    current = await manager.get_goal(goal_id)
    assert current is not None and current["goal"]["status"] == "running"
    assert sum(n["status"] == "dispatched" for n in current["nodes"]) == 1
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone() == (0,)


@pytest.mark.asyncio
async def test_project_snapshot_commit_is_recovered_before_next_iteration(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    job, _ = await _result(manager, agent, action="continue", receive=False)
    assert manager.project_applications is not None
    await manager.project_applications.capture_result(goal_id, detail["nodes"][0]["id"], job["id"])
    # Simulate an exit after immutable snapshot commit but before node projection.
    await manager.reconcile()
    await manager.reconcile()
    current = await manager.get_goal(goal_id)
    assert current is not None and len(current["nodes"]) == 2
    assert current["goal"]["model_call_count"] == 3
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM project_revisions")).fetchone() == (1,)
        assert await (
            await db.execute("SELECT COUNT(*) FROM goal_messages WHERE role='assistant'")
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_legacy_file_continuation_preserves_source_in_linked_project(tmp_path: Path) -> None:
    from tests.test_goal_code_application import CONTENT, _prepared

    manager, engine, old_id, _ = await _prepared(tmp_path)
    manager.project_applications = GoalProjectService(manager.db_path, engine)
    await manager.cancel_goal(old_id, actor_id="phone")
    replied = await manager.reply_goal(
        old_id,
        GoalMessageRequest(
            message="Build this into a complete web application",
            client_message_id="legacy-continue",
        ),
        actor_id="phone",
    )
    new_id = replied["goal"]["id"]
    assert new_id != old_id
    preview = await manager.project_applications.get_project(new_id)
    assert preview is not None
    assert any(file["path"] == "app.py" and file["content"] == CONTENT for file in preview["files"])
