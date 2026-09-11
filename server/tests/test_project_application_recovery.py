from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from app.services.approval_gateway import ApprovalGateway
from app.services.execution_engine import ExecutionConflict
from app.services.goal_project import GoalProjectConflict
from app.services.swarm_contracts import GoalMessageRequest
from tests.test_project_publication import REQUESTER, prepared_project


async def _interrupted_apply(
    manager: Any, engine: Any, goal_id: str, preview: dict[str, Any]
) -> str:
    with (
        patch.object(engine, "create_tool_call", new=AsyncMock(side_effect=OSError("interrupted"))),
        pytest.raises(OSError),
    ):
        await manager.project_applications.apply(
            goal_id, preview["revision_id"], REQUESTER, preview["sha256"]
        )
    return (await manager.project_applications.application_task_ids(goal_id))[0]


async def _append_reply(manager: Any, goal_id: str) -> None:
    await manager.conversations.append(
        goal_id,
        message="Add customer management to this application",
        client_message_id="post-interruption-reply",
        reply_to_message_id=None,
        actor_id=REQUESTER.id,
    )


async def _assert_resumed(manager: Any, goal_id: str, child_id: str) -> None:
    goal = await manager.graph.get_goal(goal_id)
    assert goal and goal["pending_message_revision"] == 0
    nodes = await manager.graph.list_nodes(goal_id)
    assert len(nodes) == 2 and sum(n["status"] == "dispatched" for n in nodes) == 1
    assert goal["model_call_count"] == 3 and goal["step_count"] == 2
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (child_id,))
        ).fetchone() == ("cancelled",)
        assert await (
            await db.execute("SELECT COUNT(*) FROM tool_calls WHERE task_id=?", (child_id,))
        ).fetchone() == (0,)
        assert await (
            await db.execute("SELECT COUNT(*) FROM approvals WHERE task_id=?", (child_id,))
        ).fetchone() == (0,)
        assert await (
            await db.execute(
                "SELECT apply_task_id FROM project_revisions WHERE goal_run_id=?", (goal_id,)
            )
        ).fetchone() == (None,)
        audit = await (
            await db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='goal.project.review_superseded' AND task_id=?",
                (child_id,),
            )
        ).fetchall()
    assert len(audit) == 1 and json.loads(audit[0][0])["apply_task_id"] == child_id


@pytest.mark.asyncio
async def test_interrupted_review_then_reply_cancels_only_unused_child(tmp_path: Path) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    child_id = await _interrupted_apply(manager, engine, goal_id, preview)
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(
            message="Add customer management", client_message_id="reply-after-crash"
        ),
        actor_id=REQUESTER.id,
    )
    await manager.reconcile(limit=1)
    await _assert_resumed(manager, goal_id, child_id)
    assert not list(engine.workspace_root.iterdir())


@pytest.mark.asyncio
async def test_recovery_cannot_create_approval_after_reply_was_durably_accepted(
    tmp_path: Path,
) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    child_id = await _interrupted_apply(manager, engine, goal_id, preview)
    # Simulate restart after accepting the message but before maintenance runs.
    await _append_reply(manager, goal_id)
    with pytest.raises(GoalProjectConflict, match="no longer accepting"):
        await manager.project_applications.apply(
            goal_id, preview["revision_id"], REQUESTER, preview["sha256"]
        )
    await manager.reconcile(limit=1)
    await _assert_resumed(manager, goal_id, child_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_before_create", [False, True])
async def test_reply_between_service_review_and_executor_claim_cannot_create_stale_approval(
    tmp_path: Path, resume_before_create: bool
) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine.create_tool_call

    async def delayed(**kwargs: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return await original(**kwargs)

    with patch.object(engine, "create_tool_call", new=delayed):
        applying = asyncio.create_task(
            manager.project_applications.apply(
                goal_id, preview["revision_id"], REQUESTER, preview["sha256"]
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        child_id = (await manager.project_applications.application_task_ids(goal_id))[0]
        await _append_reply(manager, goal_id)
        if resume_before_create:
            await manager.reconcile(limit=1)
        release.set()
        with pytest.raises(ExecutionConflict):
            await applying
    if not resume_before_create:
        await manager.reconcile(limit=1)
    await _assert_resumed(manager, goal_id, child_id)
    assert not list(engine.workspace_root.iterdir())


@pytest.mark.asyncio
async def test_existing_approval_keeps_queued_steering_until_publication_finishes(
    tmp_path: Path,
) -> None:
    manager, engine, goal_id, preview = await prepared_project(tmp_path)
    service = manager.project_applications
    call = await service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"])
    await _append_reply(manager, goal_id)
    assert await manager.reconcile(limit=1) == 0
    assert len(await manager.graph.list_nodes(goal_id)) == 1
    assert (await service.apply(goal_id, preview["revision_id"], REQUESTER, preview["sha256"]))[
        "id"
    ] == call["id"]
    await ApprovalGateway(manager.db_path).decide(
        call["approval_id"], "approve", actor_id=REQUESTER.id
    )
    completed = await engine.execute(call["id"])
    assert completed["status"] == "completed"
    await service.synchronize(call["id"])
    await manager.reconcile(limit=1)
    assert len(await manager.graph.list_nodes(goal_id)) == 2
    assert (await engine.get(call["id"]))["status"] == "completed"
