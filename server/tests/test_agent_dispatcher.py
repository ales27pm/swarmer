from pathlib import Path

import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatcher
from app.services.message_board import MessageBoardService
from app.services.state_service import StateService


@pytest.mark.asyncio
async def test_only_one_agent_can_claim_one_job(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="read"), source="device"))
    first = await state.register_agent(
        AgentCreate(name="first", endpoint="http://127.0.0.1:1", skills=["read"]), "device"
    )
    second = await state.register_agent(
        AgentCreate(name="second", endpoint="http://127.0.0.1:2", skills=["read"]), "device"
    )
    dispatcher = AgentDispatcher(state.db_path, MessageBoardService(state.db_path))
    await dispatcher.queue_job(task.id, "read", {"path": "."})

    claimed = await dispatcher.claim(first["id"])
    unavailable = await dispatcher.claim(second["id"])
    assert claimed is not None
    assert unavailable is None
