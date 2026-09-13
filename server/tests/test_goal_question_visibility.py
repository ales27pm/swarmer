from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from app.services.goal_conversation import GoalConversationConflict, GoalConversationService
from app.services.swarm_contracts import GoalCreateRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan


async def _question_goal(db_path: Path) -> tuple[GoalConversationService, str, str]:
    manager = await _manager(db_path, _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Build a Python CRM"), actor_id="phone"
    )
    now = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """UPDATE goal_runs SET status='waiting_permission',current_phase='needs_user',
            paused_at=? WHERE id=?""",
            (now, goal["id"]),
        )
        question = await GoalConversationService.assistant_locked(
            db, goal["id"], "Which CRM features?", question=True, now=now
        )
        await db.commit()
    return GoalConversationService(db_path), str(goal["id"]), question


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "phase"),
    [
        ("running", "dispatching"),
        ("running", "needs_user"),
        ("waiting_permission", "project_review"),
        ("waiting_permission", "evaluator_retry_required"),
        ("failed", "needs_user"),
    ],
)
async def test_historical_question_is_not_pending_or_replyable_outside_question_wait(
    tmp_path: Path, status: str, phase: str
) -> None:
    service, goal_id, question = await _question_goal(tmp_path / "state.db")
    before = await service.messages(goal_id)
    assert before["pending_question_id"] == question
    async with aiosqlite.connect(service.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET status=?,current_phase=? WHERE id=?", (status, phase, goal_id)
        )
        await db.commit()
    after = await service.messages(goal_id)
    assert after["messages"] == before["messages"]
    assert after["pending_question_id"] is None
    with pytest.raises(GoalConversationConflict, match="no longer current"):
        await service.append(
            goal_id,
            message="Clients, quotes, email, calendar",
            client_message_id="stale-screen-reply",
            reply_to_message_id=question,
            actor_id="phone",
        )
    assert (await service.messages(goal_id))["messages"] == before["messages"]


@pytest.mark.asyncio
async def test_new_question_after_replan_can_be_answered_without_rewriting_history(
    tmp_path: Path,
) -> None:
    service, goal_id, previous = await _question_goal(tmp_path / "state.db")
    async with aiosqlite.connect(service.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET status='running',current_phase='dispatching' WHERE id=?",
            (goal_id,),
        )
        await db.commit()
    assert (await service.messages(goal_id))["pending_question_id"] is None
    async with aiosqlite.connect(service.db_path) as db:
        now = datetime.now(UTC).isoformat()
        await db.execute(
            """UPDATE goal_runs SET status='waiting_permission',current_phase='needs_user',
            paused_at=? WHERE id=?""",
            (now, goal_id),
        )
        current = await GoalConversationService.assistant_locked(
            db,
            goal_id,
            "Which existing customer dataset should be imported?",
            question=True,
            now=now,
        )
        await db.commit()
    assert (await service.messages(goal_id))["pending_question_id"] == current
    with pytest.raises(GoalConversationConflict, match="no longer current"):
        await service.append(
            goal_id,
            message="Features already supplied",
            client_message_id="old-reply",
            reply_to_message_id=previous,
            actor_id="phone",
        )
    accepted = await service.append(
        goal_id,
        message="Use an empty database",
        client_message_id="new-reply",
        reply_to_message_id=current,
        actor_id="phone",
    )
    # Network retry of an already accepted answer remains idempotent after resume.
    assert (
        await service.append(
            goal_id,
            message="Use an empty database",
            client_message_id="new-reply",
            reply_to_message_id=current,
            actor_id="phone",
        )
        == accepted
    )
    assert (await service.messages(goal_id))["pending_question_id"] is None
    async with aiosqlite.connect(service.db_path) as db:
        assert await (
            await db.execute(
                "SELECT answered_by_message_id FROM goal_messages WHERE id=?", (previous,)
            )
        ).fetchone() == (None,)
        assert (
            await (
                await db.execute(
                    "SELECT answered_by_message_id FROM goal_messages WHERE id=?", (current,)
                )
            ).fetchone()
        )[0] is not None
        assert await (
            await db.execute(
                "SELECT count(*) FROM goal_messages WHERE client_message_id='new-reply'"
            )
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_running_guidance_does_not_mark_historical_question_answered(tmp_path: Path) -> None:
    service, goal_id, question = await _question_goal(tmp_path / "state.db")
    async with aiosqlite.connect(service.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET status='running',current_phase='dispatching' WHERE id=?",
            (goal_id,),
        )
        await db.commit()
    await service.append(
        goal_id,
        message="Also support CSV exports",
        client_message_id="steering",
        reply_to_message_id=None,
        actor_id="phone",
    )
    async with aiosqlite.connect(service.db_path) as db:
        assert await (
            await db.execute(
                "SELECT answered_by_message_id FROM goal_messages WHERE id=?", (question,)
            )
        ).fetchone() == (None,)
    assert (await service.messages(goal_id))["pending_question_id"] is None
