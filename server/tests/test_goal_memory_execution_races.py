"""Race regressions for committed memory reservations and actual lease expiry."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiosqlite
import httpx
import pytest

from app.services.context_builder import ContextBuilder
from app.services.model_request_execution import ModelExecutionControlError
from app.services.swarm_contracts import GoalMessageRequest, GoalStartRequest
from tests.test_goal_manager import _manager, _parallel_plan
from tests.test_goal_memory_execution import bound, calls, invoke
from tests.test_memory_normalization import provider as normalizer_provider
from tests.test_strategy_goal_execution import prepared


@pytest.mark.asyncio
async def test_cancel_after_reservation_commit_settles_owned_receipt(tmp_path, monkeypatch):
    manager, goal, executor = await bound(tmp_path)
    closing = asyncio.Event()
    release_close = asyncio.Event()
    original_close = aiosqlite.Connection.close
    delay_once = True
    dispatched = False

    async def delayed_close(connection):
        nonlocal delay_once
        if delay_once:
            delay_once = False
            # _reserve_model_call has committed and entered its admission
            # connection's protected close. The caller has not received its ID.
            closing.set()
            await release_close.wait()
        await original_close(connection)

    async def forbidden_request():
        nonlocal dispatched
        dispatched = True
        return {"unexpected": "request"}

    monkeypatch.setattr(aiosqlite.Connection, "close", delayed_close)
    task = asyncio.create_task(invoke(executor, forbidden_request))
    try:
        await asyncio.wait_for(closing.wait(), timeout=2)
        [reserved] = await calls(manager)
        assert reserved["status"] == "started"
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release_close.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert not dispatched
    [row] = await calls(manager)
    assert row["status"] == "failed"
    assert row["error_category"] == "cancelled_before_request"
    assert row["output_digest"] is None
    assert (await manager.graph.get_goal(goal["id"]))["model_call_count"] == 1


@pytest.mark.asyncio
async def test_expired_committed_lease_prevents_memory_http_dispatch(tmp_path, monkeypatch):
    manager, goal, executor = await bound(tmp_path)
    original_get_goal = manager.graph.get_goal
    changed = False
    dispatched = False

    async def goal_after_lease_expiry(goal_id):
        nonlocal changed
        current = await original_get_goal(goal_id)
        if not changed:
            changed = True
            # Simulate expiry during the awaited goal lookup after reservation.
            # The goal itself and its revision remain valid throughout.
            async with aiosqlite.connect(manager.db_path) as db:
                await db.execute(
                    "UPDATE goal_model_calls SET lease_expires_at=? "
                    "WHERE goal_run_id=? AND status='started'",
                    ("2000-01-01T00:00:00+00:00", goal_id),
                )
                await db.commit()
        return current

    async def forbidden_request():
        nonlocal dispatched
        dispatched = True
        return {"unexpected": "late request"}

    monkeypatch.setattr(manager.graph, "get_goal", goal_after_lease_expiry)
    with pytest.raises(ModelExecutionControlError):
        await invoke(executor, forbidden_request)

    assert changed and not dispatched
    [row] = await calls(manager)
    assert row["status"] != "completed"
    assert row["output_digest"] is None
    assert (await original_get_goal(goal["id"]))["model_call_count"] == 1


@pytest.mark.asyncio
async def test_failed_memory_qualification_cools_down_but_new_input_can_resume(
    tmp_path, monkeypatch
):
    retrieval, state, goal_id, _, embedding_requests = await prepared(tmp_path, monkeypatch)
    manager = await _manager(tmp_path, _parallel_plan())
    manager.state_service = state
    manager.strategy_retrieval = retrieval
    manager.context_builder = ContextBuilder(state.db_path)
    invalid_requests: list[httpx.Request] = []

    def invalid_response(request: httpx.Request) -> httpx.Response:
        invalid_requests.append(request)
        return httpx.Response(200, json={"choices": []})

    state.memory_normalizer = normalizer_provider(invalid_response)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET status='planning',objective=?,max_model_calls=20,"
            "started_at=?,current_phase='start_requested' WHERE id=?",
            ("Dois-je envoyer automatiquement ?", datetime.now(UTC).isoformat(), goal_id),
        )
        await db.commit()

    await manager.reconcile()
    first_rows = await calls(manager)
    assert [row["role"] for row in first_rows] == [
        "memory_embedder",
        "memory_embedder",
        "memory_normalizer",
    ]
    assert first_rows[-1]["status"] == "failed"
    failed_goal = await manager.graph.get_goal(goal_id)
    assert failed_goal["current_phase"] == "memory_retrieval_invalid"
    assert failed_goal["status"] == "planning"
    assert len(invalid_requests) == 1 and len(embedding_requests) == 2

    for _ in range(2):
        await manager.reconcile()
    assert await calls(manager) == first_rows
    assert len(invalid_requests) == 1 and len(embedding_requests) == 2

    await manager.reply_goal(
        goal_id,
        GoalMessageRequest(
            message="Reprendre la recherche avec cette nouvelle demande.",
            client_message_id="memory-cooldown-new-input",
        ),
        actor_id="test",
    )
    await manager.reconcile()
    resumed_rows = await calls(manager)
    assert len(resumed_rows) == 6
    assert len(invalid_requests) == 2 and len(embedding_requests) == 4
    assert all(row["conversation_revision"] == 1 for row in resumed_rows[3:])
    assert (await manager.graph.get_goal(goal_id))["model_call_count"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied_plan", [False, True])
async def test_successful_explicit_start_clears_same_revision_memory_cooldown(
    tmp_path, monkeypatch, supplied_plan
):
    retrieval, state, goal_id, _, _ = await prepared(tmp_path, monkeypatch)
    plan = _parallel_plan(objective="Dois-je envoyer automatiquement ?")
    manager = await _manager(tmp_path, plan)
    manager.state_service = state
    manager.strategy_retrieval = retrieval
    manager.context_builder = ContextBuilder(state.db_path)
    valid_normalizer = state.memory_normalizer
    state.memory_normalizer = normalizer_provider(
        lambda request: httpx.Response(200, json={"choices": []})
    )
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE tasks SET status='planned' WHERE id="
            "(SELECT root_task_id FROM goal_runs WHERE id=?)",
            (goal_id,),
        )
        await db.execute(
            "UPDATE goal_runs SET status='planning',objective=?,max_model_calls=20,"
            "started_at=?,current_phase='start_requested' WHERE id=?",
            ("Dois-je envoyer automatiquement ?", datetime.now(UTC).isoformat(), goal_id),
        )
        await db.commit()

    await manager.reconcile()
    failed = await manager.graph.get_goal(goal_id)
    assert failed["current_phase"] == "memory_retrieval_invalid"
    assert len(await calls(manager)) == 3
    state.memory_normalizer = valid_normalizer
    request = (
        GoalStartRequest(planner_source="manual", plan_proposal=plan)
        if supplied_plan
        else GoalStartRequest()
    )
    started = await manager.start_goal(goal_id, request)
    current = await manager.graph.get_goal(goal_id)
    assert current["status"] == "running"
    assert current["conversation_revision"] == failed["conversation_revision"]
    assert current["current_phase"] != "memory_retrieval_invalid"
    workers = [node for node in started["nodes"] if node["node_type"] == "worker"]
    assert len(workers) == 2
    assert all(node["status"] == "dispatched" for node in workers)

    # Reproduce recovery after durable worker completion, before the dependent
    # synthesis has advanced. An old failed lookup must not stall this new plan.
    for node in workers:
        await manager.graph.transition_node(
            node["id"], expected="dispatched", target="completed", result_summary="Evidence"
        )
    await manager.graph.refresh_ready_nodes(goal_id)
    before = await manager.graph.list_nodes(goal_id)
    synthesis = next(node for node in before if node["node_type"] == "synthesis")
    assert synthesis["status"] == "ready"

    await manager.reconcile()

    after = await manager.graph.get_node(synthesis["id"])
    assert after["status"] == "completed"
    async with aiosqlite.connect(state.db_path) as db:
        assert not await manager._memory_retrieval_cooling_down_locked(
            db, current, now=manager._now()
        )
    # The append-only audit is preserved; only its scheduling relevance expires.
    async with aiosqlite.connect(state.db_path) as db:
        assert await (
            await db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE trace_id=? "
                "AND event_type='goal.memory.retrieval.failed'",
                (goal_id,),
            )
        ).fetchone() == (1,)
