from __future__ import annotations

import asyncio
import fcntl
import json
from datetime import UTC, datetime

import aiosqlite
import httpx
import pytest

from app.services.context_builder import ContextBuilder
from app.services.goal_manager import _LocalModelResourceBusy
from app.services.goal_memory_execution import GoalMemoryExecutor, request_digest
from app.services.model_request_execution import (
    ModelExecutionControlError,
    ModelRequestBudgetUnavailable,
)
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest
from tests.test_goal_manager import _manager, _parallel_plan
from tests.test_strategy_goal_execution import prepared


async def bound(tmp_path, *, budget=10):
    manager = await _manager(tmp_path, _parallel_plan())
    created = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the repository", max_model_calls=budget),
        actor_id="test",
    )
    goal = created
    goal = await manager._mark_start_requested(goal["id"])
    return manager, goal, GoalMemoryExecutor(manager, goal["id"], goal["conversation_revision"])


async def calls(manager):
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        return [
            dict(row)
            for row in await (
                await db.execute("SELECT * FROM goal_model_calls ORDER BY lease_generation")
            ).fetchall()
        ]


async def invoke(executor, operation):
    return await executor.execute(
        role="memory_normalizer",
        model_id="translator",
        endpoint="http://localhost/v1/chat/completions",
        request_body={"model": "translator", "messages": [{"role": "user", "content": "bonjour"}]},
        operation=operation,
    )


@pytest.mark.asyncio
async def test_exact_request_is_charged_once_and_recorded(tmp_path):
    manager, goal, executor = await bound(tmp_path)

    async def operation():
        assert len(await calls(manager)) == 1
        assert (await manager.graph.get_goal(goal["id"]))["model_call_count"] == 1
        return {"translated": "hello"}

    assert await invoke(executor, operation) == {"translated": "hello"}
    [row] = await calls(manager)
    assert row["role"] == "memory_normalizer" and row["model_id"] == "translator"
    assert row["status"] == "completed" and row["output_digest"] == request_digest(
        {"translated": "hello"}
    )
    assert row["input_digest"] == request_digest(
        {
            "endpoint": "http://localhost/v1/chat/completions",
            "body": {"model": "translator", "messages": [{"role": "user", "content": "bonjour"}]},
        }
    )


@pytest.mark.asyncio
async def test_last_credit_is_reserved_for_planner_without_sending_memory_request(tmp_path):
    manager, goal, executor = await bound(tmp_path, budget=1)

    async def forbidden():
        pytest.fail("memory request must not be sent")

    with pytest.raises(ModelRequestBudgetUnavailable):
        await invoke(executor, forbidden)
    assert await calls(manager) == []
    assert (await manager.graph.get_goal(goal["id"]))["model_call_count"] == 0


@pytest.mark.asyncio
async def test_external_gpu_contention_does_not_charge_or_send(tmp_path):
    manager, goal, executor = await bound(tmp_path)
    path = tmp_path / "gpu.lock"
    manager.agent_dispatcher.local_model_gpu_lock_path = path
    with path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(_LocalModelResourceBusy):
            await invoke(executor, lambda: asyncio.sleep(0))
    assert await calls(manager) == []
    assert (await manager.graph.get_goal(goal["id"]))["model_call_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "cancel", "expire"])
async def test_late_result_is_fenced_and_attempt_recorded(tmp_path, change):
    manager, goal, executor = await bound(tmp_path)

    async def operation():
        async with aiosqlite.connect(manager.db_path) as db:
            if change == "revision":
                await db.execute(
                    "UPDATE goal_runs SET conversation_revision=conversation_revision+1 WHERE id=?",
                    (goal["id"],),
                )
            elif change == "cancel":
                await db.execute(
                    "UPDATE goal_runs SET status='cancelled' WHERE id=?", (goal["id"],)
                )
            else:
                await db.execute(
                    "UPDATE goal_runs SET started_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                    (goal["id"],),
                )
            await db.commit()
        return {"must_not_escape": True}

    with pytest.raises(ModelExecutionControlError):
        await invoke(executor, operation)
    [row] = await calls(manager)
    assert row["status"] == "failed" and row["error_category"] == "context_changed"
    assert row["output_digest"] is None


@pytest.mark.asyncio
async def test_cancelled_operation_releases_its_receipt(tmp_path):
    manager, _, executor = await bound(tmp_path)
    started = asyncio.Event()

    async def operation():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(invoke(executor, operation))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [row] = await calls(manager)
    assert row["status"] == "failed" and row["error_category"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_language,budget,expected_memory_calls,embedding_failure",
    [
        ("fr", 10, 5, False),
        ("fr", 1, 0, False),
        ("fr", 10, 5, True),
        ("en", 10, 7, False),
        ("en", 1, 0, False),
        ("en", 10, 7, True),
    ],
)
async def test_planner_path_accounts_for_real_memory_http_and_retains_provenance(
    tmp_path,
    monkeypatch,
    source_language,
    budget,
    expected_memory_calls,
    embedding_failure,
):
    retrieval, state, goal_id, allowed, requests = await prepared(
        tmp_path, monkeypatch, source_language=source_language
    )
    if embedding_failure:
        previous_client = httpx.AsyncClient

        def fail_embedding(request):
            requests.append(json.loads(request.content))
            return httpx.Response(503, json={"error": "temporarily unavailable"})

        def isolated_client(*args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(fail_embedding))
            return previous_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", isolated_client)
    manager = await _manager(tmp_path, _parallel_plan())
    manager.state_service = state
    manager.strategy_retrieval = retrieval
    manager.context_builder = ContextBuilder(state.db_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET status='planning',objective=?,max_model_calls=?,started_at=?,current_phase='start_requested' WHERE id=?",
            ("Dois-je envoyer automatiquement ?", budget, datetime.now(UTC).isoformat(), goal_id),
        )
        await db.commit()
    goal = await manager.graph.get_goal(goal_id)
    _, _, planner_call_id = await manager._obtain_plan(goal, GoalStartRequest())
    rows = await calls(manager)
    assert len(rows) == expected_memory_calls + 1
    presentation_roles = [
        r["role"] for r in rows if r["role"] in {"memory_presenter", "memory_presentation_reviewer"}
    ]
    assert presentation_roles == (
        ["memory_presenter", "memory_presentation_reviewer"]
        if source_language == "en" and expected_memory_calls
        else []
    )
    assert [r["status"] for r in rows[:-1]] == [
        "failed" if embedding_failure and r["role"] == "memory_embedder" else "completed"
        for r in rows[:-1]
    ]
    assert rows[-1]["id"] == planner_call_id and rows[-1]["role"] == "planner"
    assert (await manager.graph.get_goal(goal_id))["model_call_count"] == len(rows)
    assert len(requests) == (3 if expected_memory_calls else 0)
    async with aiosqlite.connect(state.db_path) as db:
        context, provenance = await (
            await db.execute(
                "SELECT context_json,provenance_json FROM goal_contexts WHERE id=?",
                (rows[-1]["context_id"],),
            )
        ).fetchone()
    receipt = json.loads(provenance)["strategy_retrieval"]
    cards = json.loads(context)["cards"]
    assert receipt["model_call_ids"] == [r["id"] for r in rows[:-1]]
    assert all(r["context_id"] == rows[-1]["context_id"] for r in rows)
    if expected_memory_calls:
        assert receipt["status"] == ("degraded" if embedding_failure else "completed")
        assert len(receipt["failed_requests"]) == (3 if embedding_failure else 0)
        if embedding_failure:
            assert any(c["card_id"] == "strategy:degraded" for c in cards)
        memories = [x for x in receipt["sources"].values() if x.get("canonical_language")]
        assert {x["source_id"] for x in memories} == {x["id"] for x in allowed}
        assert all(x["canonical_content_sha256"] and x["source_revision"] for x in memories)
        assert any(f"model_calls={expected_memory_calls}/10" in c["summary"] for c in cards)
    else:
        assert receipt["status"] == "budget_unavailable" and not receipt["sources"]
        assert any(c["kind"] == "memory_retrieval_status" for c in cards)
