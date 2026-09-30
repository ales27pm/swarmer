from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.services.context_builder import safe_context_text
from app.services.goal_conversation import GoalConversationService
from app.services.goal_limits import active_runtime_seconds, runtime_expired
from app.services.goal_manager import GoalManagerConflict
from app.services.planner_continuation_context import continuation_cards
from app.services.project_context import ProjectContextService
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
        project_id = (
            await (
                await db.execute(
                    "SELECT project_id FROM goal_project_links WHERE goal_run_id=?", (goal["id"],)
                )
            ).fetchone()
        )[0]
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
        ).fetchone() == (project_id,)
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


@pytest.mark.asyncio
async def test_model_budget_exhaustion_auto_continues_linked_run(tmp_path: Path) -> None:
    manager = await _manager(
        tmp_path / "state.db",
        _worker_plan(),
        auto_continue_on_model_budget_exhausted=True,
    )
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository", max_model_calls=1), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    await manager._terminate_goal(
        goal["id"], status="budget_exhausted", reason="goal model call budget exhausted"
    )

    history = await manager.conversation_messages(goal["id"])
    new_id = history["active_goal_id"]
    assert new_id != goal["id"]
    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
    checkpoint = history["messages"][-1]
    assert checkpoint["goal_run_id"] == new_id
    assert checkpoint["content"]

    project_context = ProjectContextService(manager.db_path)
    snapshot = await project_context.refresh(new_id)
    assert [item["text"] for item in snapshot["requirements"]] == ["Inspect the repository"]
    assert {item["source_id"] for item in snapshot["proposals"]} == {checkpoint["id"]}
    source = await project_context.source(new_id, checkpoint["id"])
    assert source["role"] == "assistant"
    assert source["content"] == safe_context_text(
        checkpoint["content"], max_chars=max(4_000, len(checkpoint["content"]))
    )
    cards = await continuation_cards(manager.db_path, new_id)
    assert any(
        card.kind == "continuation_checkpoint"
        and card.provenance_ids == (checkpoint["id"],)
        and card.summary == safe_context_text(checkpoint["content"], max_chars=4_000)
        for card in cards
    )

    old = await manager.graph.get_goal(goal["id"])
    assert old is not None and old["status"] == "budget_exhausted"
    continued = await manager.get_goal(new_id)
    assert continued is not None
    assert continued["goal"]["status"] == "running"
    assert continued["goal"]["model_call_count"] == 1
    assert continued["goal"]["max_model_calls"] == 1
    assert continued["nodes"]
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute(
                "SELECT parent_goal_id FROM goal_conversation_links WHERE goal_run_id=?", (new_id,)
            )
        ).fetchone() == (goal["id"],)


@pytest.mark.asyncio
async def test_auto_continuation_reconciles_after_crash_between_append_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = await _manager(
        tmp_path / "state.db", _worker_plan(), auto_continue_on_model_budget_exhausted=True
    )
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository", max_model_calls=1), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())

    async def crash_after_append(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated restart after durable continuation")

    monkeypatch.setattr(manager, "_resume_pending_conversation", crash_after_append)
    await manager._terminate_goal(
        goal["id"], status="budget_exhausted", reason="goal model call budget exhausted"
    )
    history = await manager.conversation_messages(goal["id"])
    new_id = history["active_goal_id"]
    assert new_id != goal["id"]
    pending = await manager.graph.get_goal(new_id)
    assert pending is not None and pending["status"] == "planning"
    assert pending["pending_message_revision"] > 0

    restarted = await _manager(
        manager.db_path, _worker_plan(), auto_continue_on_model_budget_exhausted=True
    )
    assert await restarted.reconcile() >= 1
    continued = await restarted.get_goal(new_id)
    assert continued is not None and continued["goal"]["status"] == "running"
    assert continued["nodes"]
    assert (await restarted.conversation_messages(goal["id"]))["active_goal_id"] == new_id


@pytest.mark.asyncio
async def test_auto_continuation_reconciles_after_crash_before_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = await _manager(
        tmp_path / "state.db", _worker_plan(), auto_continue_on_model_budget_exhausted=True
    )
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository", max_model_calls=1), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())

    async def crash_before_append(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated restart before durable continuation")

    monkeypatch.setattr(manager.conversations, "append", crash_before_append)
    await manager._terminate_goal(
        goal["id"], status="budget_exhausted", reason="goal model call budget exhausted"
    )
    pending = await manager.graph.get_goal(goal["id"])
    assert pending is not None and pending["current_phase"] == "auto_continuation_pending"
    assert (await manager.conversation_messages(goal["id"]))["active_goal_id"] == goal["id"]

    restarted = await _manager(
        manager.db_path, _worker_plan(), auto_continue_on_model_budget_exhausted=True
    )
    assert await restarted.reconcile() >= 1
    new_id = (await restarted.conversation_messages(goal["id"]))["active_goal_id"]
    assert new_id != goal["id"]
    await restarted.reconcile()
    assert (await restarted.conversation_messages(goal["id"]))["active_goal_id"] == new_id
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute(
                "SELECT COUNT(*) FROM goal_conversation_links WHERE parent_goal_id=?",
                (goal["id"],),
            )
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_auto_continuation_recovery_cancels_old_child_after_terminal_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = await _manager(
        tmp_path / "state.db", _worker_plan(), auto_continue_on_model_budget_exhausted=True
    )
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository", max_model_calls=1), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    nodes = await manager.graph.list_nodes(goal["id"])
    child_id = nodes[0]["task_id"]
    assert child_id

    async def crash_before_projection(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated crash before terminal projection")

    monkeypatch.setattr(manager, "_finalize_terminal_goal", crash_before_projection)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await manager._terminate_goal(
            goal["id"], status="budget_exhausted", reason="goal model call budget exhausted"
        )
    before = await manager.state_service.get_task(child_id)
    assert before is not None and before.status.value != "cancelled"

    restarted = await _manager(
        manager.db_path, _worker_plan(), auto_continue_on_model_budget_exhausted=True
    )
    assert await restarted.reconcile() >= 1
    child = await restarted.state_service.get_task(child_id)
    assert child is not None and child.status.value == "cancelled"
    history = await restarted.conversation_messages(goal["id"])
    assert history["active_goal_id"] != goal["id"]


@pytest.mark.asyncio
async def test_auto_continuation_stops_after_repeated_model_budgets_without_progress(
    tmp_path: Path,
) -> None:
    manager = await _manager(
        tmp_path / "state.db", _worker_plan(), auto_continue_on_model_budget_exhausted=True
    )
    first = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository", max_model_calls=1), actor_id="phone"
    )
    await manager.start_goal(first["id"], GoalStartRequest())

    run_id = first["id"]
    for _ in range(3):
        await manager._terminate_goal(
            run_id, status="budget_exhausted", reason="goal model call budget exhausted"
        )
        run_id = (await manager.conversation_messages(first["id"]))["active_goal_id"]

    assert run_id != first["id"]
    last = await manager.graph.get_goal(run_id)
    assert last is not None and last["status"] == "budget_exhausted"
    assert last["current_phase"] == "auto_continuation_stopped"
    history = await manager.conversation_messages(first["id"])
    assert [message["role"] for message in history["messages"]] == [
        "user",
        "assistant",
        "assistant",
    ]
    assert await manager.reconcile() == 0
    assert (await manager.conversation_messages(first["id"]))["active_goal_id"] == run_id


@pytest.mark.asyncio
async def test_auto_checkpoint_keeps_completed_work_when_recent_nodes_were_cancelled(
    tmp_path: Path,
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository"), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    async with aiosqlite.connect(manager.db_path) as db:
        completed = await (
            await db.execute("SELECT id FROM plan_nodes WHERE goal_run_id=? LIMIT 1", (goal["id"],))
        ).fetchone()
        assert completed is not None
        await db.execute(
            "UPDATE plan_nodes SET status='completed',result_summary='README inspected' WHERE id=?",
            (completed[0],),
        )
        for index in range(12):
            await db.execute(
                """INSERT INTO plan_nodes(id,goal_run_id,node_type,title,objective,status,
                priority,depends_on_json,expected_output,created_at,updated_at)
                SELECT ?,goal_run_id,node_type,'Cancelled task','Unfinished task','cancelled',
                priority,depends_on_json,expected_output,created_at,?
                FROM plan_nodes WHERE id=?""",
                (f"cancelled_{index}", f"2099-01-01T00:00:{index:02d}+00:00", completed[0]),
            )
        await db.commit()
    checkpoint = await manager._auto_continuation_checkpoint(goal["id"])
    assert checkpoint is not None
    assert f"Node {completed[0]} [completed], reported: README inspected" in checkpoint


@pytest.mark.asyncio
async def test_auto_continuation_preserves_long_user_instructions_when_checkpoint_cannot_fit(
    tmp_path: Path,
) -> None:
    manager = await _manager(
        tmp_path / "state.db", _worker_plan(), auto_continue_on_model_budget_exhausted=True
    )
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository", max_model_calls=1), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    instructions = [f"Instruction {index}: " + chr(65 + index) * 1_000 for index in range(3)]
    for index, instruction in enumerate(instructions):
        await manager.reply_goal(
            goal["id"],
            GoalMessageRequest(message=instruction, client_message_id=f"long-{index}"),
            actor_id="phone",
        )
    before = await manager.conversation_messages(goal["id"])
    assert [message["content"] for message in before["messages"]][1:] == instructions

    await manager._terminate_goal(
        goal["id"], status="budget_exhausted", reason="goal model call budget exhausted"
    )
    stopped = await manager.graph.get_goal(goal["id"])
    assert stopped is not None and stopped["status"] == "budget_exhausted"
    assert stopped["current_phase"] == "auto_continuation_stopped"
    assert "cannot preserve all user instructions" in stopped["failure_reason"]
    after = await manager.conversation_messages(goal["id"])
    assert after["active_goal_id"] == goal["id"]
    assert after["messages"] == before["messages"]
