from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite


class ExecutionError(RuntimeError):
    pass


class ExecutionEngine:
    """Sandboxed tool executor.

    Models never execute OS actions directly. They submit structured tool proposals;
    this service validates paths/arguments and performs the approved operation.
    """

    SUPPORTED_TOOLS = {
        "workspace.list_dir",
        "workspace.read_text",
        "workspace.write_text",
        "process.run",
    }

    def __init__(self, db_path: Path, workspace_root: Path) -> None:
        self.db_path = db_path
        self.workspace_root = workspace_root.resolve()

    def _resolve_workspace_path(self, raw: str) -> Path:
        candidate = (self.workspace_root / raw).resolve()
        if candidate != self.workspace_root and self.workspace_root not in candidate.parents:
            raise ExecutionError("path escapes configured workspace")
        return candidate

    def validate_tool(self, tool_name: str) -> None:
        if tool_name not in self.SUPPORTED_TOOLS:
            raise ExecutionError(f"unknown tool: {tool_name}")

    def requires_approval(self, tool_name: str) -> bool:
        self.validate_tool(tool_name)
        return tool_name in {"workspace.write_text", "process.run"}

    def default_risk(self, tool_name: str) -> str:
        self.validate_tool(tool_name)
        return {
            "workspace.list_dir": "low",
            "workspace.read_text": "low",
            "workspace.write_text": "medium",
            "process.run": "high",
        }[tool_name]

    async def create_tool_call(
        self,
        *,
        task_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        summary: str,
    ) -> dict[str, Any]:
        from datetime import UTC, datetime
        from uuid import uuid4

        self.validate_tool(tool_name)
        now = datetime.now(UTC).isoformat()
        record = {
            "id": f"call_{uuid4().hex}",
            "task_id": task_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "summary": summary,
            "risk": self.default_risk(tool_name),
            "status": "proposed",
            "approval_id": None,
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO tool_calls(
                    id,task_id,tool_name,arguments_json,summary,risk,status,
                    approval_id,result_json,error,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["id"], task_id, tool_name, json.dumps(arguments), summary,
                    record["risk"], record["status"], None, None, None, now, now,
                ),
            )
            await db.commit()
        return record

    async def attach_approval(self, tool_call_id: str, approval_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tool_calls SET approval_id=?, status='waiting_permission', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (approval_id, tool_call_id),
            )
            await db.commit()

    async def mark_denied(self, tool_call_id: str) -> dict[str, Any] | None:
        await self._set_status(tool_call_id, "denied")
        return await self.get(tool_call_id)

    async def get_by_approval(self, approval_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM tool_calls WHERE approval_id=?", (approval_id,))
            ).fetchone()
        return self._decode_row(row) if row else None

    async def get(self, tool_call_id: str) -> dict[str, Any] | None:
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
        return [self._decode_row(row) for row in rows]

    def _decode_row(self, row: aiosqlite.Row) -> dict[str, Any]:
        value = dict(row)
        value["arguments"] = json.loads(value.pop("arguments_json"))
        raw_result = value.pop("result_json")
        value["result"] = json.loads(raw_result) if raw_result else None
        return value

    async def execute(self, tool_call_id: str) -> dict[str, Any]:
        record = await self.get(tool_call_id)
        if not record:
            raise ExecutionError("tool call not found")

        await self._set_status(tool_call_id, "running")
        try:
            result = await self._dispatch(record["tool_name"], record["arguments"])
        except Exception as exc:
            await self._finish(tool_call_id, "failed", None, str(exc))
            raise

        await self._finish(tool_call_id, "completed", result, None)
        updated = await self.get(tool_call_id)
        assert updated is not None
        return updated

    async def _dispatch(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.validate_tool(tool_name)

        if tool_name == "workspace.list_dir":
            path = self._resolve_workspace_path(str(arguments.get("path", ".")))
            if not path.is_dir():
                raise ExecutionError("directory not found")
            return {"entries": sorted(p.name for p in path.iterdir())[:1000]}

        if tool_name == "workspace.read_text":
            if "path" not in arguments:
                raise ExecutionError("path is required")
            path = self._resolve_workspace_path(str(arguments["path"]))
            if not path.is_file():
                raise ExecutionError("file not found")
            text = path.read_text(encoding="utf-8")
            return {"text": text[:131072], "truncated": len(text) > 131072}

        if tool_name == "workspace.write_text":
            if "path" not in arguments:
                raise ExecutionError("path is required")
            path = self._resolve_workspace_path(str(arguments["path"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            content = str(arguments.get("content", ""))
            if len(content.encode("utf-8")) > 1_000_000:
                raise ExecutionError("write exceeds 1 MB limit")
            path.write_text(content, encoding="utf-8")
            return {
                "path": str(path.relative_to(self.workspace_root)),
                "bytes": len(content.encode("utf-8")),
            }

        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise ExecutionError("argv must be a non-empty string array")
        if len(argv) > 64:
            raise ExecutionError("argv too long")
        cwd = self._resolve_workspace_path(str(arguments.get("cwd", ".")))
        timeout = min(max(float(arguments.get("timeout_seconds", 15)), 0.1), 30.0)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise ExecutionError("process timed out")
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[:65536],
            "stderr": stderr.decode("utf-8", errors="replace")[:65536],
        }

    async def _set_status(self, tool_call_id: str, status: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tool_calls SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, tool_call_id),
            )
            await db.commit()

    async def _finish(
        self,
        tool_call_id: str,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tool_calls SET status=?, result_json=?, error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, json.dumps(result) if result is not None else None, error, tool_call_id),
            )
            await db.commit()
