from __future__ import annotations

import asyncio
import codecs
import json
import math
import os
import sqlite3
import stat
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, TypeVar
from uuid import uuid4

import aiosqlite

from app.services.approval_binding import (
    ApprovalBindingError,
    binding_matches,
    canonical_action_digest,
    consent_context_from_audit,
    public_tool_call,
    public_tool_error,
    public_tool_summary,
    safe_affected_data_summary,
)
from app.services.audit_log import append_audit_event
from app.services.permission_policy import PermissionPolicy
from app.services.process_sandbox import ProcessSandbox, ProcessSandboxError


class ExecutionError(RuntimeError):
    pass


class ExecutionConflict(ExecutionError):
    pass


class ExecutionOutcomeUncertain(ExecutionError):
    """Dispatch ran, but its terminal state and audit could not be committed."""


_T = TypeVar("_T")


@dataclass(frozen=True)
class AuthenticatedRequester:
    id: str
    name: str
    type: str = "device"

    def __post_init__(self) -> None:
        if self.type != "device" or not self.id or not self.name:
            raise ExecutionError("authenticated device requester identity is required")


class ExecutionEngine:
    """Persist and execute validated tool calls against one isolated workspace."""

    SUPPORTED_TOOLS: ClassVar[set[str]] = {
        "workspace.list_dir",
        "workspace.read_text",
        "workspace.write_text",
        "process.run",
    }
    PROPOSABLE_TASK_STATES: ClassVar[frozenset[str]] = frozenset({"created", "planned"})
    TOOL_ARGUMENT_KEYS: ClassVar[dict[str, frozenset[str]]] = {
        "workspace.list_dir": frozenset({"path"}),
        "workspace.read_text": frozenset({"path"}),
        "workspace.write_text": frozenset({"path", "content"}),
        "process.run": frozenset({"argv", "cwd", "timeout_seconds"}),
    }
    MAX_READ_BYTES: ClassVar[int] = 131_072
    MAX_WRITE_BYTES: ClassVar[int] = 1_000_000

    def __init__(
        self,
        db_path: Path,
        workspace_root: Path,
        policy: PermissionPolicy,
    ) -> None:
        self.db_path = db_path
        self.workspace_root = workspace_root.resolve()
        self.policy = policy
        self.process_sandbox = ProcessSandbox(self.workspace_root, policy)

    def _resolve_workspace_path(self, raw: str) -> Path:
        candidate = (self.workspace_root / raw).resolve()
        if candidate != self.workspace_root and self.workspace_root not in candidate.parents:
            raise ExecutionError("path escapes configured workspace")
        return candidate

    def _workspace_relative(self, raw: str, *, allow_root: bool = False) -> Path:
        """Return a lexical relative path suitable for descriptor-relative traversal."""

        if not raw or "\x00" in raw:
            raise ExecutionError("workspace path is empty or invalid")
        raw_path = Path(raw)
        if raw_path.is_absolute():
            raise ExecutionError("path escapes configured workspace")
        parts: list[str] = []
        for part in raw_path.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                raise ExecutionError("path escapes configured workspace")
            parts.append(part)
        if not parts:
            if allow_root:
                return Path(".")
            raise ExecutionError("workspace file path is required")
        return Path(*parts)

    def _assert_not_protected(self, relative: Path) -> None:
        if self.policy.is_protected(relative):
            raise ExecutionError("protected path is not available to tools")

    def _assert_inode_not_protected(self, target: os.stat_result) -> None:
        """Reject a hard-link alias of any protected regular file.

        Ordinary files and safe hard links remain usable. Only multiply-linked
        targets need the descriptor-rooted workspace scan, and traversal fails
        closed if the protected-inode relationship cannot be established safely.
        """

        if target.st_nlink <= 1:
            return
        target_identity = (target.st_dev, target.st_ino)

        def fail_walk(error: OSError) -> None:
            raise ExecutionError("cannot verify workspace hard-link safety") from error

        with self._open_workspace_directory(Path(".")) as workspace_fd:
            for root, _, files, directory_fd in os.fwalk(
                ".",
                onerror=fail_walk,
                follow_symlinks=False,
                dir_fd=workspace_fd,
            ):
                root_relative = Path(root)
                for name in files:
                    relative = root_relative / name
                    if not self.policy.is_protected(relative):
                        continue
                    try:
                        candidate = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError as exc:
                        raise ExecutionError("cannot verify workspace hard-link safety") from exc
                    if (
                        stat.S_ISREG(candidate.st_mode)
                        and (
                            candidate.st_dev,
                            candidate.st_ino,
                        )
                        == target_identity
                    ):
                        raise ExecutionError(
                            "protected file hard-link alias is not available to tools"
                        )

    @staticmethod
    def _directory_open_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW

    @contextmanager
    def _open_workspace_directory(self, relative: Path, *, create: bool = False) -> Iterator[int]:
        """Walk from the workspace fd without ever following a path component."""

        descriptors: list[int] = []
        try:
            current = os.open(self.workspace_root, self._directory_open_flags())
            descriptors.append(current)
            if relative != Path("."):
                for part in relative.parts:
                    try:
                        next_fd = os.open(part, self._directory_open_flags(), dir_fd=current)
                    except FileNotFoundError:
                        if not create:
                            raise
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=current)
                        except FileExistsError:
                            # A racing creator is accepted only if the no-follow open below
                            # proves that it created a real directory.
                            pass
                        next_fd = os.open(part, self._directory_open_flags(), dir_fd=current)
                    descriptors.append(next_fd)
                    current = next_fd
            yield current
        except OSError as exc:
            raise ExecutionError(
                "workspace path is unavailable or contains a symbolic link"
            ) from exc
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @contextmanager
    def _open_workspace_parent(
        self, relative: Path, *, create: bool = False
    ) -> Iterator[tuple[int, str]]:
        parts = relative.parts
        if not parts:
            raise ExecutionError("workspace file path is required")
        parent = Path(*parts[:-1]) if len(parts) > 1 else Path(".")
        with self._open_workspace_directory(parent, create=create) as parent_fd:
            yield parent_fd, parts[-1]

    def _read_workspace_text(self, relative: Path) -> dict[str, Any]:
        with self._open_workspace_parent(relative) as (parent_fd, name):
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise ExecutionError("file not found or is not a safe regular file") from exc
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ExecutionError("workspace read target is not a regular file")
                self._assert_inode_not_protected(metadata)
                remaining = self.MAX_READ_BYTES + 1
                chunks: list[bytes] = []
                while remaining > 0:
                    chunk = os.read(descriptor, min(8192, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            finally:
                os.close(descriptor)

        raw = b"".join(chunks)
        truncated = len(raw) > self.MAX_READ_BYTES
        visible = raw[: self.MAX_READ_BYTES]
        try:
            # With a truncated file, final=False safely withholds an incomplete UTF-8
            # codepoint at the boundary without reading the rest of the file.
            decoder = codecs.getincrementaldecoder("utf-8")()
            text = decoder.decode(visible, final=not truncated)
        except UnicodeDecodeError as exc:
            raise ExecutionError("workspace file is not valid UTF-8 text") from exc
        return {"text": text, "truncated": truncated}

    def _write_workspace_text(self, relative: Path, content: str) -> dict[str, Any]:
        encoded = content.encode("utf-8")
        if len(encoded) > self.MAX_WRITE_BYTES:
            raise ExecutionError("write exceeds 1 MB limit")

        with self._open_workspace_parent(relative, create=True) as (parent_fd, name):
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            except OSError as exc:
                raise ExecutionError("workspace write target is unavailable") from exc
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise ExecutionError("workspace write target is not a regular file")
            if existing is not None:
                self._assert_inode_not_protected(existing)

            temporary = f".mongars-write-{uuid4().hex}"
            descriptor: int | None = None
            installed = False
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK,
                    0o600,
                    dir_fd=parent_fd,
                )
                offset = 0
                while offset < len(encoded):
                    written = os.write(descriptor, encoded[offset:])
                    if written <= 0:
                        raise ExecutionError("workspace write made no progress")
                    offset += written
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                os.replace(
                    temporary,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                installed = True
            except OSError as exc:
                raise ExecutionError("workspace write failed safely") from exc
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                if not installed:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass

        return {"path": relative.as_posix(), "bytes": len(encoded)}

    def validate_tool(self, tool_name: str) -> None:
        if tool_name not in self.SUPPORTED_TOOLS:
            raise ExecutionError(f"unknown tool: {tool_name}")

    def requires_approval(self, tool_name: str) -> bool:
        self.validate_tool(tool_name)
        return self.policy.evaluate_tool(tool_name).decision == "ask"

    def default_risk(self, tool_name: str) -> str:
        self.validate_tool(tool_name)
        return self.policy.evaluate_tool(tool_name).risk

    def validate_arguments(self, tool_name: str, arguments: dict[str, Any]) -> None:
        self.validate_tool(tool_name)
        if set(arguments) - self.TOOL_ARGUMENT_KEYS[tool_name]:
            raise ExecutionError(f"{tool_name} received an unsupported argument")
        if tool_name in {"workspace.read_text", "workspace.write_text"}:
            if "path" not in arguments:
                raise ExecutionError("path is required")
            raw_path = arguments["path"]
            if not isinstance(raw_path, str):
                raise ExecutionError("path must be a string")
            if tool_name == "workspace.write_text" and not isinstance(
                arguments.get("content", ""), str
            ):
                raise ExecutionError("content must be a string")
            relative = self._workspace_relative(raw_path)
            self._assert_not_protected(relative)
        elif tool_name == "workspace.list_dir":
            raw_path = arguments.get("path", ".")
            if not isinstance(raw_path, str):
                raise ExecutionError("path must be a string")
            relative = self._workspace_relative(raw_path, allow_root=True)
            self._assert_not_protected(relative)
        elif tool_name == "process.run":
            cwd = arguments.get("cwd", ".")
            if not isinstance(cwd, str):
                raise ExecutionError("cwd must be a string")
            timeout = arguments.get("timeout_seconds", 15)
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or isinstance(timeout, float)
                and not math.isfinite(timeout)
            ):
                raise ExecutionError("timeout_seconds must be a finite number")
            argv = arguments.get("argv")
            if not isinstance(argv, list):
                raise ExecutionError("argv must be a non-empty string array")
            try:
                self.process_sandbox.validate(argv)
            except ProcessSandboxError as exc:
                raise ExecutionError(str(exc)) from exc
            self._resolve_workspace_path(cwd)

    async def create_tool_call(
        self,
        *,
        task_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        summary: str,
        requester: AuthenticatedRequester,
    ) -> dict[str, Any]:
        """Atomically create one call and, when required, its one-shot approval."""

        self.validate_arguments(tool_name, arguments)
        policy_rule = self.policy.evaluate_tool(tool_name)
        if policy_rule.decision == "deny":
            raise ExecutionError(f"tool denied by policy rule {policy_rule.id}")
        created = datetime.now(UTC)
        now = created.isoformat()
        needs_approval = policy_rule.decision == "ask"
        approval_id = f"apr_{uuid4().hex}" if needs_approval else None
        tool_call_id = f"call_{uuid4().hex}"
        action_digest: str | None = None
        if needs_approval:
            try:
                action_digest = canonical_action_digest(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            except ApprovalBindingError as exc:
                raise ExecutionError(str(exc)) from exc
        if not isinstance(summary, str) or not summary:
            raise ExecutionError("tool proposal summary is required")
        safe_summary = public_tool_summary(tool_name)
        record = {
            "id": tool_call_id,
            "task_id": task_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "summary": safe_summary,
            "risk": policy_rule.risk,
            "status": "waiting_permission" if needs_approval else "queued",
            "approval_id": approval_id,
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            task = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
            ).fetchone()
            if task is None:
                await db.rollback()
                raise ExecutionConflict("task not found")
            task_status = str(task[0])
            if task_status not in self.PROPOSABLE_TASK_STATES:
                await db.rollback()
                raise ExecutionConflict(f"task cannot accept a tool call from {task_status}")

            if approval_id:
                affected_data_summary = safe_affected_data_summary(tool_name, arguments)
                audit_event = await append_audit_event(
                    db,
                    "approval.requested",
                    {
                        "approval_id": approval_id,
                        "task_id": task_id,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "action_digest": action_digest,
                        "requester_name": requester.name,
                        "policy": policy_rule.approval_context(),
                        "affected_data_summary": affected_data_summary,
                    },
                    actor_type=requester.type,
                    actor_id=requester.id,
                    task_id=task_id,
                    trace_id=task_id,
                    created_at=now,
                )
                request_audit_id = int(audit_event["id"])
                approval_ttl_seconds = policy_rule.approval_ttl_seconds
                if approval_ttl_seconds is None:
                    await db.rollback()
                    raise ExecutionError("approval policy is missing its expiry")
                await db.execute(
                    """
                    INSERT INTO approvals(
                        id,task_id,tool_call_id,action_digest,request_audit_id,
                        action,summary,risk,status,
                        created_at,expires_at,decided_at,decision_json
                    ) VALUES(?,?,?,?,?,?,?,?, 'pending',?,?,NULL,NULL)
                    """,
                    (
                        approval_id,
                        task_id,
                        tool_call_id,
                        action_digest,
                        request_audit_id,
                        tool_name,
                        safe_summary,
                        record["risk"],
                        now,
                        (created + timedelta(seconds=approval_ttl_seconds)).isoformat(),
                    ),
                )
            await db.execute(
                """
                INSERT INTO tool_calls(
                    id,task_id,tool_name,arguments_json,summary,risk,status,
                    approval_id,result_json,error,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?,?)
                """,
                (
                    record["id"],
                    task_id,
                    tool_name,
                    json.dumps(arguments, sort_keys=True),
                    safe_summary,
                    record["risk"],
                    record["status"],
                    approval_id,
                    now,
                    now,
                ),
            )
            next_task_status = "waiting_permission" if needs_approval else "queued"
            cursor = await db.execute(
                "UPDATE tasks SET status=?,updated_at=? WHERE id=? AND status=?",
                (next_task_status, now, task_id, task_status),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise ExecutionConflict("task changed while the tool call was proposed")
            await db.commit()
        return public_tool_call(record)

    async def get_by_approval(self, approval_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM tool_calls WHERE approval_id=?", (approval_id,))
            ).fetchone()
        return public_tool_call(self._decode_row(row)) if row else None

    async def get(self, tool_call_id: str) -> dict[str, Any] | None:
        record = await self._get_internal(tool_call_id)
        return public_tool_call(record) if record else None

    async def _get_internal(self, tool_call_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM tool_calls WHERE id=?", (tool_call_id,))
            ).fetchone()
        return self._decode_row(row) if row else None

    async def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM tool_calls WHERE task_id=? ORDER BY created_at ASC", (task_id,)
                )
            ).fetchall()
        return [public_tool_call(self._decode_row(row)) for row in rows]

    @staticmethod
    def _decode_row(row: aiosqlite.Row) -> dict[str, Any]:
        value = dict(row)
        value["arguments"] = json.loads(value.pop("arguments_json"))
        raw_result = value.pop("result_json")
        value["result"] = json.loads(raw_result) if raw_result else None
        return value

    async def execute(self, tool_call_id: str) -> dict[str, Any]:
        record = await self._get_internal(tool_call_id)
        if not record:
            raise ExecutionError("tool call not found")

        _, claim_cancellation = await self._await_critical(self._claim_execution(record))
        if claim_cancellation is not None:
            await self._await_critical(
                self._finish(record, "failed", None, "execution cancelled after claim")
            )
            raise claim_cancellation

        try:
            result = await self._dispatch(record["tool_name"], record["arguments"])
        except BaseException as exc:
            message = str(exc) or (
                "execution cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else type(exc).__name__
            )
            try:
                _, cleanup_cancellation = await self._await_critical(
                    self._finish(record, "failed", None, message)
                )
            except BaseException as finish_exc:
                if isinstance(finish_exc, asyncio.CancelledError):
                    raise
                raise ExecutionOutcomeUncertain(
                    "tool outcome could not be persisted after dispatch"
                ) from finish_exc
            if cleanup_cancellation is not None and not isinstance(exc, asyncio.CancelledError):
                raise cleanup_cancellation from exc
            raise

        if record["tool_name"] == "process.run" and result.get("returncode") != 0:
            error = f"process exited with status {result['returncode']}"
            try:
                _, finish_cancellation = await self._await_critical(
                    self._finish(record, "failed", result, error)
                )
            except BaseException as finish_exc:
                if isinstance(finish_exc, asyncio.CancelledError):
                    raise
                raise ExecutionOutcomeUncertain(
                    "tool outcome could not be persisted after dispatch"
                ) from finish_exc
            if finish_cancellation is not None:
                raise finish_cancellation
            raise ExecutionError(error)

        try:
            _, finish_cancellation = await self._await_critical(
                self._finish(record, "completed", result, None)
            )
        except BaseException as finish_exc:
            if isinstance(finish_exc, asyncio.CancelledError):
                raise
            raise ExecutionOutcomeUncertain(
                "tool outcome could not be persisted after dispatch"
            ) from finish_exc
        if finish_cancellation is not None:
            raise finish_cancellation
        updated = await self.get(tool_call_id)
        if updated is None:
            raise ExecutionError("tool call disappeared after execution")
        return updated

    async def _dispatch(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.validate_tool(tool_name)

        if tool_name == "workspace.list_dir":
            raw_path = arguments.get("path", ".")
            if not isinstance(raw_path, str):
                raise ExecutionError("path must be a string")
            relative = self._workspace_relative(raw_path, allow_root=True)
            self._assert_not_protected(relative)
            with self._open_workspace_directory(relative) as directory_fd:
                names = os.listdir(directory_fd)
            entries = []
            for name in names:
                child = Path(name) if relative == Path(".") else relative / name
                if not self.policy.is_protected(child):
                    entries.append(name)
            return {"entries": sorted(entries)[:1000]}

        if tool_name == "workspace.read_text":
            raw_path = arguments.get("path")
            if not isinstance(raw_path, str):
                raise ExecutionError("path must be a string")
            relative = self._workspace_relative(raw_path)
            self._assert_not_protected(relative)
            return self._read_workspace_text(relative)

        if tool_name == "workspace.write_text":
            raw_path = arguments.get("path")
            content = arguments.get("content", "")
            if not isinstance(raw_path, str):
                raise ExecutionError("path must be a string")
            if not isinstance(content, str):
                raise ExecutionError("content must be a string")
            relative = self._workspace_relative(raw_path)
            self._assert_not_protected(relative)
            return self._write_workspace_text(relative, content)

        argv = arguments.get("argv")
        if not isinstance(argv, list):
            raise ExecutionError("argv must be a non-empty string array")
        raw_cwd = arguments.get("cwd", ".")
        timeout = arguments.get("timeout_seconds", 15)
        if not isinstance(raw_cwd, str):
            raise ExecutionError("cwd must be a string")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or isinstance(timeout, float)
            and not math.isfinite(timeout)
        ):
            raise ExecutionError("timeout_seconds must be a finite number")
        cwd = self._resolve_workspace_path(raw_cwd)
        try:
            result = await self.process_sandbox.run(argv, cwd, timeout)
        except ProcessSandboxError as exc:
            raise ExecutionError(str(exc)) from exc
        return result.as_dict()

    @staticmethod
    async def _await_critical(
        operation: Coroutine[Any, Any, _T],
    ) -> tuple[_T, asyncio.CancelledError | None]:
        """Finish a DB transition even if the caller is cancelled mid-commit.

        The pending cancellation is returned to the caller and re-raised only after
        the transition's outcome is known. This closes the in-process ambiguity where
        SQLite may have committed a claim or finish while the awaiting task was
        cancelled. Host/process death still requires an external lease reaper.
        """

        task = asyncio.create_task(operation)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = exc
        return task.result(), cancellation

    async def _claim_execution(self, record: dict[str, Any]) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    """
                    SELECT task_id,tool_name,arguments_json,status,approval_id
                    FROM tool_calls WHERE id=?
                    """,
                    (record["id"],),
                )
            ).fetchone()
            task = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (record["task_id"],))
            ).fetchone()
            if row is None or task is None or str(row[3]) != "queued" or str(task[0]) != "queued":
                await db.rollback()
                raise ExecutionConflict("tool call is not executable in the current task state")
            try:
                current_arguments = json.loads(str(row[2]))
            except json.JSONDecodeError as exc:
                await db.rollback()
                raise ExecutionConflict("tool call arguments are not valid JSON") from exc
            current_task_id = str(row[0])
            current_tool_name = str(row[1])
            if (
                not isinstance(current_arguments, dict)
                or current_task_id != record["task_id"]
                or current_tool_name != record["tool_name"]
            ):
                await db.rollback()
                raise ExecutionConflict("tool call changed before execution claim")
            approval_id = row[4]
            if self.requires_approval(current_tool_name):
                if approval_id is None:
                    await db.rollback()
                    raise ExecutionConflict("tool call does not have an approved one-shot grant")
                approval = await (
                    await db.execute(
                        """
                        SELECT a.status,a.task_id,a.tool_call_id,a.action,a.action_digest,
                               a.request_audit_id,
                               e.id,e.trace_id,e.event_type,e.actor_type,e.actor_id,e.task_id,
                               e.payload_json,e.prev_hash,e.hash,e.created_at
                        FROM approvals AS a
                        LEFT JOIN audit_events AS e ON e.id=a.request_audit_id
                        WHERE a.id=?
                        """,
                        (approval_id,),
                    )
                ).fetchone()
                if (
                    approval is None
                    or str(approval[0]) != "approved"
                    or str(approval[1]) != current_task_id
                    or str(approval[2]) != record["id"]
                    or str(approval[3]) != current_tool_name
                    or not binding_matches(
                        approval[4],
                        tool_call_id=record["id"],
                        tool_name=current_tool_name,
                        arguments=current_arguments,
                    )
                    or not consent_context_from_audit(
                        approval_id=str(approval_id),
                        task_id=current_task_id,
                        tool_call_id=record["id"],
                        action_digest=approval[4],
                        tool_name=current_tool_name,
                        arguments=current_arguments,
                        request_audit_id=approval[5],
                        audit={
                            "id": approval[6],
                            "trace_id": approval[7],
                            "event_type": approval[8],
                            "actor_type": approval[9],
                            "actor_id": approval[10],
                            "task_id": approval[11],
                            "payload_json": approval[12],
                            "prev_hash": approval[13],
                            "hash": approval[14],
                            "created_at": approval[15],
                        },
                    ).valid
                ):
                    await db.rollback()
                    raise ExecutionConflict("tool call does not match its approved one-shot grant")
            elif approval_id is not None:
                await db.rollback()
                raise ExecutionConflict("read-only tool call unexpectedly has an approval")

            # Dispatch exactly the arguments validated and atomically claimed above.
            record["arguments"] = current_arguments

            tool_cursor = await db.execute(
                "UPDATE tool_calls SET status='running',updated_at=? WHERE id=? AND status='queued'",
                (datetime.now(UTC).isoformat(), record["id"]),
            )
            task_cursor = await db.execute(
                "UPDATE tasks SET status='running',updated_at=? WHERE id=? AND status='queued'",
                (datetime.now(UTC).isoformat(), record["task_id"]),
            )
            if tool_cursor.rowcount != 1 or task_cursor.rowcount != 1:
                await db.rollback()
                raise ExecutionConflict("tool call claim lost a concurrent state transition")
            await db.commit()

    async def _finish(
        self,
        record: dict[str, Any],
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        if status not in {"completed", "failed"}:
            raise ExecutionError("executor can only finish as completed or failed")
        for attempt in range(3):
            try:
                await self._finish_once(record, status, result, error)
                return
            except sqlite3.OperationalError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0)

    async def _finish_once(
        self,
        record: dict[str, Any],
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        result_json = json.dumps(result) if result is not None else None
        task_error = public_tool_error(str(record["tool_name"]), error)
        task_error_json = json.dumps({"message": task_error}) if task_error else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            tool_cursor = await db.execute(
                """
                UPDATE tool_calls SET status=?,result_json=?,error=?,updated_at=?
                WHERE id=? AND task_id=? AND status='running'
                """,
                (
                    status,
                    result_json,
                    error,
                    now,
                    record["id"],
                    record["task_id"],
                ),
            )
            task_cursor = await db.execute(
                """
                UPDATE tasks SET status=?,updated_at=?,completed_at=?,error_json=?
                WHERE id=? AND status='running'
                """,
                (
                    status,
                    now,
                    now,
                    task_error_json,
                    record["task_id"],
                ),
            )
            if tool_cursor.rowcount != 1 or task_cursor.rowcount != 1:
                terminal = await (
                    await db.execute(
                        """
                        SELECT c.status,c.result_json,c.error,t.status,t.error_json
                        FROM tool_calls AS c JOIN tasks AS t ON t.id=c.task_id
                        WHERE c.id=? AND c.task_id=?
                        """,
                        (record["id"], record["task_id"]),
                    )
                ).fetchone()
                if terminal == (status, result_json, error, status, task_error_json):
                    await db.rollback()
                    return
                await db.rollback()
                raise ExecutionConflict(
                    "executor could not atomically finish the task and tool call"
                )
            event_type = "tool.completed" if status == "completed" else "tool.failed"
            payload: dict[str, Any] = {"tool_call_id": record["id"]}
            if status == "completed":
                payload["tool_name"] = record["tool_name"]
            else:
                payload.update({"error": task_error, "durable_status": "failed"})
            await append_audit_event(
                db,
                event_type,
                payload,
                task_id=record["task_id"],
                trace_id=record["task_id"],
                created_at=now,
            )
            await db.commit()
