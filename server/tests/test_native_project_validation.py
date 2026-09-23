from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.goal_manager import GoalManager
from app.services.goal_project import GoalProjectConflict
from app.services.project_contracts import ProjectResult, ProjectWriteArguments, project_digest
from app.services.project_progress import has_project_progress, project_progress_message
from app.services.project_validation import pause_native_validation_locked
from app.services.swarm_contracts import GoalMessageRequest
from tests.test_goal_project_runtime import _project, _result
from tests.test_project_progress import check, payload, result
from tests.test_project_publication import REQUESTER, prepared_project

SWIFT = {"path": "Hello.swift", "content": 'print("Hello")\n'}
FILES = [SWIFT, {"path": "README.md", "content": "A local Hello example."}]


@pytest.mark.parametrize(
    "path",
    [
        "Hello.swift",
        "Sources/Hello.SWIFT",
        "Package.swift",
        "Hello.xcodeproj/project.pbxproj",
        "Hello.XCWORKSPACE/contents.xcworkspacedata",
    ],
)
def test_non_native_checks_do_not_validate_native_files(path: str) -> None:
    files = [{**SWIFT, "path": path}, FILES[1]]
    before = payload(files=files, base_sha256=project_digest(files))
    after = result(files=files, checks=[check()], action="complete")
    # Historical schema 1.0 snapshots remain decodable, but are not native evidence.
    assert ProjectResult.model_validate_json(after.model_dump_json()).action == "complete"
    assert not has_project_progress(before, after)
    message = project_progress_message(before, after)
    assert "Validation Swift/iOS non prise en charge" in message
    assert "ready for review" not in message


def test_native_file_changes_remain_progress_without_claiming_validation() -> None:
    assert has_project_progress(payload(), result(files=FILES, checks=[check()]))
    assert "Validation Swift/iOS non prise en charge" in project_progress_message(
        payload(), result(files=FILES, checks=[check()])
    )


@pytest.mark.parametrize("command", [["npm", "test"], ["swift", "test"], ["xcodebuild", "test"]])
def test_command_name_is_not_native_runner_provenance(command: list[str]) -> None:
    before = payload(files=FILES, base_sha256=project_digest(FILES))
    after = result(files=FILES, checks=[check(command=command)], action="complete")
    assert not has_project_progress(before, after)
    assert "Validation Swift/iOS non prise en charge" in project_progress_message(before, after)


@pytest.mark.parametrize("path", ["README.md", "swift_notes.txt", "Hello.swift.txt"])
def test_prose_and_similar_non_native_paths_are_not_blocked(path: str) -> None:
    files = [{"path": path, "content": "Swift/iOS .xcodeproj xcodebuild all tests pass"}]
    before = payload(files=files, base_sha256=project_digest(files))
    after = result(files=files, checks=[check()])
    assert has_project_progress(before, after)
    assert "Validation Swift/iOS non prise en charge" not in project_progress_message(before, after)


async def native_result(
    manager: GoalManager, agent: str, *, action: str = "complete", receive: bool = True
) -> tuple[dict[str, Any], dict[str, Any]]:
    job = await manager.agent_dispatcher.claim(agent)
    assert job
    value = {
        "schema_version": "1.0",
        "action": action,
        "message": "Hello Swift compiles successfully. Which feature next?",
        "files": copy.deepcopy(FILES),
        "checks": [check(), check(command=["npm", "test"])],
        "plan": ["Create Hello", "Validate native application"],
        "run_instructions": "Open the project in Xcode.",
        "runtime": "python_node",
        "focus_paths": [],
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
    return accepted, value


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["complete", "continue", "clarify"])
async def test_native_iteration_pauses_immediately_preserving_evidence_and_no_question(
    tmp_path: Path,
    action: str,
) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    job, raw = await native_result(manager, agent, action=action)
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["current_phase"] == "needs_user"
    assert current["goal"]["status"] == "waiting_permission"
    assert "Validation Swift/iOS non prise en charge" in current["goal"]["evaluator_summary"]
    assert len(current["nodes"]) == 1
    assert await manager.agent_dispatcher.claim(agent) is None
    assert manager.project_applications
    preview = await manager.project_applications.get_project(goal_id)
    assert preview and preview["state"] == "needs_user"
    assert preview["files"] == raw["files"] and preview["checks"] == raw["checks"]
    assert preview["sha256"] == project_digest(raw["files"])
    conversation = await manager.conversation_messages(goal_id)
    assert conversation["pending_question_id"] is None
    assert "compiles successfully" not in str(conversation)
    await manager.on_job_result(job)
    await manager.reconcile()
    again = await manager.get_goal(goal_id)
    assert again and again["goal"]["model_call_count"] == current["goal"]["model_call_count"]
    assert len(again["nodes"]) == 1
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone() == (0,)
        assert await (await db.execute("SELECT COUNT(*) FROM project_revisions")).fetchone() == (1,)
        snapshot = await (
            await db.execute("SELECT snapshot_json FROM project_revisions")
        ).fetchone()
        assert snapshot and json.loads(snapshot[0])["action"] != "complete"
        stored_job = await (
            await db.execute("SELECT result_json FROM agent_jobs WHERE id=?", (job["id"],))
        ).fetchone()
        assert stored_job and json.loads(stored_job[0]) == raw


@pytest.mark.asyncio
async def test_native_stale_result_does_not_drop_queued_user_steering(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    job, _ = await native_result(manager, agent, receive=False)
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(message="Keep the Hello source for later", client_message_id="later"),
        actor_id="phone",
    )
    await manager.on_job_result(job)
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["status"] == "running"
    assert sum(node["status"] == "dispatched" for node in current["nodes"]) == 1
    followup = await manager.agent_dispatcher.claim(agent)
    assert followup and any(
        item["role"] == "user" and item["content"] == "Keep the Hello source for later"
        for item in followup["payload"]["conversation"]
    )


async def legacy_native_snapshot(manager: GoalManager, goal_id: str) -> tuple[str, str]:
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                "SELECT id,snapshot_json FROM project_revisions WHERE goal_run_id=?",
                (goal_id,),
            )
        ).fetchone()
        assert row
        snapshot = json.loads(row[1])
        snapshot["files"].append(copy.deepcopy(SWIFT))
        encoded = json.dumps(snapshot)
        digest = project_digest(snapshot["files"])
        await db.execute(
            "UPDATE project_revisions SET snapshot_json=?,sha256=? WHERE id=?",
            (encoded, digest, row[0]),
        )
        await db.commit()
    return encoded, digest


@pytest.mark.asyncio
async def test_legacy_complete_native_preview_is_read_only_and_apply_is_blocked(
    tmp_path: Path,
) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await _result(manager, agent, action="complete")
    original, digest = await legacy_native_snapshot(manager, goal_id)
    assert ProjectResult.model_validate_json(original).action == "complete"
    assert manager.project_applications
    service = manager.project_applications
    preview = await service.get_project(goal_id)
    assert preview and preview["state"] == "needs_user"
    assert preview["sha256"] == digest
    assert preview["checks"] == json.loads(original)["checks"]
    assert "Validation Swift/iOS non prise en charge" in preview["message"]
    with pytest.raises(GoalProjectConflict, match="Swift/iOS"):
        await service.apply(goal_id, preview["revision_id"], REQUESTER, digest)
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT snapshot_json FROM project_revisions")
        ).fetchone() == (original,)
        assert await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone() == (0,)
        assert await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone() == (0,)


@pytest.mark.asyncio
async def test_legacy_saved_native_revision_is_not_finalized_as_validated(tmp_path: Path) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    service = manager.project_applications
    assert service
    call = await service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"])
    original, digest = await legacy_native_snapshot(manager, goal_id)
    arguments = ProjectWriteArguments(
        project_id=preview["project_id"],
        revision_id=preview["revision_id"],
        sha256=digest,
        files=json.loads(original)["files"],
    )
    receipt = {
        "path": arguments.path,
        "sha256": digest,
        "files": len(arguments.files),
        "bytes": sum(len(file.content.encode("utf-8")) for file in arguments.files),
    }
    # Simulate a pre-upgrade persisted save receipt. No generated source is executed.
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE tool_calls SET status='completed',arguments_json=?,result_json=? WHERE id=?",
            (arguments.model_dump_json(), json.dumps(receipt), call["id"]),
        )
        await db.commit()
    assert await service.synchronize(call["id"]) == {goal_id}
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["current_phase"] == "needs_user"
    assert "checks passed" not in current["nodes"][0]["result_summary"]
    assert (await engine.get(call["id"]))["status"] == "completed"
    shown = await service.get_project(goal_id)
    assert shown and shown["state"] == "applied"
    assert "Validation Swift/iOS non prise en charge" in shown["message"]
    assert await service.synchronize(call["id"]) == set()
    assert (await manager.conversation_messages(goal_id))["pending_question_id"] is None
    assert not list(engine.workspace_root.iterdir())


@pytest.mark.asyncio
async def test_evaluator_cannot_complete_legacy_native_snapshot_with_python_receipts(
    tmp_path: Path,
) -> None:
    manager, _, goal_id, _ = await prepared_project(tmp_path)
    await legacy_native_snapshot(manager, goal_id)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE plan_nodes SET status='completed' WHERE goal_run_id=?", (goal_id,))
        await db.execute(
            "UPDATE goal_runs SET status='running',current_phase='dispatching' WHERE id=?",
            (goal_id,),
        )
        await db.commit()
    await manager._evaluate_if_quiescent(goal_id)
    current = await manager.get_goal(goal_id)
    assert current and current["goal"]["current_phase"] == "needs_user"
    assert current["goal"]["status"] == "waiting_permission"
    assert current["goal"]["evaluator_status"] != "done"
    assert (await manager.conversation_messages(goal_id))["pending_question_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "cancelled"])
async def test_late_native_save_does_not_rewrite_terminal_goal_or_conversation(
    tmp_path: Path,
    status: str,
) -> None:
    manager, _, goal_id, preview = await prepared_project(tmp_path)
    service = manager.project_applications
    assert service
    call = await service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"])
    await legacy_native_snapshot(manager, goal_id)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE goal_runs SET status=? WHERE id=?", (status, goal_id))
        await db.execute(
            "UPDATE tasks SET status=? WHERE id=(SELECT root_task_id FROM goal_runs WHERE id=?)",
            (status, goal_id),
        )
        await db.execute("UPDATE tool_calls SET status='completed' WHERE id=?", (call["id"],))
        await db.commit()
        before_goal = await (
            await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_id,))
        ).fetchone()
        before_root = await (
            await db.execute(
                "SELECT * FROM tasks WHERE id=(SELECT root_task_id FROM goal_runs WHERE id=?)",
                (goal_id,),
            )
        ).fetchone()
        before_messages = await (await db.execute("SELECT * FROM goal_messages")).fetchall()
        # Defense in depth even if a future caller reaches the helper after termination.
        await db.execute("BEGIN IMMEDIATE")
        await pause_native_validation_locked(db, goal_id, now=manager._now())
        await db.commit()
    assert await service.synchronize(call["id"]) == set()
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await (await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_id,))).fetchone()
            == before_goal
        )
        assert (
            await (
                await db.execute(
                    "SELECT * FROM tasks WHERE id=(SELECT root_task_id FROM goal_runs WHERE id=?)",
                    (goal_id,),
                )
            ).fetchone()
            == before_root
        )
        assert await (await db.execute("SELECT * FROM goal_messages")).fetchall() == before_messages
