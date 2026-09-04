from pathlib import Path

import pytest

from app.services.execution_engine import ExecutionEngine, ExecutionError
from app.services.state_service import StateService


@pytest.mark.asyncio
async def test_workspace_write_and_read(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await StateService(db_path).initialize()
    engine = ExecutionEngine(db_path, workspace)

    write_call = await engine.create_tool_call(
        task_id="tsk_test",
        tool_name="workspace.write_text",
        arguments={"path": "notes/hello.txt", "content": "salut swarm"},
        summary="write a test file",
    )
    assert engine.requires_approval(write_call["tool_name"]) is True
    write_result = await engine.execute(write_call["id"])
    assert write_result["status"] == "completed"
    assert (workspace / "notes/hello.txt").read_text() == "salut swarm"

    read_call = await engine.create_tool_call(
        task_id="tsk_test",
        tool_name="workspace.read_text",
        arguments={"path": "notes/hello.txt"},
        summary="read the test file",
    )
    assert engine.requires_approval(read_call["tool_name"]) is False
    read_result = await engine.execute(read_call["id"])
    assert read_result["result"]["text"] == "salut swarm"


@pytest.mark.asyncio
async def test_workspace_escape_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await StateService(db_path).initialize()
    engine = ExecutionEngine(db_path, workspace)

    call = await engine.create_tool_call(
        task_id="tsk_test",
        tool_name="workspace.read_text",
        arguments={"path": "../secret.txt"},
        summary="attempt escape",
    )

    with pytest.raises(ExecutionError, match="escapes configured workspace"):
        await engine.execute(call["id"])
