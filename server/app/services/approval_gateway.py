from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.services.approval_binding import (
    binding_matches,
    consent_context_from_audit,
    public_tool_summary,
    safe_action_preview,
)
from app.services.audit_log import append_audit_event


class ApprovalConflict(RuntimeError):
    def __init__(self, message: str, record: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.record = record


class ApprovalGateway:
    """Read and decide approvals created atomically with their tool call."""

    _JOINED_SELECT = """
        SELECT a.*,
               c.id AS _linked_tool_call_id,
               c.task_id AS _linked_task_id,
               c.tool_name AS _linked_tool_name,
               c.arguments_json AS _linked_arguments_json,
               e.id AS _audit_id,
               e.trace_id AS _audit_trace_id,
               e.event_type AS _audit_event_type,
               e.actor_type AS _audit_actor_type,
               e.actor_id AS _audit_actor_id,
               e.task_id AS _audit_task_id,
               e.payload_json AS _audit_payload_json,
               e.prev_hash AS _audit_prev_hash,
               e.hash AS _audit_hash,
               e.created_at AS _audit_created_at
        FROM approvals AS a
        LEFT JOIN tool_calls AS c ON c.approval_id=a.id
        LEFT JOIN audit_events AS e ON e.id=a.request_audit_id
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def get(self, approval_id: str) -> dict[str, Any] | None:
        await self.expire_pending()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(f"{self._JOINED_SELECT} WHERE a.id=?", (approval_id,))
            ).fetchone()
        return self._decode(row) if row else None

    async def list_by_status(self, status: str = "pending") -> list[dict[str, Any]]:
        await self.expire_pending()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"{self._JOINED_SELECT} WHERE a.status=? ORDER BY a.created_at DESC",
                    (status,),
                )
            ).fetchall()
        return [self._decode(row) for row in rows]

    async def list_all(self, limit: int = 500) -> list[dict[str, Any]]:
        await self.expire_pending()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"{self._JOINED_SELECT} ORDER BY a.created_at DESC LIMIT ?", (limit,)
                )
            ).fetchall()
        return [self._decode(row) for row in rows]

    async def list_pending(self) -> list[dict[str, Any]]:
        return await self.list_by_status("pending")

    async def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        await self.expire_pending()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"{self._JOINED_SELECT} WHERE a.task_id=? ORDER BY a.created_at ASC",
                    (task_id,),
                )
            ).fetchall()
        return [self._decode(row) for row in rows]

    async def decide(
        self,
        approval_id: str,
        decision: str,
        *,
        actor_id: str,
        user_note: str | None = None,
    ) -> dict[str, Any] | None:
        approval_status = "approved" if decision == "approve" else "denied"
        next_task_status = "queued" if decision == "approve" else "blocked"
        next_tool_status = "queued" if decision == "approve" else "denied"
        decision_json = json.dumps(
            {"decision": decision, "actor_id": actor_id, "user_note": user_note},
            sort_keys=True,
            separators=(",", ":"),
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat()
            row = await (
                await db.execute(f"{self._JOINED_SELECT} WHERE a.id=?", (approval_id,))
            ).fetchone()
            if row is None:
                await db.rollback()
                return None
            record = self._decode(row)
            if record["status"] != "pending":
                await db.rollback()
                raise ApprovalConflict("approval already decided or cancelled", record)
            if str(record["expires_at"]) <= now:
                transitioned = await self._expire_locked(
                    db,
                    str(record["id"]),
                    str(record["task_id"]),
                    str(record["tool_call_id"]) if record.get("tool_call_id") else None,
                    now,
                )
                if not transitioned:
                    await db.rollback()
                    raise ApprovalConflict("approval lost a concurrent state transition", record)
                await db.commit()
                record.update(status="expired", decided_at=now)
                raise ApprovalConflict("approval expired", record)
            if not record["binding_valid"]:
                await db.rollback()
                raise ApprovalConflict("approval action binding is invalid", record)
            if not record["consent_context_valid"]:
                await db.rollback()
                raise ApprovalConflict("approval consent context is invalid", record)

            tool = await (
                await db.execute(
                    """
                    SELECT id,task_id,tool_name,arguments_json,status
                    FROM tool_calls WHERE approval_id=?
                    """,
                    (approval_id,),
                )
            ).fetchone()
            task = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (record["task_id"],))
            ).fetchone()
            if tool is None or task is None:
                await db.rollback()
                raise ApprovalConflict("approval is not linked to a live tool call", record)
            if (
                str(tool["status"]) != "waiting_permission"
                or str(task["status"]) != "waiting_permission"
            ):
                await db.rollback()
                raise ApprovalConflict("approval no longer matches the task state", record)

            approval_cursor = await db.execute(
                """
                UPDATE approvals SET status=?,decided_at=?,decision_json=?
                WHERE id=? AND status='pending' AND expires_at>?
                """,
                (approval_status, now, decision_json, approval_id, now),
            )
            tool_cursor = await db.execute(
                """
                UPDATE tool_calls SET status=?,updated_at=?
                WHERE id=? AND approval_id=? AND status='waiting_permission'
                """,
                (next_tool_status, now, tool["id"], approval_id),
            )
            task_cursor = await db.execute(
                "UPDATE tasks SET status=?,updated_at=? WHERE id=? AND status='waiting_permission'",
                (next_task_status, now, record["task_id"]),
            )
            if (
                approval_cursor.rowcount != 1
                or tool_cursor.rowcount != 1
                or task_cursor.rowcount != 1
            ):
                await db.rollback()
                raise ApprovalConflict("approval lost a concurrent state transition", record)
            await append_audit_event(
                db,
                "approval.decided",
                {"approval_id": approval_id, "decision": decision},
                actor_type="device",
                actor_id=actor_id,
                task_id=record["task_id"],
                trace_id=record["task_id"],
                created_at=now,
            )
            if decision == "deny":
                await append_audit_event(
                    db,
                    "tool.denied",
                    {"tool_call_id": str(tool["id"])},
                    task_id=record["task_id"],
                    trace_id=record["task_id"],
                    created_at=now,
                )
            await db.commit()

        updated = await self.get(approval_id)
        if updated is None:
            raise ApprovalConflict("approval disappeared after its decision")
        return updated

    async def expire_pending(self) -> None:
        """Materialize time-based expiry before returning authoritative state."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat()
            rows = await (
                await db.execute(
                    """
                    SELECT id,task_id,tool_call_id FROM approvals
                    WHERE status='pending' AND expires_at<=?
                    """,
                    (now,),
                )
            ).fetchall()
            for approval_id, task_id, tool_call_id in rows:
                await self._expire_locked(
                    db,
                    str(approval_id),
                    str(task_id),
                    str(tool_call_id) if tool_call_id is not None else None,
                    now,
                )
            await db.commit()

    @staticmethod
    async def _expire_locked(
        db: aiosqlite.Connection,
        approval_id: str,
        task_id: str,
        tool_call_id: str | None,
        now: str,
    ) -> bool:
        approval_cursor = await db.execute(
            "UPDATE approvals SET status='expired',decided_at=? WHERE id=? AND status='pending'",
            (now, approval_id),
        )
        if approval_cursor.rowcount != 1:
            return False
        await db.execute(
            """
            UPDATE tool_calls SET status='cancelled',updated_at=?
            WHERE approval_id=? AND status='waiting_permission'
            """,
            (now, approval_id),
        )
        await db.execute(
            "UPDATE tasks SET status='blocked',updated_at=? WHERE id=? AND status='waiting_permission'",
            (now, task_id),
        )
        payload = {"approval_id": approval_id}
        if tool_call_id is not None:
            payload["tool_call_id"] = tool_call_id
        await append_audit_event(
            db,
            "approval.expired",
            payload,
            task_id=task_id,
            trace_id=task_id,
            created_at=now,
        )
        return True

    @staticmethod
    def _decode(row: aiosqlite.Row) -> dict[str, Any]:
        record = dict(row)
        raw_decision = record.pop("decision_json", None)
        record["decision"] = json.loads(raw_decision) if raw_decision else None
        request_audit_id = record.pop("request_audit_id", None)
        linked_call_id = record.pop("_linked_tool_call_id", None)
        linked_task_id = record.pop("_linked_task_id", None)
        linked_tool_name = record.pop("_linked_tool_name", None)
        raw_arguments = record.pop("_linked_arguments_json", None)
        audit = {
            "id": record.pop("_audit_id", None),
            "trace_id": record.pop("_audit_trace_id", None),
            "event_type": record.pop("_audit_event_type", None),
            "actor_type": record.pop("_audit_actor_type", None),
            "actor_id": record.pop("_audit_actor_id", None),
            "task_id": record.pop("_audit_task_id", None),
            "payload_json": record.pop("_audit_payload_json", None),
            "prev_hash": record.pop("_audit_prev_hash", None),
            "hash": record.pop("_audit_hash", None),
            "created_at": record.pop("_audit_created_at", None),
        }
        arguments: dict[str, Any] | None = None
        if raw_arguments is not None:
            try:
                decoded_arguments = json.loads(str(raw_arguments))
            except json.JSONDecodeError:
                decoded_arguments = None
            if isinstance(decoded_arguments, dict):
                arguments = decoded_arguments
        record["binding_valid"] = bool(
            arguments is not None
            and str(record.get("tool_call_id", "")) == str(linked_call_id)
            and str(record.get("task_id", "")) == str(linked_task_id)
            and str(record.get("action", "")) == str(linked_tool_name)
            and binding_matches(
                record.get("action_digest"),
                tool_call_id=str(linked_call_id),
                tool_name=str(linked_tool_name),
                arguments=arguments,
            )
        )
        tool_name = str(linked_tool_name or record.get("action", "unknown"))
        record["summary"] = public_tool_summary(tool_name)
        record["action_preview"] = safe_action_preview(tool_name, arguments or {})
        context = consent_context_from_audit(
            approval_id=str(record.get("id", "")),
            task_id=str(record.get("task_id", "")),
            tool_call_id=str(linked_call_id or record.get("tool_call_id", "")),
            action_digest=record.get("action_digest"),
            tool_name=tool_name,
            arguments=arguments or {},
            request_audit_id=request_audit_id,
            audit=audit,
        )
        record["requester"] = context.requester if context.valid else None
        record["policy"] = context.policy if context.valid else None
        record["affected_data_summary"] = context.affected_data_summary
        record["audit_id"] = context.audit_id
        record["consent_context_valid"] = context.valid
        return record
