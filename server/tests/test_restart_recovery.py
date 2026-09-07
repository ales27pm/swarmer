import stat
from pathlib import Path

import aiosqlite
import pytest

from app.models import TaskCreate, TaskRecord
from app.services.approval_gateway import ApprovalGateway
from app.services.execution_engine import AuthenticatedRequester, ExecutionEngine, ExecutionError
from app.services.permission_policy import PermissionPolicy, ProcessPolicy, ToolPermissionRule
from app.services.state_service import StateService


def policy() -> PermissionPolicy:
    return PermissionPolicy(
        protected_paths=("**/.env",),
        tool_rules={
            "workspace.list_dir": ToolPermissionRule("list", "List files.", "allow", "low"),
            "workspace.read_text": ToolPermissionRule("read", "Read files.", "allow", "low"),
            "workspace.write_text": ToolPermissionRule(
                "write", "Writing requires approval.", "ask", "medium", 300
            ),
            "process.run": ToolPermissionRule(
                "process", "Processes require approval.", "ask", "high", 300
            ),
        },
        process=ProcessPolicy(
            backend="bubblewrap",
            binary=Path("/definitely/missing/bwrap"),
            limiter_binary=Path("/definitely/missing/prlimit"),
            network="deny",
            allowed_commands=frozenset({"pytest"}),
            max_timeout_seconds=30,
            max_output_bytes=65_536,
            max_memory_bytes=1_073_741_824,
            max_processes=64,
            max_file_bytes=16_777_216,
            max_open_files=256,
        ),
    )


REQUESTER = AuthenticatedRequester(id="pytest-device", name="pytest")


@pytest.mark.asyncio
async def test_restart_marks_interrupted_execution_uncertain_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "private" / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="interrupted work"), source="pytest")
    )
    engine = ExecutionEngine(database, workspace, policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.list_dir",
        arguments={"path": "."},
        summary="List once",
        requester=REQUESTER,
    )
    async with aiosqlite.connect(database) as db:
        await db.execute("UPDATE tool_calls SET status='running' WHERE id=?", (call["id"],))
        await db.execute("UPDATE tasks SET status='running' WHERE id=?", (task.id,))
        await db.commit()

    await StateService(database).initialize()

    recovered_call = await engine.get(call["id"])
    recovered_task = await state.get_task(task.id)
    assert recovered_call is not None
    assert recovered_call["status"] == "failed"
    assert "outcome uncertain; not retried" in recovered_call["error"]
    assert recovered_task is not None
    assert recovered_task.status.value == "failed"
    assert recovered_task.error_json is not None
    assert "outcome uncertain; not retried" in recovered_task.error_json["message"]
    audit = await state.list_audit(limit=20)
    interruption = next(
        event
        for event in audit
        if event["event_type"] == "execution.interrupted"
        and event["payload"]["tool_call_id"] == call["id"]
    )
    assert interruption["payload"] == {
        "tool_call_id": call["id"],
        "stage": "running",
        "outcome": "uncertain",
        "retry": False,
    }

    dispatched = False

    async def must_not_dispatch(*_: object) -> dict[str, object]:
        nonlocal dispatched
        dispatched = True
        return {}

    monkeypatch.setattr(engine, "_dispatch", must_not_dispatch)
    with pytest.raises(ExecutionError, match="not executable"):
        await engine.execute(call["id"])
    assert dispatched is False


@pytest.mark.asyncio
async def test_state_database_is_created_owner_only(tmp_path: Path) -> None:
    database = tmp_path / "private" / "state.db"
    await StateService(database).initialize()

    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_restart_fails_approved_call_before_claim_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "private" / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="approved but not claimed"), source="pytest")
    )
    engine = ExecutionEngine(database, workspace, policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.write_text",
        arguments={"path": "must-not-run.txt", "content": "one use only"},
        summary="Model-provided context is not the consent scope",
        requester=REQUESTER,
    )
    approval_id = str(call["approval_id"])
    approved = await ApprovalGateway(database).decide(approval_id, "approve", actor_id="pytest")
    assert approved is not None
    assert approved["status"] == "approved"
    queued_call = await engine.get(call["id"])
    assert queued_call is not None
    assert queued_call["status"] == "queued"

    await StateService(database).initialize()

    recovered_call = await engine.get(call["id"])
    recovered_task = await state.get_task(task.id)
    recovered_approval = await ApprovalGateway(database).get(approval_id)
    assert recovered_call is not None
    assert recovered_call["status"] == "failed"
    assert "after approval but before execution claim" in recovered_call["error"]
    assert "execution not started; not retried" in recovered_call["error"]
    assert "outcome uncertain" not in recovered_call["error"]
    assert recovered_task is not None
    assert recovered_task.status.value == "failed"
    assert recovered_task.error_json is not None
    assert "execution not started; not retried" in recovered_task.error_json["message"]
    assert recovered_approval is not None
    assert recovered_approval["status"] == "approved"

    audit = await state.list_audit(limit=20)
    interruption = next(
        event
        for event in audit
        if event["event_type"] == "execution.not_started"
        and event["payload"]["tool_call_id"] == call["id"]
    )
    assert interruption["payload"] == {
        "tool_call_id": call["id"],
        "stage": "approved_before_claim",
        "outcome": "not_started",
        "retry": False,
    }
    assert not any(
        event["event_type"] == "execution.interrupted"
        and event["payload"].get("tool_call_id") == call["id"]
        for event in audit
    )

    dispatched = False

    async def must_not_dispatch(*_: object) -> dict[str, object]:
        nonlocal dispatched
        dispatched = True
        return {}

    monkeypatch.setattr(engine, "_dispatch", must_not_dispatch)
    with pytest.raises(ExecutionError, match="not executable"):
        await engine.execute(call["id"])
    assert dispatched is False
    assert not (workspace / "must-not-run.txt").exists()


@pytest.mark.asyncio
async def test_restart_fails_unclaimed_read_without_dispatch_or_duplicate_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "private" / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="queued read not claimed"), source="pytest")
    )
    engine = ExecutionEngine(database, workspace, policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.list_dir",
        arguments={"path": "."},
        summary="List workspace entries",
        requester=REQUESTER,
    )
    assert call["approval_id"] is None
    assert call["status"] == "queued"

    await StateService(database).initialize()
    await StateService(database).initialize()

    recovered_call = await engine.get(call["id"])
    recovered_task = await state.get_task(task.id)
    assert recovered_call is not None
    assert recovered_call["status"] == "failed"
    assert "queued but before execution claim" in recovered_call["error"]
    assert "execution not started; not retried" in recovered_call["error"]
    assert "outcome uncertain" not in recovered_call["error"]
    assert recovered_task is not None
    assert recovered_task.status.value == "failed"
    assert recovered_task.error_json is not None
    assert "execution not started; not retried" in recovered_task.error_json["message"]

    audit = await state.list_audit(limit=20)
    not_started = [
        event
        for event in audit
        if event["event_type"] == "execution.not_started"
        and event["payload"].get("tool_call_id") == call["id"]
    ]
    assert len(not_started) == 1
    assert not_started[0]["payload"] == {
        "tool_call_id": call["id"],
        "stage": "queued_before_claim",
        "outcome": "not_started",
        "retry": False,
    }

    dispatched = False

    async def must_not_dispatch(*_: object) -> dict[str, object]:
        nonlocal dispatched
        dispatched = True
        return {}

    monkeypatch.setattr(engine, "_dispatch", must_not_dispatch)
    with pytest.raises(ExecutionError, match="not executable"):
        await engine.execute(call["id"])
    assert dispatched is False


@pytest.mark.asyncio
async def test_unclaimed_read_recovery_audit_failure_rolls_back_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "private" / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="atomic queued recovery"), source="pytest")
    )
    engine = ExecutionEngine(database, workspace, policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.list_dir",
        arguments={"path": "."},
        summary="List workspace entries",
        requester=REQUESTER,
    )

    async def reject_recovery_audit(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("recovery audit persistence rejected")

    monkeypatch.setattr("app.services.state_service.append_audit_event", reject_recovery_audit)
    with pytest.raises(RuntimeError, match="recovery audit persistence rejected"):
        await StateService(database).initialize()

    async with aiosqlite.connect(database) as db:
        tool_status = await (
            await db.execute("SELECT status FROM tool_calls WHERE id=?", (call["id"],))
        ).fetchone()
        task_status = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (task.id,))
        ).fetchone()
        audit_count = int(
            (
                await (
                    await db.execute(
                        """
                        SELECT COUNT(*) FROM audit_events
                        WHERE event_type='execution.not_started'
                        """
                    )
                ).fetchone()
            )[0]
        )
    assert tool_status == ("queued",)
    assert task_status == ("queued",)
    assert audit_count == 0

    monkeypatch.undo()
    await StateService(database).initialize()
    await StateService(database).initialize()
    recovered_call = await engine.get(call["id"])
    recovered_task = await state.get_task(task.id)
    audit = await state.list_audit(limit=20)
    assert recovered_call is not None
    assert recovered_call["status"] == "failed"
    assert recovered_task is not None
    assert recovered_task.status.value == "failed"
    assert (
        sum(
            event["event_type"] == "execution.not_started"
            and event["payload"].get("tool_call_id") == call["id"]
            for event in audit
        )
        == 1
    )


@pytest.mark.asyncio
async def test_restart_audit_failure_rolls_back_recovery_for_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "private" / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="retry recovery audit"), source="pytest")
    )
    engine = ExecutionEngine(database, workspace, policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.list_dir",
        arguments={"path": "."},
        summary="Recover atomically",
        requester=REQUESTER,
    )
    async with aiosqlite.connect(database) as db:
        await db.execute("UPDATE tool_calls SET status='running' WHERE id=?", (call["id"],))
        await db.execute("UPDATE tasks SET status='running' WHERE id=?", (task.id,))
        await db.commit()

    async def reject_recovery_audit(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("recovery audit persistence rejected")

    monkeypatch.setattr("app.services.state_service.append_audit_event", reject_recovery_audit)
    with pytest.raises(RuntimeError, match="recovery audit persistence rejected"):
        await StateService(database).initialize()

    async with aiosqlite.connect(database) as db:
        tool_status = await (
            await db.execute("SELECT status FROM tool_calls WHERE id=?", (call["id"],))
        ).fetchone()
        task_status = await (
            await db.execute("SELECT status FROM tasks WHERE id=?", (task.id,))
        ).fetchone()
        audit_count = int(
            (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM audit_events WHERE event_type='execution.interrupted'"
                    )
                ).fetchone()
            )[0]
        )
    assert tool_status == ("running",)
    assert task_status == ("running",)
    assert audit_count == 0

    monkeypatch.undo()
    await StateService(database).initialize()
    recovered_call = await engine.get(call["id"])
    recovered_task = await state.get_task(task.id)
    audit = await state.list_audit(limit=20)
    assert recovered_call is not None
    assert recovered_call["status"] == "failed"
    assert recovered_task is not None
    assert recovered_task.status.value == "failed"
    assert sum(event["event_type"] == "execution.interrupted" for event in audit) == 1
