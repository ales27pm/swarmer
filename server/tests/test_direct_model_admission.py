"""Direct chat/translation cannot bypass Studio or durable goal admission."""

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
from pydantic import BaseModel
from test_model_resource_integration import LocalPlanner, goal, manager, reserve

from app.services.direct_model_admission import LocalGPUUnavailable, direct_model_admission
from app.services.memory_normalization import (
    MemoryNormalizationError,
    OpenAIMemoryNormalizationProvider,
)
from app.services.model_resource_admission import ExternalGPUAdmission


async def test_direct_call_holds_host_slot_but_not_database_writer(tmp_path: Path) -> None:
    runtime = await manager(tmp_path, LocalPlanner())
    path = tmp_path / "gpu.lock"
    contender = ExternalGPUAdmission(path)
    with pytest.raises(ValueError, match="provider failure"):
        async with direct_model_admission(runtime.db_path, path):
            assert not contender.try_acquire()
            async with aiosqlite.connect(runtime.db_path, timeout=0) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.rollback()
            raise ValueError("provider failure")
    assert contender.try_acquire()
    contender.close()


async def test_direct_call_refuses_committed_goal_lease(tmp_path: Path) -> None:
    runtime = await manager(tmp_path, LocalPlanner())
    created = await goal(runtime)
    await reserve(runtime, created["id"])
    path = tmp_path / "gpu.lock"
    with pytest.raises(LocalGPUUnavailable):
        async with direct_model_admission(runtime.db_path, path):
            pytest.fail("inference must not begin")
    contender = ExternalGPUAdmission(path)
    assert contender.try_acquire()
    contender.close()


def test_busy_chat_is_retryable_and_does_not_append_messages(
    client, test_app, paired_headers, tmp_path: Path
) -> None:
    path = tmp_path / "gpu.lock"
    test_app.state.settings.local_model_gpu_lock_path = path
    owner = ExternalGPUAdmission(path)
    assert owner.try_acquire()
    model = AsyncMock(return_value="Bonjour")
    test_app.state.orchestrator_service.chat = model
    try:
        response = client.post("/chat", headers=paired_headers, json={"content": "Bonjour"})
        assert response.status_code == 503
        assert response.headers["X-Mongars-Resource"] == "local_gpu_busy"
        assert response.headers["Retry-After"] == "5"
        model.assert_not_awaited()
        with sqlite3.connect(test_app.state.settings.db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        # Creating work without running a model remains available.
        task = client.post(
            "/chat", headers=paired_headers, json={"content": "Later", "start_task": True}
        )
        assert task.status_code == 201
    finally:
        owner.close()
    assert (
        client.post("/chat", headers=paired_headers, json={"content": "Bonjour"}).status_code == 201
    )
    model.assert_awaited_once()


def test_busy_legacy_planning_does_not_fail_or_modify_task(
    client, test_app, paired_headers, tmp_path: Path
) -> None:
    path = tmp_path / "gpu.lock"
    test_app.state.settings.local_model_gpu_lock_path = path
    task = client.post("/tasks", headers=paired_headers, json={"input": "Inspect"}).json()
    model = AsyncMock(return_value={"tool_name": "none", "arguments": {}, "summary": "Proposal"})
    test_app.state.planner_provider.plan = model
    path.write_bytes(b"chroma-studio-v1:orphan\n")
    response = client.post(f"/tasks/{task['id']}/plan", headers=paired_headers)
    assert response.status_code == 503
    assert client.get(f"/tasks/{task['id']}", headers=paired_headers).json()["task"] == task
    model.assert_not_awaited()
    assert path.read_bytes() == b"chroma-studio-v1:orphan\n"
    path.write_bytes(b"")  # Test simulates Studio-confirmed cleanup.
    assert client.post(f"/tasks/{task['id']}/plan", headers=paired_headers).status_code == 200
    model.assert_awaited_once()


async def test_normalization_admission_precedes_http_and_holds_through_response(
    tmp_path: Path,
) -> None:
    runtime = await manager(tmp_path, LocalPlanner())
    path = tmp_path / "gpu.lock"
    contender = ExternalGPUAdmission(path)
    calls = []

    class Result(BaseModel):
        value: str

    def respond(request):
        calls.append(request)
        assert not contender.try_acquire()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"value":"ok"}'},
                    }
                ]
            },
        )

    provider = OpenAIMemoryNormalizationProvider(
        "http://127.0.0.1:11434/v1",
        "test-model",
        model_admission=lambda: direct_model_admission(runtime.db_path, path),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert contender.try_acquire()
        try:
            with pytest.raises(MemoryNormalizationError, match="local_gpu_busy"):
                await provider._request(client, "test-model", "test", {}, Result)
            assert calls == []
        finally:
            contender.close()
        result = await provider._request(client, "test-model", "test", {}, Result)
        assert result.value == "ok" and len(calls) == 1
    assert contender.try_acquire()
    contender.close()
