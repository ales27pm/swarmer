from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import (
    AgentCreate,
    FeedbackCreate,
    MemoryCreate,
    MemoryUpdate,
    TaskCreate,
    TaskRecord,
)
from app.services import state_service as state_service_module
from app.services.state_service import StateService


async def fail_audit(*args: Any, **kwargs: Any) -> dict[str, Any]:
    del args, kwargs
    raise RuntimeError("audit unavailable")


@pytest.fixture
async def state(tmp_path: Path) -> StateService:
    service = StateService(tmp_path / "state.db")
    await service.initialize()
    return service


@pytest.mark.asyncio
async def test_task_and_chat_creation_roll_back_when_audit_fails(
    state: StateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = TaskRecord.new(TaskCreate(input="atomic task"), source="device-1")
    monkeypatch.setattr(state_service_module, "append_audit_event", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await state.create_task(task)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await state.create_chat_task("atomic chat", None, "normal", "device-1")

    assert await state.get_task(task.id) is None
    assert await state.list_tasks() == []
    assert await state.list_conversations() == []


@pytest.mark.asyncio
async def test_memory_creation_rolls_back_when_audit_fails(
    state: StateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_service_module, "append_audit_event", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await state.create_memory(MemoryCreate(content="atomic memory"), "device-1")

    assert await state.list_memory() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_memory_change_rolls_back_when_audit_fails(
    state: StateService, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    created = await state.create_memory(MemoryCreate(content="original"), "device-1")
    monkeypatch.setattr(state_service_module, "append_audit_event", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        if operation == "update":
            await state.update_memory(created["id"], MemoryUpdate(content="changed"), "device-1")
        else:
            await state.delete_memory(created["id"], "device-1")

    retained = await state.get_memory(created["id"])
    assert retained is not None
    assert retained["content"] == "original"


@pytest.mark.asyncio
async def test_agent_registration_rolls_back_when_audit_fails(
    state: StateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_service_module, "append_audit_event", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await state.register_agent(
            AgentCreate(name="atomic agent", endpoint="http://127.0.0.1:9001"),
            "device-1",
        )

    assert await state.list_agents() == []


@pytest.mark.asyncio
async def test_feedback_creation_rolls_back_when_audit_fails(
    state: StateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_service_module, "append_audit_event", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await state.create_feedback(FeedbackCreate(score=4), "device-1")

    async with aiosqlite.connect(state.db_path) as db:
        row = await (await db.execute("SELECT COUNT(*) FROM feedback_events")).fetchone()
    assert row is not None
    assert row[0] == 0
