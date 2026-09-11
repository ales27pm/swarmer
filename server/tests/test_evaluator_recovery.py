from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import pytest
from test_evaluator import continue_decision, evaluation_context
from test_goal_runtime_recovery import _manager, _worker_plan

from app.services.evaluator_provider import (
    EvaluatorFailureCategory,
    EvaluatorProviderError,
    UbuntuEvaluatorProvider,
)
from app.services.goal_manager import GoalManager
from app.services.swarm_contracts import (
    AutonomyProfile,
    EvaluationDecision,
    GoalCreateRequest,
    GoalMessageRequest,
    GoalStartRequest,
    PlannerSource,
)


class FailingEvaluator:
    source = PlannerSource.TEST

    def __init__(self, category: EvaluatorFailureCategory = "invalid_response") -> None:
        self.calls = 0
        self.category = category
        self.valid = False

    async def evaluate(self, context: Any) -> EvaluationDecision:
        del context
        self.calls += 1
        if self.valid:
            return EvaluationDecision.model_validate(
                {
                    **continue_decision(),
                    "status": "needs_user",
                    "suggested_new_nodes": [],
                    "user_question": "Which requirement should be addressed next?",
                }
            )
        raise EvaluatorProviderError(
            "SECRET_MODEL_TEXT must not be persisted",
            category=self.category,
            diagnostic="schema",
        )


async def failed_worker_goal(manager: GoalManager, *, max_calls: int = 30) -> str:
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Inspect the repository",
            autonomy_profile=AutonomyProfile.AUTONOMOUS,
            max_model_calls=max_calls,
            max_runtime_seconds=1800,
        ),
        actor_id="test-phone",
    )
    goal_id = str(goal["id"])
    await manager.start_goal(goal_id, GoalStartRequest())
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET status='failed',error_summary='worker failed' WHERE goal_run_id=?",
            (goal_id,),
        )
        await db.execute(
            """UPDATE agent_jobs SET status='failed',error='worker failed' WHERE task_id IN
            (SELECT task_id FROM plan_nodes WHERE goal_run_id=?)""",
            (goal_id,),
        )
        await db.execute(
            """UPDATE tasks SET status='failed' WHERE id IN
            (SELECT task_id FROM plan_nodes WHERE goal_run_id=?)""",
            (goal_id,),
        )
        await db.commit()
    return goal_id


async def expire_cooldown(manager: GoalManager, goal_id: str) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            """UPDATE goal_model_calls SET completed_at=? WHERE goal_run_id=?
            AND role='evaluator' AND status='failed'""",
            (past, goal_id),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_cooldown_survives_restart_and_all_entry_points(tmp_path: Path) -> None:
    evaluator = FailingEvaluator("transport_unavailable")
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=evaluator)
    goal_id = await failed_worker_goal(manager)
    assert await manager.reconcile() == 1
    restarted = await _manager(manager.db_path, _worker_plan(), evaluator=evaluator)
    for _ in range(3):
        assert await restarted.reconcile() == 0
        await restarted._evaluate_if_quiescent(goal_id)
    goal = await restarted.graph.get_goal(goal_id)
    assert goal and evaluator.calls == 1 and goal["model_call_count"] == 2
    await expire_cooldown(restarted, goal_id)
    evaluator.valid = True
    assert await restarted.reconcile() == 1
    assert evaluator.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["invalid_response", "request_rejected"])
async def test_invalid_attempts_pause_and_explicit_start_preserves_usage(
    tmp_path: Path, category: EvaluatorFailureCategory
) -> None:
    evaluator = FailingEvaluator(category)
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=evaluator)
    goal_id = await failed_worker_goal(manager)
    before = await manager.graph.get_goal(goal_id)
    for _ in range(3):
        await expire_cooldown(manager, goal_id)
        assert await manager.reconcile() == 1
    paused = await manager.graph.get_goal(goal_id)
    assert paused and before
    assert paused["status"] == "waiting_permission"
    assert paused["current_phase"] == "evaluator_retry_required"
    assert paused["paused_at"] and paused["model_call_count"] == 4
    assert await manager.reconcile() == 0
    history = await manager.conversation_messages(goal_id)
    assert history["pending_question_id"] is None
    assert "SECRET_MODEL_TEXT" not in json.dumps(history)
    # An explicit retry grants one attempt, never a fresh automatic allowance.
    await manager.start_goal(goal_id, GoalStartRequest())
    retried = await manager.graph.get_goal(goal_id)
    assert retried and retried["current_phase"] == "evaluator_retry_required"
    assert retried["model_call_count"] == 5
    assert await manager.reconcile() == 0
    evaluator.valid = True
    await manager.start_goal(goal_id, GoalStartRequest())
    recovered = await manager.graph.get_goal(goal_id)
    assert recovered and recovered["current_phase"] == "needs_user"
    assert recovered["model_call_count"] == 6
    for key in (
        "started_at",
        "step_count",
        "replan_count",
        "max_model_calls",
        "max_runtime_seconds",
    ):
        assert recovered[key] == before[key]
    async with aiosqlite.connect(manager.db_path) as db:
        audit = await (await db.execute("SELECT payload_json FROM audit_events")).fetchall()
    assert "SECRET_MODEL_TEXT" not in str(audit)


@pytest.mark.asyncio
async def test_cooling_older_goal_does_not_starve_later_goal(tmp_path: Path) -> None:
    evaluator = FailingEvaluator()
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=evaluator)
    first = await failed_worker_goal(manager)
    await manager._evaluate_if_quiescent(first)
    second = await failed_worker_goal(manager)
    assert await manager.reconcile(limit=1) == 1
    assert evaluator.calls == 2
    second_goal = await manager.graph.get_goal(second)
    assert second_goal and second_goal["model_call_count"] == 2


@pytest.mark.asyncio
async def test_changed_worker_evidence_bypasses_old_failure_cooldown(tmp_path: Path) -> None:
    evaluator = FailingEvaluator()
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=evaluator)
    goal_id = await failed_worker_goal(manager)
    await manager.reconcile()
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET error_summary='New worker evidence',updated_at=? WHERE goal_run_id=?",
            (datetime.now(UTC).isoformat(), goal_id),
        )
        await db.commit()
    evaluator.valid = True
    assert await manager.reconcile(limit=1) == 1
    assert evaluator.calls == 2


@pytest.mark.asyncio
async def test_legacy_failure_cooldown_matches_scheduler_and_entry(tmp_path: Path) -> None:
    evaluator = FailingEvaluator()
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=evaluator)
    goal_id = await failed_worker_goal(manager)
    await manager.reconcile()
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_model_calls SET error_category='provider_unavailable' WHERE role='evaluator'"
        )
        await db.execute(
            "UPDATE goal_runs SET current_phase='evaluator_unavailable' WHERE id=?", (goal_id,)
        )
        await db.commit()
    assert await manager.reconcile() == 0
    await manager._evaluate_if_quiescent(goal_id)
    assert evaluator.calls == 1


@pytest.mark.asyncio
async def test_new_reply_recovers_paused_goal_without_resetting_budget(tmp_path: Path) -> None:
    evaluator = FailingEvaluator()
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=evaluator)
    goal_id = await failed_worker_goal(manager)
    for _ in range(3):
        await expire_cooldown(manager, goal_id)
        await manager.reconcile()
    paused = await manager.graph.get_goal(goal_id)
    assert paused and paused["paused_at"]
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(message="Inspect the updated repository.", client_message_id="recover"),
        actor_id="test-phone",
    )
    await manager.reconcile()
    resumed = await manager.graph.get_goal(goal_id)
    assert resumed and resumed["status"] == "running" and resumed["paused_at"] is None
    assert resumed["conversation_revision"] == 1
    assert resumed["model_call_count"] == paused["model_call_count"] + 1
    assert resumed["started_at"] == paused["started_at"]
    assert resumed["max_model_calls"] == paused["max_model_calls"]
    assert resumed["paused_seconds"] >= paused["paused_seconds"]
    assert evaluator.calls == 3
    # New conversation failures start a separate bounded recovery sequence.
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET status='failed',error_summary='New iteration failed' WHERE goal_run_id=?",
            (goal_id,),
        )
        await db.commit()
    await manager._evaluate_if_quiescent(goal_id)
    latest = await manager.graph.get_goal(goal_id)
    assert latest and latest["status"] == "running"
    assert latest["current_phase"] == "evaluator_invalid_response"
    assert evaluator.calls == 4


@pytest.mark.asyncio
async def test_concurrent_managers_cannot_retry_after_fast_failure(tmp_path: Path) -> None:
    evaluator = FailingEvaluator()
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=evaluator)
    second = await _manager(manager.db_path, _worker_plan(), evaluator=evaluator)
    goal_id = await failed_worker_goal(manager)
    ready, release = asyncio.Event(), asyncio.Event()
    reserve = second._reserve_model_call

    async def delayed_reservation(*args: Any, **kwargs: Any) -> str:
        # The second entry check observes no failure, but its actual reservation
        # occurs only after the first evaluator has returned and released its lease.
        ready.set()
        await release.wait()
        return await reserve(*args, **kwargs)

    with patch.object(second, "_reserve_model_call", delayed_reservation):
        waiting = asyncio.create_task(second._evaluate_if_quiescent(goal_id))
        await ready.wait()
        await manager._evaluate_if_quiescent(goal_id)
        release.set()
        await waiting
    goal = await manager.graph.get_goal(goal_id)
    assert goal and evaluator.calls == 1 and goal["model_call_count"] == 2


@pytest.mark.asyncio
async def test_late_failure_cannot_overwrite_new_reply(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedEvaluator(FailingEvaluator):
        async def evaluate(self, context: Any) -> EvaluationDecision:
            entered.set()
            await release.wait()
            return await super().evaluate(context)

    evaluator = DelayedEvaluator()
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=evaluator)
    goal_id = await failed_worker_goal(manager)
    pending = asyncio.create_task(manager._evaluate_if_quiescent(goal_id))
    await entered.wait()
    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(message="Use the latest requirements.", client_message_id="new-reply"),
        actor_id="test-phone",
    )
    before = await manager.graph.get_goal(goal_id)
    release.set()
    await pending
    after = await manager.graph.get_goal(goal_id)
    assert before == after
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute("SELECT error_category FROM goal_model_calls WHERE role='evaluator'")
        ).fetchone()
    assert row and row[0] == "conversation_changed"


@pytest.mark.asyncio
async def test_invalid_context_pauses_without_model_credit_and_can_retry(tmp_path: Path) -> None:
    evaluator = FailingEvaluator()
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=evaluator)
    goal_id = await failed_worker_goal(manager)
    broken = AsyncMock()
    broken.build_evaluation_context.side_effect = ValueError("SECRET_CONTEXT_TEXT")
    manager.context_builder = broken
    await manager._evaluate_if_quiescent(goal_id)
    paused = await manager.graph.get_goal(goal_id)
    assert paused and paused["current_phase"] == "evaluator_retry_required"
    assert paused["model_call_count"] == 1 and evaluator.calls == 0
    assert "SECRET" not in str(paused)
    await manager.start_goal(goal_id, GoalStartRequest())
    assert evaluator.calls == 0  # Explicit retry cannot bypass context validation.
    manager.context_builder = None
    evaluator.valid = True
    await manager.start_goal(goal_id, GoalStartRequest())
    assert evaluator.calls == 1


@pytest.mark.asyncio
async def test_explicit_retry_keeps_original_model_budget(tmp_path: Path) -> None:
    evaluator = FailingEvaluator()
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=evaluator)
    goal_id = await failed_worker_goal(manager, max_calls=4)
    for _ in range(3):
        await expire_cooldown(manager, goal_id)
        await manager.reconcile()
    await manager.start_goal(goal_id, GoalStartRequest())
    goal = await manager.graph.get_goal(goal_id)
    assert goal and goal["status"] == "budget_exhausted"
    assert goal["model_call_count"] == 4 and evaluator.calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,category",
    [
        (400, "request_rejected"),
        (401, "request_rejected"),
        (408, "transport_unavailable"),
        (429, "transport_unavailable"),
        (500, "transport_unavailable"),
    ],
)
async def test_provider_classifies_http_status_without_response_values(
    tmp_path: Path, status: int, category: str
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    provider = UbuntuEvaluatorProvider(
        base_url="http://invalid.test/v1", model="test", policy=manager.permission_policy
    )
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "http://invalid.test/v1/chat/completions"),
        text="SECRET_RESPONSE",
    )
    with (
        patch("httpx.AsyncClient.post", AsyncMock(return_value=response)),
        pytest.raises(EvaluatorProviderError) as caught,
    ):
        await provider.evaluate(evaluation_context())
    assert caught.value.category == category
    assert caught.value.diagnostic == "http_status"
    assert "SECRET_RESPONSE" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["envelope", "json", "schema", "graph"])
async def test_http_200_invalid_response_has_safe_stage_and_digest(
    tmp_path: Path, stage: str
) -> None:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    provider = UbuntuEvaluatorProvider(
        base_url="http://invalid.test/v1", model="test", policy=manager.permission_policy
    )
    raw = continue_decision()
    if stage == "schema":
        raw["SECRET_FIELD"] = "SECRET_CONTENT"
    if stage == "graph":
        raw["suggested_new_nodes"][0]["required_skill"] = "SECRET_SKILL"
    content = "not json SECRET" if stage == "json" else json.dumps(raw)
    envelope = {} if stage == "envelope" else {"choices": [{"message": {"content": content}}]}
    response = httpx.Response(
        200, request=httpx.Request("POST", "http://invalid.test/v1/chat/completions"), json=envelope
    )
    with (
        patch("httpx.AsyncClient.post", AsyncMock(return_value=response)),
        pytest.raises(EvaluatorProviderError) as caught,
    ):
        await provider.evaluate(evaluation_context())
    assert caught.value.category == "invalid_response"
    assert caught.value.diagnostic == stage
    assert "SECRET" not in str(caught.value)
    assert (caught.value.output_digest is None) == (stage == "envelope")
