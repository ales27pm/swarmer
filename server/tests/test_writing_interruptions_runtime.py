from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.services.agent_dispatcher import AgentDispatchConflict
from app.services.swarm_contracts import GoalMessageRequest
from tests.test_goal_writing import RESULT, done
from tests.test_writing_declined_runtime import DECLINED, active, submit

CLARIFICATION = {
    **DECLINED,
    "outcome": "needs_clarification",
    "question": "Le CRM doit-il fonctionner sur un seul ordinateur ou être partagé entre utilisateurs ?",
    "text": "Le choix dépend du nombre d’utilisateurs simultanés.",
    "summary": "Préciser le nombre d’utilisateurs.",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [[], {}, 4, "unknown"])
async def test_malformed_outcome_is_rejected_without_server_error(
    tmp_path: Path, outcome: object
) -> None:
    manager, _, _, job = await active(tmp_path)
    with pytest.raises(AgentDispatchConflict, match="invalid_writing_result"):
        await submit(manager, job, result={**DECLINED, "outcome": outcome})
    assert (await manager.agent_dispatcher.get_job(job["id"]))["status"] == "claimed"


@pytest.mark.asyncio
async def test_clarification_waits_for_correlated_reply_without_evaluator_or_repeat(
    tmp_path: Path,
) -> None:
    manager, evaluator, goal_id, job = await active(tmp_path)
    recorded, _ = await submit(manager, job, result=CLARIFICATION)
    assert recorded["error"] == "writing_needs_clarification"
    detail = await manager.on_job_result(recorded)
    assert detail["goal"]["status"] == "waiting_permission"
    assert detail["goal"]["current_phase"] == "needs_user"
    assert evaluator.contexts == []
    history = await manager.conversation_messages(goal_id)
    assert history["pending_question_id"]
    assert CLARIFICATION["question"] in history["messages"][-1]["content"]
    assert "refus" not in history["messages"][-1]["content"]
    await manager.on_job_result(recorded)
    await manager.reconcile()
    assert (await manager.conversation_messages(goal_id)) == history
    before = await manager.graph.get_goal(goal_id)
    resumed = await manager.reply_goal(
        goal_id,
        GoalMessageRequest(
            message="Un seul ordinateur.",
            client_message_id="answer",
            reply_to_message_id=history["pending_question_id"],
        ),
        actor_id="phone",
    )
    assert resumed["goal"]["status"] == "running"
    assert resumed["goal"]["model_call_count"] == before["model_call_count"]
    assert (await manager.conversation_messages(goal_id))["pending_question_id"] is None
    # Answering must enable actual completion, not leave the earlier failed
    # clarification permanently blocking the evaluator's completion gate.
    manager.evaluator = done()
    await manager.reconcile()
    successor = await manager.agent_dispatcher.claim(job["claimed_by"])
    assert successor is not None and successor["id"] != job["id"]
    assert successor["payload"]["conversation"][-1]["content"] == "Un seul ordinateur."
    delivered, _ = await submit(manager, successor, status="completed", result=RESULT)
    final = await manager.on_job_result(delivered)
    assert final["goal"]["status"] == "completed"
    assert final["nodes"][0]["status"] == "skipped"
    assert (await manager.agent_dispatcher.get_job(job["id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_old_clarification_cannot_replace_newer_instruction(tmp_path: Path) -> None:
    manager, evaluator, goal_id, job = await active(tmp_path)
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(
            message="Un seul utilisateur, sur cet ordinateur.", client_message_id="newer"
        ),
        actor_id="phone",
    )
    recorded, _ = await submit(manager, job, result=CLARIFICATION)
    await manager.on_job_result(recorded)
    assert (await manager.graph.get_goal(goal_id))["status"] == "running"
    assert (await manager.conversation_messages(goal_id))["pending_question_id"] is None
    assert evaluator.contexts == []


@pytest.mark.asyncio
async def test_source_shortage_is_not_a_refusal_or_completed_document(tmp_path: Path) -> None:
    manager, evaluator, goal_id, job = await active(tmp_path)
    recorded, _ = await submit(
        manager,
        job,
        result={
            **DECLINED,
            "outcome": "insufficient_sources",
            "text": "Il manque une source officielle sur SQLite.",
            "summary": "Sources insuffisantes.",
        },
    )
    assert recorded["error"] == "writing_insufficient_sources"
    detail = await manager.on_job_result(recorded)
    assert detail["goal"]["status"] == "failed"
    assert detail["goal"]["failure_reason"] == "writing_insufficient_sources"
    assert evaluator.contexts == []
    history = await manager.conversation_messages(goal_id)
    assert history["pending_question_id"] is None
    assert "refus" not in history["messages"][-1]["content"]
    async with aiosqlite.connect(manager.db_path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone())[0] == 1
