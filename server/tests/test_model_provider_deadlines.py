from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.services.evaluator_provider import EvaluatorProviderError, UbuntuEvaluatorProvider
from app.services.permission_policy import PermissionPolicy
from app.services.planner_provider import SwarmPlannerProviderError, UbuntuSwarmPlannerProvider
from app.services.swarm_contracts import GoalEvaluationContext


def provider_call(kind: str, timeout: float) -> Callable[[], Awaitable[Any]]:
    if kind == "planner":
        planner = UbuntuSwarmPlannerProvider(
            base_url="http://127.0.0.1:8711/v1", model="test", timeout_seconds=timeout
        )
        return lambda: planner.propose(
            {"cards": [{"kind": "goal", "card_id": "goal:goal_test", "summary": "Write a plan."}]}
        )
    evaluator = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:8711/v1",
        model="test",
        policy=PermissionPolicy.from_yaml(
            Path(__file__).resolve().parents[2] / "configs/permissions.yaml"
        ),
        timeout_seconds=timeout,
    )
    context = GoalEvaluationContext(
        schema_version="1.0",
        goal_run_id="goal_test",
        objective="Write a plan.",
        completion_criteria=["The plan is available."],
        node_results=[],
        known_node_ids=[],
        available_skills=[],
        remaining_step_budget=2,
        remaining_model_call_budget=2,
        elapsed_seconds=1,
        state_fingerprint="0" * 64,
    )
    return lambda: evaluator.evaluate(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["planner", "evaluator"])
@pytest.mark.parametrize("phase", ["headers", "body"])
async def test_absolute_deadline_bounds_stalled_headers_and_continuously_arriving_body(
    monkeypatch: pytest.MonkeyPatch, kind: str, phase: str
) -> None:
    calls = 0
    chunks = 0
    closed = asyncio.Event()

    class DrippingBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal chunks
            while True:
                # Every chunk arrives well inside the inactivity timeout. Only
                # an absolute round-trip deadline can bound the whole response.
                await asyncio.sleep(0.01)
                chunks += 1
                yield b"private incomplete response "

        async def aclose(self) -> None:
            closed.set()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if phase == "headers":
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        return httpx.Response(200, request=request, stream=DrippingBody())

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )
    call = provider_call(kind, 0.1)
    started = time.monotonic()
    with pytest.raises((SwarmPlannerProviderError, EvaluatorProviderError)) as failed:
        await call()
    assert time.monotonic() - started < 0.6
    assert failed.value.category == "transport_unavailable"
    assert isinstance(failed.value.__cause__, TimeoutError)
    assert "private" not in str(failed.value)
    assert calls == 1
    assert closed.is_set()
    if phase == "body":
        assert chunks >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["planner", "evaluator"])
async def test_external_cancellation_is_not_relabelled_as_a_provider_timeout(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("cancelled HTTP request unexpectedly resumed")

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )
    task = asyncio.create_task(provider_call(kind, 60)())
    async with asyncio.timeout(1):
        await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1
    assert cancelled.is_set()
