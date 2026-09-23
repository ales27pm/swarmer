from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.services.goal_manager import GoalManager
from app.services.project_contracts import ProjectPayload, ProjectResult, project_digest
from app.services.project_progress import has_project_progress
from app.services.swarm_contracts import GoalMessageRequest
from tests.test_goal_project_runtime import _project

FILES = [
    {
        "path": "Package.swift",
        "content": '// swift-tools-version: 5.9\nimport PackageDescription\nlet package = Package(name: "Addition", targets: [.target(name: "Addition"), .testTarget(name: "AdditionTests", dependencies: ["Addition"])])\n',
    },
    {
        "path": "Sources/Addition/Addition.swift",
        "content": "public func add(_ a: Int, _ b: Int) -> Int { a + b }\n",
    },
    {
        "path": "Tests/AdditionTests/AdditionTests.swift",
        "content": "import XCTest\n@testable import Addition\nfinal class AdditionTests: XCTestCase { func testAdd() { XCTAssertEqual(add(2, 3), 5) } }\n",
    },
    {"path": "README.md", "content": "A local Swift addition example; swift test validates it.\n"},
]


async def submit_native(
    manager: GoalManager,
    agent: str,
    files: list[dict[str, str]],
    *,
    state: str = "authoring",
    focus: list[str] | None = None,
    receive: bool = True,
) -> dict[str, Any]:
    job = await manager.agent_dispatcher.claim(agent)
    assert job
    value = {
        "schema_version": "1.0",
        "action": "continue",
        "native_validation": state,
        "message": "The example is complete and tested.",
        "plan": ["Create package, source, tests, README", "Validate the exact revision"],
        "files": copy.deepcopy(files),
        "checks": [],
        "runtime": "python",
        "run_instructions": "swift test",
        "focus_paths": focus or [],
        "base_revision_id": job["payload"]["base_revision_id"],
        "base_sha256": job["payload"]["base_sha256"],
    }
    accepted, changed = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="completed",
        result=value,
        error=None,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    assert changed
    if receive:
        await manager.on_job_result(accepted)
    return accepted


@pytest.mark.asyncio
async def test_four_native_revisions_progress_then_request_exact_native_validation(
    tmp_path: Path,
) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=12)
    goal_id = detail["goal"]["id"]
    for index in range(4):
        await submit_native(
            manager, agent, FILES[: index + 1], state="authoring" if index < 3 else "required"
        )
        current = await manager.get_goal(goal_id)
        assert current
        assert current["goal"]["status"] == ("running" if index < 3 else "waiting_permission")
        assert current["goal"]["current_phase"] == (
            "project_building" if index < 3 else "needs_user"
        )
        assert manager.project_applications
        preview = await manager.project_applications.get_project(goal_id)
        assert preview and preview["files"] == FILES[: index + 1]
        assert preview["sha256"] == project_digest(FILES[: index + 1])
        assert preview["state"] == ("building" if index < 3 else "needs_user")
        assert preview["checks"] == []
    assert await manager.agent_dispatcher.claim(agent) is None
    conversation = await manager.conversation_messages(goal_id)
    assert conversation["pending_question_id"] is None
    assert "complete and tested" not in str(conversation)
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM project_revisions")).fetchone() == (4,)
        assert await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone() == (0,)
        assert await (
            await db.execute("SELECT COUNT(*) FROM swift_project_validations")
        ).fetchone() == (0,)
        rows = await (await db.execute("SELECT snapshot_json FROM project_revisions")).fetchall()
        assert all(json.loads(row[0])["action"] == "continue" for row in rows)


@pytest.mark.asyncio
async def test_native_no_progress_and_repeated_reads_remain_bounded(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=16)
    goal_id = detail["goal"]["id"]
    await submit_native(manager, agent, FILES[:1])
    for _ in range(3):
        await submit_native(manager, agent, FILES[:1])
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["current_phase"] == "needs_user"
    assert await manager.agent_dispatcher.claim(agent) is None
    assert (await manager.conversation_messages(goal_id))["pending_question_id"] is None


@pytest.mark.asyncio
async def test_native_repeated_reads_pause_without_resetting_no_progress(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path, max_calls=16)
    goal_id = detail["goal"]["id"]
    await submit_native(manager, agent, FILES[:1])
    for _ in range(4):
        await submit_native(manager, agent, FILES[:1], focus=["Package.swift"])
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["current_phase"] == "needs_user"
    assert await manager.agent_dispatcher.claim(agent) is None
    assert (await manager.conversation_messages(goal_id))["pending_question_id"] is None


@pytest.mark.asyncio
async def test_native_required_stale_result_preserves_new_user_instruction(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    job = await submit_native(manager, agent, FILES[:1], state="required", receive=False)
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(
            message="Add subtraction before validation", client_message_id="subtract"
        ),
        actor_id="phone",
    )
    await manager.on_job_result(job)
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["status"] == "running"
    followup = await manager.agent_dispatcher.claim(agent)
    assert followup and followup["payload"]["native_validation"] == "required"
    assert any(
        item["content"] == "Add subtraction before validation"
        for item in followup["payload"]["conversation"]
    )


@pytest.mark.asyncio
async def test_cancelled_native_authoring_does_not_redispatch(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    job = await submit_native(manager, agent, FILES[:1], receive=False)
    await manager.cancel_goal(goal_id, actor_id="phone")
    await manager.on_job_result(job)
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["status"] == "cancelled"
    assert await manager.agent_dispatcher.claim(agent) is None


@pytest.mark.asyncio
async def test_removing_last_swift_file_retains_native_coverage_in_followup(tmp_path: Path) -> None:
    manager, _, agent = await _project(tmp_path, max_calls=10)
    await submit_native(manager, agent, FILES[:1])
    await submit_native(manager, agent, [])
    followup = await manager.agent_dispatcher.claim(agent)
    assert followup and followup["payload"]["files"] == []
    assert followup["payload"]["native_validation"] == "authoring"
    before = ProjectPayload.model_validate(followup["payload"])
    after = ProjectResult.model_validate(
        {
            "schema_version": "1.0",
            "action": "continue",
            "message": "Checks passed",
            "plan": [],
            "files": [],
            "checks": [
                {
                    "command": ["npm", "test"],
                    "status": "passed",
                    "exit_code": 0,
                    "output": "1 test",
                    "duration_ms": 1,
                }
            ],
            "run_instructions": "",
            "runtime": "python",
            "native_validation": "authoring",
            "base_revision_id": before.base_revision_id,
            "base_sha256": before.base_sha256,
        }
    )
    assert not has_project_progress(before, after)


@pytest.mark.asyncio
async def test_non_native_initial_payload_omits_new_optional_marker(tmp_path: Path) -> None:
    manager, _, agent = await _project(tmp_path)
    job = await manager.agent_dispatcher.claim(agent)
    assert job and "native_validation" not in job["payload"]
