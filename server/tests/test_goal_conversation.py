from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.services.goal_conversation import GoalConversationService
from app.services.goal_limits import active_runtime_seconds, runtime_expired
from app.services.goal_manager import GoalManagerConflict
from app.services.state_service import StateService
from app.services.swarm_contracts import GoalCreateRequest, GoalMessageRequest, GoalStartRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan


def test_human_wait_excludes_idle_time_but_not_work_already_spent() -> None:
    now = datetime.now(UTC)
    goal = {
        "started_at": (now - timedelta(days=2, seconds=20)).isoformat(),
        "paused_at": (now - timedelta(days=2)).isoformat(),
        "paused_seconds": 0,
        "max_runtime_seconds": 30,
    }
    assert active_runtime_seconds(goal, now=now) == 20
    assert not runtime_expired(goal, now=now)
    goal["max_runtime_seconds"] = 10
    assert runtime_expired(goal, now=now)


@pytest.mark.asyncio
async def test_reply_is_durable_idempotent_and_fences_old_model_context(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository"), actor_id="phone"
    )
    call = await manager._reserve_model_call(
        goal["id"],
        role="planner",
        context_id=None,
        input_digest="a" * 64,
        provider_source="test",
    )
    reply = GoalMessageRequest(message="Include the README", client_message_id="reply-one")
    first, second = await asyncio.gather(
        manager.reply_goal(goal["id"], reply, actor_id="phone"),
        manager.reply_goal(goal["id"], reply, actor_id="phone"),
    )
    assert first["goal"]["id"] == second["goal"]["id"] == goal["id"]
    restarted = await _manager(manager.db_path, _worker_plan())
    history = await restarted.conversation_messages(goal["id"])
    assert [m["content"] for m in history["messages"]] == [
        "Inspect the repository",
        "Include the README",
    ]
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute(
                "SELECT status,error_category FROM goal_model_calls WHERE id=?", (call,)
            )
        ).fetchone() == ("failed", "conversation_changed")
    with pytest.raises(GoalManagerConflict, match="different reply"):
        await manager.reply_goal(
            goal["id"], reply.model_copy(update={"message": "Different"}), actor_id="phone"
        )
    with pytest.raises(GoalManagerConflict, match="conversation changed"):
        await manager._reserve_model_call(
            goal["id"],
            role="planner",
            context_id=None,
            input_digest="b" * 64,
            provider_source="test",
            conversation_revision=0,
        )


@pytest.mark.asyncio
async def test_question_answer_resumes_once_and_preserves_budget(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository", max_runtime_seconds=30),
        actor_id="phone",
    )
    now = datetime.now(UTC)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            """UPDATE goal_runs SET status='waiting_permission',current_phase='needs_user',
            started_at=?,paused_at=?,model_call_count=3,step_count=1,replan_count=1 WHERE id=?""",
            (
                (now - timedelta(days=2, seconds=10)).isoformat(),
                (now - timedelta(days=2)).isoformat(),
                goal["id"],
            ),
        )
        question = await GoalConversationService.assistant_locked(
            db, goal["id"], "Which interface?", question=True, now=now.isoformat()
        )
        await db.commit()
    assert await manager.reconcile() == 0
    assert (await manager.conversation_messages(goal["id"]))["pending_question_id"] == question
    answered = await manager.reply_goal(
        goal["id"],
        GoalMessageRequest(
            message="A web interface", client_message_id="answer", reply_to_message_id=question
        ),
        actor_id="phone",
    )
    assert answered["goal"]["status"] == "running"
    assert [
        answered["goal"][key] for key in ("model_call_count", "step_count", "replan_count")
    ] == [3, 1, 1]
    assert (await manager.conversation_messages(goal["id"]))["pending_question_id"] is None
    current = await manager.graph.get_goal(goal["id"])
    assert current is not None and 10 <= active_runtime_seconds(current) < 12
    with pytest.raises(GoalManagerConflict, match="no longer current"):
        await manager.reply_goal(
            goal["id"],
            GoalMessageRequest(
                message="CLI", client_message_id="old-answer", reply_to_message_id=question
            ),
            actor_id="phone",
        )


@pytest.mark.asyncio
async def test_terminal_reply_creates_one_linked_run_without_resetting_history(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository", max_model_calls=7), actor_id="phone"
    )
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET status='budget_exhausted',model_call_count=7,step_count=2 WHERE id=?",
            (goal["id"],),
        )
        await db.execute("INSERT INTO coding_projects VALUES('project_one','now','now')")
        await db.execute("INSERT INTO goal_project_links VALUES(?,'project_one')", (goal["id"],))
        await db.commit()
    request = GoalMessageRequest(message="Continue with exports", client_message_id="followup")
    first, second = await asyncio.gather(
        manager.reply_goal(goal["id"], request, actor_id="phone"),
        manager.reply_goal(goal["id"], request, actor_id="phone"),
    )
    new_id = first["goal"]["id"]
    assert new_id != goal["id"] and new_id == second["goal"]["id"]
    assert first["goal"]["max_model_calls"] == 7 and first["goal"]["model_call_count"] == 0
    old = await manager.graph.get_goal(goal["id"])
    assert old is not None and (old["status"], old["model_call_count"], old["step_count"]) == (
        "budget_exhausted",
        7,
        2,
    )
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute(
                "SELECT project_id FROM goal_project_links WHERE goal_run_id=?", (new_id,)
            )
        ).fetchone() == ("project_one",)
    history = await manager.conversation_messages(goal["id"])
    assert history["active_goal_id"] == new_id and len(history["messages"]) == 2


@pytest.mark.asyncio
async def test_running_job_keeps_reply_queued_until_iteration_boundary(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository"), actor_id="phone"
    )
    started = await manager.start_goal(goal["id"], GoalStartRequest())
    replied = await manager.reply_goal(
        goal["id"],
        GoalMessageRequest(message="Check README too", client_message_id="steer"),
        actor_id="phone",
    )
    await manager.reconcile()
    assert replied["nodes"][0]["worker_job_id"] == started["nodes"][0]["worker_job_id"]
    current = await manager.graph.get_goal(goal["id"])
    assert current is not None and current["pending_message_revision"] == 1
    assert current["model_call_count"] == 1


@pytest.mark.asyncio
async def test_chat_context_uses_recent_window_in_chronological_order(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    conversation, _ = await state.append_chat_user_message("initial", None, "phone")
    for i in range(45):
        await state.append_chat_user_message(f"message {i}", conversation, "phone")
    messages = await state.list_messages(conversation, 40)
    assert len(messages) == 40
    assert messages[0]["content"] == "message 5"
    assert messages[-1]["content"] == "message 44"


@pytest.mark.asyncio
async def test_late_planner_timeout_cannot_terminate_newer_reply(tmp_path: Path) -> None:
    from app.services.swarm_contracts import PlannerSource

    entered, release = asyncio.Event(), asyncio.Event()

    class SlowPlanner:
        source = PlannerSource.TEST

        async def propose(self, context: object) -> object:
            entered.set()
            await release.wait()
            raise TimeoutError("old request timed out")

    manager = await _manager(tmp_path / "state.db", _worker_plan())
    manager.planner = SlowPlanner()  # type: ignore[assignment]
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository"), actor_id="phone"
    )
    pending = asyncio.create_task(manager.start_goal(goal["id"], GoalStartRequest()))
    await asyncio.wait_for(entered.wait(), timeout=3)
    await manager.reply_goal(
        goal["id"],
        GoalMessageRequest(message="Include README", client_message_id="during-timeout"),
        actor_id="phone",
    )
    release.set()
    with pytest.raises(GoalManagerConflict, match="fenced"):
        await asyncio.wait_for(pending, timeout=3)
    current = await manager.graph.get_goal(goal["id"])
    assert current is not None and current["status"] == "planning"
    assert current["pending_message_revision"] == 1
    assert current["model_call_count"] == 1
