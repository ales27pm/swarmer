from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.agent_dispatcher import AgentDispatchConflict
from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest
from tests.test_goal_context_payloads import _CapturingEvaluator
from tests.test_goal_runtime_recovery import _manager
from tests.test_goal_writing import OBJECTIVE, RESULT, plan, register

DECLINED = {
    **RESULT,
    "outcome": "declined",
    "model_id": "fixture-writer",
    "text": "Je ne peux pas fournir ce document. password=private-fixture-value",
    "summary": "Le modèle n’a pas fourni le document.",
}


async def active(tmp_path: Path) -> tuple[GoalManager, _CapturingEvaluator, str, dict[str, Any]]:
    evaluator = _CapturingEvaluator()
    manager = await _manager(tmp_path / "declined.db", plan(), evaluator=evaluator)
    agent = await register(manager)
    goal = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None
    return manager, evaluator, str(goal["id"]), job


async def submit(
    manager: GoalManager, job: dict[str, Any], *, status: str = "failed", result: Any = None
) -> tuple[dict[str, Any], bool]:
    return await manager.agent_dispatcher.submit_result(
        str(job["claimed_by"]),
        job["id"],
        job["claim_token"],
        status=status,
        result=DECLINED if result is None else result,
        error="untrusted arbitrary worker error",
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )


@pytest.mark.asyncio
async def test_declined_result_is_failed_preserved_and_idempotent(tmp_path: Path) -> None:
    manager, _, _, job = await active(tmp_path)
    recorded, changed = await submit(manager, job)
    assert changed and recorded["status"] == "failed"
    assert recorded["error"] == "model_declined"
    assert recorded["result"] == DECLINED
    repeated, changed = await submit(manager, job)
    assert not changed and repeated["result"] == DECLINED
    task = await manager.state_service.get_task(job["task_id"])
    assert task is not None and task.status.value == "failed"


@pytest.mark.asyncio
async def test_completed_declined_result_cannot_claim_success(tmp_path: Path) -> None:
    manager, _, _, job = await active(tmp_path)
    with pytest.raises(AgentDispatchConflict, match="invalid_writing_result"):
        await submit(manager, job, status="completed")
    assert (await manager.agent_dispatcher.get_job(job["id"]))["status"] == "claimed"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"model_id": ""}, {"text": ""}, {"content_trust": "trusted"}])
async def test_malformed_decline_cannot_gain_failure_diagnostic(
    tmp_path: Path, change: dict[str, str]
) -> None:
    manager, _, _, job = await active(tmp_path)
    with pytest.raises(AgentDispatchConflict, match="invalid_writing_result"):
        await submit(manager, job, result={**DECLINED, **change})


@pytest.mark.asyncio
async def test_decline_stops_goal_without_evaluation_retry_or_question(tmp_path: Path) -> None:
    manager, evaluator, goal_id, job = await active(tmp_path)
    recorded, _ = await submit(manager, job)
    detail = await manager.on_job_result(recorded)
    assert detail is not None
    assert detail["goal"]["status"] == "failed"
    assert detail["goal"]["failure_reason"] == "model_declined"
    assert detail["nodes"][0]["status"] == "failed"
    assert detail["nodes"][0]["error_summary"] == "model_declined"
    assert evaluator.contexts == []
    assert detail["goal"]["model_call_count"] == 2
    history = await manager.conversation_messages(goal_id)
    diagnostic = history["messages"][-1]["content"]
    assert "fixture-writer" in diagnostic and "refus" in diagnostic.lower()
    assert "Je ne peux pas fournir ce document." in diagnostic
    assert "private-fixture-value" not in diagnostic
    assert history["pending_question_id"] is None
    await manager.on_job_result(recorded)
    await manager.reconcile()
    assert (await manager.conversation_messages(goal_id))["messages"] == history["messages"]
    assert evaluator.contexts == []
    async with aiosqlite.connect(manager.db_path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone())[0] == 0
        assert (
            json.loads(
                (await (await db.execute("SELECT result_json FROM agent_jobs")).fetchone())[0]
            )
            == DECLINED
        )


@pytest.mark.asyncio
async def test_reconciliation_recovers_declined_without_an_evaluator_call(tmp_path: Path) -> None:
    manager, evaluator, goal_id, job = await active(tmp_path)
    await submit(manager, job)
    await manager.reconcile()
    assert (await manager.graph.get_goal(goal_id))["failure_reason"] == "model_declined"
    assert evaluator.contexts == []


@pytest.mark.asyncio
async def test_decline_hook_uses_recorded_job_not_forged_mapping(tmp_path: Path) -> None:
    manager, evaluator, goal_id, job = await active(tmp_path)
    recorded, _ = await submit(manager, job, status="completed", result=RESULT)
    await manager.on_job_result({**recorded, "status": "failed", "result": DECLINED})
    detail = await manager.get_goal(goal_id)
    assert detail is not None and detail["nodes"][0]["status"] == "completed"
    assert detail["goal"]["failure_reason"] != "model_declined"
    assert len(evaluator.contexts) == 1


@pytest.mark.asyncio
async def test_old_decline_preserves_newer_user_reply(tmp_path: Path) -> None:
    manager, evaluator, goal_id, job = await active(tmp_path)
    await manager.conversations.append(
        goal_id,
        message="Rédige plutôt une liste de courses.",
        actor_id="phone",
        client_message_id="new-scope",
        reply_to_message_id=None,
    )
    recorded, _ = await submit(manager, job)
    await manager.on_job_result(recorded)
    detail = await manager.get_goal(goal_id)
    assert detail is not None and detail["goal"]["status"] == "running"
    assert (await manager.graph.get_goal(goal_id))["pending_message_revision"] == 1
    assert detail["nodes"][0]["status"] == "failed"
    assert evaluator.contexts == []
    assert (await manager.conversation_messages(goal_id))["messages"][-1]["content"] == (
        "Rédige plutôt une liste de courses."
    )


@pytest.mark.asyncio
async def test_decline_cannot_override_a_cancelled_goal(tmp_path: Path) -> None:
    manager, evaluator, goal_id, job = await active(tmp_path)
    recorded, _ = await submit(manager, job)
    await manager.cancel_goal(goal_id, actor_id="phone")
    before = await manager.conversation_messages(goal_id)
    await manager.on_job_result(recorded)
    assert (await manager.graph.get_goal(goal_id))["status"] == "cancelled"
    assert (await manager.conversation_messages(goal_id)) == before
    assert evaluator.contexts == []


@pytest.mark.asyncio
async def test_decline_from_mismatched_task_provenance_cannot_terminate_goal(
    tmp_path: Path,
) -> None:
    manager, evaluator, goal_id, job = await active(tmp_path)
    recorded, _ = await submit(manager, job)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE tasks SET source='goal:other' WHERE id=?", (job["task_id"],))
        await db.commit()
    with pytest.raises(GoalManagerConflict, match="writing refusal evidence is unavailable"):
        await manager.on_job_result(recorded)
    assert (await manager.graph.get_goal(goal_id))["status"] == "running"
    assert evaluator.contexts == []
