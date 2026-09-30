from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from app.services.goal_manager import GoalManagerConflict
from app.services.swarm_contracts import GoalCreateRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan


@pytest.mark.asyncio
async def test_chat_goal_preserves_context_without_execution_and_retries_once(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("INSERT INTO conversations VALUES('chat_one','CRM','now','now')")
        for identifier, role, content in [
            ("first", "user", "Un CRM local en français."),
            ("second", "assistant", "Je propose SQLite, à vérifier."),
            ("third", "system", "Private implementation event"),
        ]:
            await db.execute(
                "INSERT INTO messages(id,conversation_id,role,content,created_at) VALUES(?,'chat_one',?,?,'now')",
                (identifier, role, content),
            )
        await db.commit()
    request = GoalCreateRequest(
        objective="Compare SQLite et JSON sans créer de fichiers.",
        conversation_id="chat_one",
        client_request_id="request_one",
        autonomy_profile="autonomous",
    )
    first, second = await asyncio.gather(
        manager.create_goal(request, actor_id="phone"),
        manager.create_goal(request, actor_id="phone"),
    )
    assert first["id"] == second["id"]
    assert first["status"] == "planning" and first["model_call_count"] == 0
    messages = (await manager.conversation_messages(first["id"]))["messages"]
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Un CRM local en français."),
        ("assistant", "Je propose SQLite, à vérifier."),
        ("user", request.objective),
    ]
    async with aiosqlite.connect(manager.db_path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM goal_runs")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM messages")).fetchone())[0] == 4
        assert (
            await (
                await db.execute(
                    "SELECT conversation_id FROM tasks WHERE id=?", (first["root_task_id"],)
                )
            ).fetchone()
        )[0] == "chat_one"
    with pytest.raises(GoalManagerConflict, match="different request"):
        await manager.create_goal(
            request.model_copy(update={"objective": "Different goal"}), actor_id="phone"
        )
    restarted = await _manager(manager.db_path, _worker_plan())
    assert (await restarted.create_goal(request, actor_id="phone"))["id"] == first["id"]


@pytest.mark.asyncio
async def test_missing_chat_does_not_leave_orphan_goal_or_task(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    with pytest.raises(GoalManagerConflict, match="conversation not found"):
        await manager.create_goal(
            GoalCreateRequest(
                objective="Research SQLite", conversation_id="missing", client_request_id="one"
            ),
            actor_id="phone",
        )
    async with aiosqlite.connect(manager.db_path) as db:
        for table in ("goal_runs", "tasks", "idempotency_receipts"):
            assert (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_goal_request_keys_are_actor_scoped(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    request = GoalCreateRequest(objective="Research SQLite", client_request_id="one")
    first = await manager.create_goal(request, actor_id="phone")
    second = await manager.create_goal(request, actor_id="another-device")
    assert first["id"] != second["id"]
