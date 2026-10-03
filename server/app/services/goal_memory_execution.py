"""Goal-bound admission, accounting and fencing for individual memory requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

import aiosqlite
from pydantic import BaseModel

from app.services.audit_log import append_audit_event
from app.services.model_request_execution import (
    MEMORY_MODEL_ROLES,
    MemoryModelRole,
    ModelExecutionControlError,
)

if TYPE_CHECKING:
    from app.services.goal_manager import GoalManager
    from app.services.maintenance_lease import MaintenanceLeaseGuard

_Result = TypeVar("_Result")


def request_digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class GoalMemoryExecutor:
    manager: GoalManager
    goal_id: str
    conversation_revision: int
    maintenance_guard: MaintenanceLeaseGuard | None = None
    # Only receipts accumulate. Goal identity and admission binding never change.
    _call_ids: list[str] = field(default_factory=list, repr=False, compare=False)
    _failures: list[dict[str, str]] = field(default_factory=list, repr=False, compare=False)

    @property
    def call_ids(self) -> tuple[str, ...]:
        return tuple(self._call_ids)

    @property
    def failures(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(item) for item in self._failures)

    async def _fail_call(self, call_id: str, category: str) -> None:
        cleanup = asyncio.create_task(
            self.manager._finish_model_call(
                call_id,
                status="failed",
                error_category=category,
                maintenance_guard=self.maintenance_guard,
            )
        )
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()

    async def bind_context(
        self,
        context_id: str | None,
        *,
        hints: dict[str, Any],
        selected_card_ids: set[str],
        status: str,
    ) -> None:
        """Keep source revision/hash evidence out of prose, attached to its context."""
        metadata_fields = (
            "source_id",
            "canonical_language",
            "presentation_language",
            "source_revision",
            "canonical_content_sha256",
            "canonical_summary_sha256",
            "canonical_receipt_id",
            "canonical_metadata_sha256",
            "scope",
        )
        sources = {}
        for group in ("successful", "failures", "memory"):
            for index, hint in enumerate(hints.get(group, [])):
                card_id = f"strategy:{group}:{index}"
                if card_id in selected_card_ids:
                    sources[card_id] = {
                        key: hint[key]
                        for key in metadata_fields
                        if isinstance(hint.get(key), str) and len(hint[key]) <= 200
                    }
        receipt = {
            "status": "degraded" if status == "completed" and self._failures else status,
            "conversation_revision": self.conversation_revision,
            "model_call_ids": list(self.call_ids),
            "sources": sources,
            "failed_requests": list(self.failures),
        }
        async with aiosqlite.connect(self.manager.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if self.maintenance_guard is not None:
                await self.maintenance_guard.require_current_locked(db)
            current = await (
                await db.execute(
                    "SELECT status,conversation_revision FROM goal_runs WHERE id=?", (self.goal_id,)
                )
            ).fetchone()
            if (
                current is None
                or current[0] in self.manager.graph.GOAL_TERMINAL
                or int(current[1]) != self.conversation_revision
            ):
                raise ModelExecutionControlError("goal changed after memory retrieval")
            if context_id is not None:
                context = await (
                    await db.execute(
                        "SELECT provenance_json FROM goal_contexts WHERE id=? AND goal_run_id=?",
                        (context_id, self.goal_id),
                    )
                ).fetchone()
                if context is None:
                    raise ModelExecutionControlError("memory context is unavailable")
                provenance = json.loads(context[0])
                provenance["strategy_retrieval"] = receipt
                await db.execute(
                    "UPDATE goal_contexts SET provenance_json=? WHERE id=?",
                    (json.dumps(provenance, ensure_ascii=False), context_id),
                )
                for call_id in self.call_ids:
                    await db.execute(
                        "UPDATE goal_model_calls SET context_id=? WHERE id=? AND goal_run_id=? AND context_id IS NULL",
                        (context_id, call_id, self.goal_id),
                    )
            await append_audit_event(
                db,
                "goal.memory.retrieved",
                {"goal_run_id": self.goal_id, "context_id": context_id, **receipt},
                actor_type="system",
                actor_id=self.manager.instance_id,
                trace_id=self.goal_id,
                created_at=self.manager._now(),
            )
            await db.commit()

    async def execute(
        self,
        *,
        role: MemoryModelRole,
        model_id: str,
        endpoint: str,
        request_body: dict[str, Any],
        operation: Callable[[], Awaitable[_Result]],
    ) -> _Result:
        if role not in MEMORY_MODEL_ROLES or request_body.get("model") != model_id:
            raise ValueError("memory request identity is invalid")
        digest = request_digest({"endpoint": endpoint, "body": request_body})
        reservation = asyncio.create_task(
            self.manager._reserve_model_call(
                self.goal_id,
                role=role,
                model_id=model_id,
                input_digest=digest,
                context_id=None,
                provider_source="ubuntu_local",
                conversation_revision=self.conversation_revision,
                reserved_followup_calls=1,
                maintenance_guard=self.maintenance_guard,
            )
        )
        try:
            call_id = await asyncio.shield(reservation)
        except asyncio.CancelledError:
            # SQLite commit may already have completed on its worker thread.
            # Drain the one reservation; never cancel it and guess its outcome.
            while not reservation.done():
                try:
                    await asyncio.shield(reservation)
                except asyncio.CancelledError:
                    continue
                except (RuntimeError, aiosqlite.Error, OSError, TypeError, ValueError):
                    break
            if not reservation.cancelled() and reservation.exception() is None:
                call_id = reservation.result()
                self._call_ids.append(call_id)
                await self._fail_call(call_id, "cancelled_before_request")
            raise
        self._call_ids.append(call_id)
        try:
            goal = await self.manager.graph.get_goal(self.goal_id)
            if (
                goal is None
                or goal["status"] in self.manager.graph.GOAL_TERMINAL
                or int(goal["conversation_revision"]) != self.conversation_revision
            ):
                raise ModelExecutionControlError("goal changed before memory request")
            async with aiosqlite.connect(self.manager.db_path) as db:
                await self.manager._require_current_model_call_locked(
                    db, call_id, now=self.manager._now()
                )
                lease = await (
                    await db.execute(
                        "SELECT lease_expires_at FROM goal_model_calls WHERE id=?", (call_id,)
                    )
                ).fetchone()
            if lease is None or not lease[0]:
                raise ModelExecutionControlError("memory request lease is unavailable")
            lease_remaining = (
                datetime.fromisoformat(str(lease[0])) - datetime.now(UTC)
            ).total_seconds()
            remaining = min(self.manager._remaining_runtime_seconds(goal), lease_remaining)
            if remaining <= 0:
                raise TimeoutError("goal runtime exhausted before memory request")
            async with asyncio.timeout(remaining):
                result = await operation()
            output_digest = request_digest(result)
            async with aiosqlite.connect(self.manager.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                now = self.manager._now()
                if self.maintenance_guard is not None:
                    await self.maintenance_guard.require_current_locked(db)
                await self.manager._require_current_model_call_locked(db, call_id, now=now)
                # The lease/revision check above does not itself reject a terminal goal.
                live = await (
                    await db.execute("SELECT * FROM goal_runs WHERE id=?", (self.goal_id,))
                ).fetchone()
                if (
                    live is None
                    or live["status"] in self.manager.graph.GOAL_TERMINAL
                    or self.manager._runtime_expired(dict(live))
                ):
                    raise ModelExecutionControlError("goal ended during memory request")
                if not await self.manager._finish_model_call_locked(
                    db, call_id, status="completed", now=now, output_digest=output_digest
                ):
                    raise ModelExecutionControlError("memory request receipt was fenced")
                await db.commit()
            return result
        except (Exception, asyncio.CancelledError) as exc:
            category = (
                "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "context_changed"
                if isinstance(exc, ModelExecutionControlError)
                else "provider_failed"
            )
            self._failures.append({"call_id": call_id, "role": role, "category": category})
            await self._fail_call(call_id, category)
            raise
