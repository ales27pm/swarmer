import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import TaskCreate, TaskRecord
from app.services.approval_gateway import ApprovalGateway
from app.services.execution_engine import (
    AuthenticatedRequester,
    ExecutionEngine,
    ExecutionError,
    ExecutionOutcomeUncertain,
)
from app.services.permission_policy import PermissionPolicy, ProcessPolicy, ToolPermissionRule
from app.services.state_service import StateService


def permission_policy(binary: Path = Path("/definitely/missing/bwrap")) -> PermissionPolicy:
    return PermissionPolicy(
        protected_paths=("**/.env", "**/.env.*", "**/*.key", "**/*token*"),
        tool_rules={
            "workspace.list_dir": ToolPermissionRule(
                "allow-workspace-list", "List workspace files.", "allow", "low"
            ),
            "workspace.read_text": ToolPermissionRule(
                "allow-workspace-read", "Read workspace text.", "allow", "low"
            ),
            "workspace.write_text": ToolPermissionRule(
                "ask-workspace-write", "Writing requires approval.", "ask", "medium", 300
            ),
            "process.run": ToolPermissionRule(
                "ask-sandboxed-process", "Processes require approval.", "ask", "high", 300
            ),
        },
        process=ProcessPolicy(
            backend="bubblewrap",
            binary=binary,
            network="deny",
            allowed_commands=frozenset({"git", "pytest", "python3"}),
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
async def test_workspace_write_and_read(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="write test"), source="pytest"))
    engine = ExecutionEngine(db_path, workspace, permission_policy())

    write_call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.write_text",
        arguments={"path": "notes/hello.txt", "content": "salut swarm"},
        summary="write a test file",
        requester=REQUESTER,
    )
    assert engine.requires_approval(write_call["tool_name"]) is True
    with pytest.raises(ExecutionError, match="not executable"):
        await engine.execute(write_call["id"])

    gateway = ApprovalGateway(db_path)
    approval_id = write_call["approval_id"]
    assert approval_id is not None
    await gateway.decide(approval_id, "approve", actor_id="pytest")
    write_result = await engine.execute(write_call["id"])
    assert write_result["status"] == "completed"
    assert (workspace / "notes/hello.txt").read_text() == "salut swarm"

    read_task = await state.create_task(
        TaskRecord.new(TaskCreate(input="read test"), source="pytest")
    )
    read_call = await engine.create_tool_call(
        task_id=read_task.id,
        tool_name="workspace.read_text",
        arguments={"path": "notes/hello.txt"},
        summary="read the test file",
        requester=REQUESTER,
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
    engine = ExecutionEngine(db_path, workspace, permission_policy())

    with pytest.raises(ExecutionError, match="escapes configured workspace"):
        await engine.create_tool_call(
            task_id="tsk_test",
            tool_name="workspace.read_text",
            arguments={"path": "../secret.txt"},
            summary="attempt escape",
            requester=REQUESTER,
        )


@pytest.mark.asyncio
async def test_protected_paths_are_rejected_before_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await StateService(db_path).initialize()
    engine = ExecutionEngine(db_path, workspace, permission_policy())

    with pytest.raises(ExecutionError, match="protected path"):
        await engine.create_tool_call(
            task_id="tsk_test",
            tool_name="workspace.read_text",
            arguments={"path": ".env"},
            summary="read a secret",
            requester=REQUESTER,
        )


@pytest.mark.asyncio
async def test_protected_file_hardlink_alias_cannot_be_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = workspace / ".env"
    alias = workspace / "notes.txt"
    protected.write_text("DATABASE_PASSWORD=do-not-read", encoding="utf-8")
    os.link(protected, alias)
    engine = ExecutionEngine(tmp_path / "state.db", workspace, permission_policy())

    with pytest.raises(ExecutionError, match="protected file hard-link alias"):
        await engine._dispatch("workspace.read_text", {"path": "notes.txt"})


@pytest.mark.asyncio
async def test_protected_file_hardlink_alias_cannot_be_written(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = workspace / ".env"
    alias = workspace / "notes.txt"
    protected.write_text("DATABASE_PASSWORD=unchanged", encoding="utf-8")
    os.link(protected, alias)
    engine = ExecutionEngine(tmp_path / "state.db", workspace, permission_policy())

    with pytest.raises(ExecutionError, match="protected file hard-link alias"):
        await engine._dispatch(
            "workspace.write_text", {"path": "notes.txt", "content": "replacement"}
        )

    assert protected.read_text(encoding="utf-8") == "DATABASE_PASSWORD=unchanged"
    assert alias.read_text(encoding="utf-8") == "DATABASE_PASSWORD=unchanged"


@pytest.mark.asyncio
async def test_safe_file_hardlinks_remain_usable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = workspace / "source.txt"
    alias = workspace / "alias.txt"
    original.write_text("safe content", encoding="utf-8")
    os.link(original, alias)
    engine = ExecutionEngine(tmp_path / "state.db", workspace, permission_policy())

    read_result = await engine._dispatch("workspace.read_text", {"path": "alias.txt"})
    write_result = await engine._dispatch(
        "workspace.write_text", {"path": "alias.txt", "content": "updated alias"}
    )

    assert read_result == {"text": "safe content", "truncated": False}
    assert write_result == {"path": "alias.txt", "bytes": 13}
    assert original.read_text(encoding="utf-8") == "safe content"
    assert alias.read_text(encoding="utf-8") == "updated alias"


@pytest.mark.asyncio
async def test_process_run_fails_closed_without_bubblewrap(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await StateService(db_path).initialize()
    engine = ExecutionEngine(db_path, workspace, permission_policy())

    with pytest.raises(ExecutionError, match="sandbox backend is unavailable"):
        await engine.create_tool_call(
            task_id="tsk_test",
            tool_name="process.run",
            arguments={"argv": ["pytest"]},
            summary="run tests",
            requester=REQUESTER,
        )


@pytest.mark.parametrize(
    ("tool_name", "arguments", "error"),
    [
        ("workspace.list_dir", {"path": {"private": "value"}}, "path must be a string"),
        ("workspace.read_text", {"path": 7}, "path must be a string"),
        (
            "workspace.write_text",
            {"path": "notes.txt", "content": ["private-value"]},
            "content must be a string",
        ),
        (
            "process.run",
            {"argv": ["pytest"], "cwd": {"private": "value"}},
            "cwd must be a string",
        ),
        (
            "process.run",
            {"argv": ["pytest"], "timeout_seconds": True},
            "timeout_seconds must be a finite number",
        ),
        (
            "process.run",
            {"argv": ["pytest"], "timeout_seconds": float("nan")},
            "timeout_seconds must be a finite number",
        ),
        (
            "process.run",
            {"argv": ["pytest"], "timeout_seconds": float("inf")},
            "timeout_seconds must be a finite number",
        ),
        ("workspace.list_dir", {"path": ".", "private": "value"}, "unsupported argument"),
        (
            "workspace.read_text",
            {"path": "notes.txt", "private": "value"},
            "unsupported argument",
        ),
        (
            "workspace.write_text",
            {"path": "notes.txt", "content": "ok", "private": "value"},
            "unsupported argument",
        ),
        (
            "process.run",
            {"argv": ["pytest"], "environment": {"PRIVATE": "value"}},
            "unsupported argument",
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_argument_shapes_are_rejected_before_persistence(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, Any],
    error: str,
) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="reject malformed arguments"), source="pytest")
    )
    engine = ExecutionEngine(db_path, workspace, permission_policy(Path("/bin/true")))

    with pytest.raises(ExecutionError, match=error):
        await engine.create_tool_call(
            task_id=task.id,
            tool_name=tool_name,
            arguments=arguments,
            summary="private model text",
            requester=REQUESTER,
        )

    async with aiosqlite.connect(db_path) as db:
        call_count = int(
            (await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone())[0]
        )
        approval_count = int(
            (await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone())[0]
        )
        task_status = str(
            (
                await (
                    await db.execute("SELECT status FROM tasks WHERE id=?", (task.id,))
                ).fetchone()
            )[0]
        )
    assert call_count == 0
    assert approval_count == 0
    assert task_status == "created"


@pytest.mark.asyncio
async def test_read_holds_parent_descriptor_across_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    safe = workspace / "safe"
    outside = tmp_path / "outside"
    safe.mkdir(parents=True)
    outside.mkdir()
    (safe / "secret.txt").write_text("inside", encoding="utf-8")
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    engine = ExecutionEngine(tmp_path / "state.db", workspace, permission_policy())

    original_open = os.open
    swapped = False

    def swap_after_parent_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "secret.txt" and dir_fd is not None and not swapped:
            safe.rename(workspace / "detached")
            safe.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_after_parent_open)
    result = await engine._dispatch("workspace.read_text", {"path": "safe/secret.txt"})

    assert swapped is True
    assert result == {"text": "inside", "truncated": False}
    assert (workspace / "safe" / "secret.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.asyncio
async def test_write_holds_parent_descriptor_across_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    safe = workspace / "safe"
    outside = tmp_path / "outside"
    safe.mkdir(parents=True)
    outside.mkdir()
    (outside / "target.txt").write_text("do not touch", encoding="utf-8")
    engine = ExecutionEngine(tmp_path / "state.db", workspace, permission_policy())

    original_open = os.open
    swapped = False

    def swap_before_temporary_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if isinstance(path, str) and path.startswith(".mongars-write-") and not swapped:
            safe.rename(workspace / "detached")
            safe.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_temporary_open)
    result = await engine._dispatch(
        "workspace.write_text", {"path": "safe/target.txt", "content": "inside only"}
    )

    assert swapped is True
    assert result == {"path": "safe/target.txt", "bytes": 11}
    assert (workspace / "detached" / "target.txt").read_text(encoding="utf-8") == "inside only"
    assert (outside / "target.txt").read_text(encoding="utf-8") == "do not touch"


@pytest.mark.asyncio
async def test_read_is_bounded_before_allocating_large_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "large.txt").write_bytes(b"x" * 2_000_000)
    engine = ExecutionEngine(tmp_path / "state.db", workspace, permission_policy())
    original_read = os.read
    requested_sizes: list[int] = []

    def tracked_read(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", tracked_read)
    result = await engine._dispatch("workspace.read_text", {"path": "large.txt"})

    assert result["truncated"] is True
    assert len(result["text"]) == engine.MAX_READ_BYTES
    assert sum(requested_sizes) == engine.MAX_READ_BYTES + 1
    assert max(requested_sizes) <= 8192


@pytest.mark.asyncio
async def test_non_idempotent_process_call_cannot_be_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binary = tmp_path / "bwrap"
    binary.touch(mode=0o700)
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="run once"), source="pytest"))
    engine = ExecutionEngine(db_path, workspace, permission_policy(binary))
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="process.run",
        arguments={"argv": ["pytest"]},
        summary="one non-idempotent execution",
        requester=REQUESTER,
    )
    approval_id = call["approval_id"]
    assert isinstance(approval_id, str)
    await ApprovalGateway(db_path).decide(approval_id, "approve", actor_id="pytest")
    dispatch_count = 0

    async def dispatch_once(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal dispatch_count
        dispatch_count += 1
        assert tool_name == "process.run"
        return {"returncode": 0, "arguments": arguments}

    monkeypatch.setattr(engine, "_dispatch", dispatch_once)
    assert (await engine.execute(call["id"]))["status"] == "completed"
    with pytest.raises(ExecutionError, match="not executable"):
        await engine.execute(call["id"])
    assert dispatch_count == 1


@pytest.mark.asyncio
async def test_approval_request_audit_failure_rolls_back_entire_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="atomic approval audit"), source="pytest")
    )
    engine = ExecutionEngine(db_path, workspace, permission_policy())

    async def reject_audit(*_: object, **__: object) -> dict[str, Any]:
        raise RuntimeError("audit persistence rejected")

    monkeypatch.setattr("app.services.execution_engine.append_audit_event", reject_audit)
    with pytest.raises(RuntimeError, match="audit persistence rejected"):
        await engine.create_tool_call(
            task_id=task.id,
            tool_name="workspace.write_text",
            arguments={"path": "atomic.txt", "content": "must not persist"},
            summary="Atomic approval",
            requester=REQUESTER,
        )

    async with aiosqlite.connect(db_path) as db:
        approval_count = int(
            (await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone())[0]
        )
        call_count = int(
            (await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone())[0]
        )
        task_status = str(
            (
                await (
                    await db.execute("SELECT status FROM tasks WHERE id=?", (task.id,))
                ).fetchone()
            )[0]
        )
    assert approval_count == 0
    assert call_count == 0
    assert task_status == "created"


@pytest.mark.parametrize(
    ("dispatch_fails", "terminal_event"),
    [(False, "tool.completed"), (True, "tool.failed")],
)
@pytest.mark.asyncio
async def test_terminal_audit_failure_rolls_back_tool_and_task_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dispatch_fails: bool,
    terminal_event: str,
) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="atomic terminal audit"), source="pytest")
    )
    engine = ExecutionEngine(db_path, workspace, permission_policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.list_dir",
        arguments={"path": "."},
        summary="Execute once before terminal persistence",
        requester=REQUESTER,
    )
    dispatch_count = 0

    async def dispatch_once(*_: object) -> dict[str, object]:
        nonlocal dispatch_count
        dispatch_count += 1
        if dispatch_fails:
            raise ExecutionError("dispatch failed")
        return {"entries": []}

    async def reject_terminal_audit(
        _: object,
        event_type: str,
        __: dict[str, object],
        **___: object,
    ) -> dict[str, object]:
        assert event_type == terminal_event
        raise RuntimeError("terminal audit persistence rejected")

    monkeypatch.setattr(engine, "_dispatch", dispatch_once)
    monkeypatch.setattr("app.services.execution_engine.append_audit_event", reject_terminal_audit)
    with pytest.raises(
        ExecutionOutcomeUncertain,
        match="tool outcome could not be persisted after dispatch",
    ) as captured:
        await engine.execute(call["id"])
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert str(captured.value.__cause__) == "terminal audit persistence rejected"

    async with aiosqlite.connect(db_path) as db:
        tool_state = await (
            await db.execute(
                "SELECT status,result_json,error FROM tool_calls WHERE id=?", (call["id"],)
            )
        ).fetchone()
        task_state = await (
            await db.execute(
                "SELECT status,completed_at,error_json FROM tasks WHERE id=?", (task.id,)
            )
        ).fetchone()
        audit_count = int(
            (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM audit_events WHERE event_type=?",
                        (terminal_event,),
                    )
                ).fetchone()
            )[0]
        )
    assert dispatch_count == 1
    assert tool_state == ("running", None, None)
    assert task_state == ("running", None, None)
    assert audit_count == 0


@pytest.mark.asyncio
async def test_terminal_retry_after_commit_acknowledgement_loss_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="recover a committed finish"), source="pytest")
    )
    engine = ExecutionEngine(db_path, workspace, permission_policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.list_dir",
        arguments={"path": "."},
        summary="Finish exactly once",
        requester=REQUESTER,
    )
    original_finish_once = engine._finish_once
    finish_attempts = 0

    async def commit_then_lose_acknowledgement(
        record: dict[str, Any],
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        nonlocal finish_attempts
        finish_attempts += 1
        await original_finish_once(record, status, result, error)
        if finish_attempts == 1:
            raise sqlite3.OperationalError("commit acknowledgement lost")

    monkeypatch.setattr(engine, "_finish_once", commit_then_lose_acknowledgement)
    completed = await engine.execute(call["id"])

    async with aiosqlite.connect(db_path) as db:
        tool_state = await (
            await db.execute(
                "SELECT status,result_json,error FROM tool_calls WHERE id=?", (call["id"],)
            )
        ).fetchone()
        task_state = await (
            await db.execute(
                "SELECT status,completed_at,error_json FROM tasks WHERE id=?", (task.id,)
            )
        ).fetchone()
        completion_audits = int(
            (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM audit_events WHERE event_type='tool.completed'"
                    )
                ).fetchone()
            )[0]
        )
    assert completed["status"] == "completed"
    assert finish_attempts == 2
    assert tool_state == ("completed", '{"entries": []}', None)
    assert task_state[0] == "completed"
    assert task_state[1] is not None
    assert task_state[2] is None
    assert completion_audits == 1


@pytest.mark.asyncio
async def test_execution_revalidates_request_audit_context_after_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="revalidate consent context"), source="pytest")
    )
    engine = ExecutionEngine(db_path, workspace, permission_policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.write_text",
        arguments={"path": "must-not-write.txt", "content": "one use"},
        summary="Revalidate request audit",
        requester=REQUESTER,
    )
    approval_id = str(call["approval_id"])
    gateway = ApprovalGateway(db_path)
    approval = await gateway.get(approval_id)
    assert approval is not None
    await gateway.decide(approval_id, "approve", actor_id="pytest")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE audit_events SET actor_id='forged-device' WHERE id=?",
            (approval["audit_id"],),
        )
        await db.commit()

    dispatched = False

    async def must_not_dispatch(*_: object) -> dict[str, Any]:
        nonlocal dispatched
        dispatched = True
        return {}

    monkeypatch.setattr(engine, "_dispatch", must_not_dispatch)
    with pytest.raises(ExecutionError, match="does not match its approved one-shot grant"):
        await engine.execute(call["id"])
    assert dispatched is False
    assert not (workspace / "must-not-write.txt").exists()


@pytest.mark.asyncio
async def test_cancellation_marks_claimed_task_and_tool_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="cancel execution"), source="pytest")
    )
    engine = ExecutionEngine(db_path, workspace, permission_policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.list_dir",
        arguments={"path": "."},
        summary="wait until cancelled",
        requester=REQUESTER,
    )
    started = asyncio.Event()

    async def wait_forever(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        del tool_name, arguments
        started.set()
        await asyncio.Event().wait()
        return {}

    monkeypatch.setattr(engine, "_dispatch", wait_forever)
    execution = asyncio.create_task(engine.execute(call["id"]))
    await asyncio.wait_for(started.wait(), timeout=1)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    async with aiosqlite.connect(db_path) as db:
        tool_status = await (
            await db.execute("SELECT status,error FROM tool_calls WHERE id=?", (call["id"],))
        ).fetchone()
        task_status = await (
            await db.execute("SELECT status,error_json FROM tasks WHERE id=?", (task.id,))
        ).fetchone()
        failed_audits = int(
            (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM audit_events WHERE event_type='tool.failed' "
                        "AND task_id=?",
                        (task.id,),
                    )
                ).fetchone()
            )[0]
        )
    assert tool_status == ("failed", "execution cancelled")
    assert task_status is not None
    assert task_status[0] == "failed"
    assert "execution cancelled" in str(task_status[1])
    assert failed_audits == 1


@pytest.mark.asyncio
async def test_cancellation_during_claim_settles_the_committed_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="claim cancellation"), source="pytest")
    )
    engine = ExecutionEngine(db_path, workspace, permission_policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.list_dir",
        arguments={"path": "."},
        summary="cancel after claim commit",
        requester=REQUESTER,
    )
    original_claim = engine._claim_execution
    claim_committed = asyncio.Event()
    release_claim = asyncio.Event()

    async def delayed_claim(record: dict[str, Any]) -> None:
        await original_claim(record)
        claim_committed.set()
        await release_claim.wait()

    monkeypatch.setattr(engine, "_claim_execution", delayed_claim)
    execution = asyncio.create_task(engine.execute(call["id"]))
    await asyncio.wait_for(claim_committed.wait(), timeout=1)
    execution.cancel()
    release_claim.set()
    with pytest.raises(asyncio.CancelledError):
        await execution

    finished_call = await engine.get(call["id"])
    assert finished_call is not None
    assert finished_call["status"] == "failed"
    assert finished_call["error"] == "execution cancelled after claim"
    finished_task = await state.get_task(task.id)
    assert finished_task is not None
    assert finished_task.status == "failed"


@pytest.mark.asyncio
async def test_cancellation_during_finish_waits_for_terminal_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="finish cancellation"), source="pytest")
    )
    engine = ExecutionEngine(db_path, workspace, permission_policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.list_dir",
        arguments={"path": "."},
        summary="complete despite outer cancellation",
        requester=REQUESTER,
    )
    original_finish = engine._finish
    finish_started = asyncio.Event()
    release_finish = asyncio.Event()

    async def delayed_finish(
        record: dict[str, Any],
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        finish_started.set()
        await release_finish.wait()
        await original_finish(record, status, result, error)

    monkeypatch.setattr(engine, "_finish", delayed_finish)
    execution = asyncio.create_task(engine.execute(call["id"]))
    await asyncio.wait_for(finish_started.wait(), timeout=1)
    execution.cancel()
    release_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await execution

    finished_call = await engine.get(call["id"])
    assert finished_call is not None
    assert finished_call["status"] == "completed"
    finished_task = await state.get_task(task.id)
    assert finished_task is not None
    assert finished_task.status == "completed"


@pytest.mark.asyncio
async def test_finish_retries_transient_database_error_without_redispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = StateService(db_path)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="retry finish"), source="pytest")
    )
    engine = ExecutionEngine(db_path, workspace, permission_policy())
    call = await engine.create_tool_call(
        task_id=task.id,
        tool_name="workspace.list_dir",
        arguments={"path": "."},
        summary="retry only terminal persistence",
        requester=REQUESTER,
    )
    original_finish_once = engine._finish_once
    finish_attempts = 0

    async def flaky_finish_once(
        record: dict[str, Any],
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        nonlocal finish_attempts
        finish_attempts += 1
        if finish_attempts == 1:
            raise sqlite3.OperationalError("transient test lock")
        await original_finish_once(record, status, result, error)

    dispatch_count = 0

    async def counted_dispatch(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal dispatch_count
        dispatch_count += 1
        return await ExecutionEngine._dispatch(engine, tool_name, arguments)

    monkeypatch.setattr(engine, "_finish_once", flaky_finish_once)
    monkeypatch.setattr(engine, "_dispatch", counted_dispatch)
    assert (await engine.execute(call["id"]))["status"] == "completed"
    assert finish_attempts == 2
    assert dispatch_count == 1
