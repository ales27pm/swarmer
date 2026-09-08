from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

import aiosqlite


class DistributedStateConflict(RuntimeError):
    pass


class TaskStateMachine:
    ALLOWED: ClassVar[dict[str, frozenset[str]]] = {
        "created": frozenset({"queued", "cancelled"}),
        "planned": frozenset({"queued", "cancelled"}),
        "queued": frozenset({"running", "cancelled", "failed"}),
        "running": frozenset({"queued", "completed", "failed", "cancelled"}),
        "waiting_permission": frozenset({"cancelled"}),
        "blocked": frozenset({"cancelled"}),
        "completed": frozenset(),
        "failed": frozenset(),
        "cancelled": frozenset(),
    }

    @classmethod
    async def transition_locked(
        cls,
        db: aiosqlite.Connection,
        *,
        task_id: str,
        current: str,
        target: str,
        now: str,
        error: str | None = None,
    ) -> None:
        if target not in cls.ALLOWED.get(current, frozenset()):
            raise DistributedStateConflict(f"task cannot transition from {current} to {target}")
        terminal = target in {"completed", "failed", "cancelled"}
        cursor = await db.execute(
            """
            UPDATE tasks SET status=?,updated_at=?,completed_at=?,error_json=?
            WHERE id=? AND status=?
            """,
            (
                target,
                now,
                now if terminal else None,
                json.dumps({"message": error}) if error else None,
                task_id,
                current,
            ),
        )
        if cursor.rowcount != 1:
            raise DistributedStateConflict("task changed during distributed transition")


class AgentJobStateMachine:
    ALLOWED: ClassVar[dict[str, frozenset[str]]] = {
        "queued": frozenset({"claimed", "cancelled", "failed"}),
        "claimed": frozenset({"running", "queued", "completed", "failed", "cancelled"}),
        "running": frozenset({"running", "queued", "completed", "failed", "cancelled"}),
        "completed": frozenset(),
        "failed": frozenset(),
        "cancelled": frozenset(),
    }
    UPDATE_COLUMNS = frozenset(
        {
            "claimed_by",
            "claim_token",
            "lease_id",
            "lease_token_hash",
            "lease_expires_at",
            "lease_generation",
            "result_json",
            "error",
            "claimed_at",
            "heartbeat_at",
            "completed_at",
            "attempt_count",
            "last_agent_id",
            "last_failure_reason",
        }
    )

    @classmethod
    async def transition_locked(
        cls,
        db: aiosqlite.Connection,
        *,
        job_id: str,
        current: str,
        target: str,
        now: str,
        updates: Mapping[str, Any] | None = None,
        extra_where: str = "",
        where_values: Iterable[Any] = (),
    ) -> None:
        if target not in cls.ALLOWED.get(current, frozenset()):
            raise DistributedStateConflict(f"job cannot transition from {current} to {target}")
        values = dict(updates or {})
        unsupported = set(values) - cls.UPDATE_COLUMNS
        if unsupported:
            raise ValueError(f"unsupported agent job update columns: {sorted(unsupported)}")
        assignments = ["status=?", "updated_at=?", *[f"{key}=?" for key in values]]
        parameters = [target, now, *values.values(), job_id, current, *where_values]
        cursor = await db.execute(
            f"UPDATE agent_jobs SET {','.join(assignments)} WHERE id=? AND status=?{extra_where}",  # nosec B608 - internal allowlisted SQL
            parameters,
        )
        if cursor.rowcount != 1:
            raise DistributedStateConflict("job changed during distributed transition")
