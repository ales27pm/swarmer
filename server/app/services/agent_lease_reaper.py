from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from app.services.audit_log import append_audit_event
from app.services.distributed_state import (
    AgentJobStateMachine,
    DistributedStateConflict,
    TaskStateMachine,
)
from app.services.maintenance_lease import (
    MaintenanceLeaseGuard,
    MaintenanceLeaseLost,
    MaintenanceLeaseService,
)
from app.services.message_board import MessageBoard
from app.services.outbox import OutboxService
from app.services.permission_policy import PermissionPolicy
from app.services.worker_skill_policy import (
    WorkerSkillPolicySnapshot,
    WorkerSkillPolicyStore,
)

AUTOMATIC_RETRY_SKILLS = frozenset({"workspace.list_dir", "workspace.read_text"})
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
_REAP_BATCH_SIZE = 8
_MAX_REAPS_PER_INVOCATION = 128


class AgentLeaseReaper:
    """Recover expired read-only worker leases with generation fencing."""

    def __init__(
        self,
        db_path: Path,
        board: MessageBoard,
        *,
        maintenance_leases: MaintenanceLeaseService | None = None,
        owner_instance_id: str | None = None,
        outbox_instance_id: str | None = None,
        outbox_publication_lease_seconds: int = 30,
        permission_policy: PermissionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self.outbox = OutboxService(
            db_path,
            board,
            instance_id=outbox_instance_id,
            publication_lease_seconds=outbox_publication_lease_seconds,
        )
        self.maintenance_leases = maintenance_leases
        self.owner_instance_id = owner_instance_id
        self.permission_policy = permission_policy
        self.worker_skill_policy = WorkerSkillPolicyStore(db_path, permission_policy)
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise RuntimeError("agent lease reaper clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _can_redistribute(
        skill: str,
        policy_snapshot: WorkerSkillPolicySnapshot | None,
    ) -> bool:
        if skill not in AUTOMATIC_RETRY_SKILLS:
            return False
        if policy_snapshot is None:
            return True
        return policy_snapshot.can_auto_redistribute(skill)

    @staticmethod
    def _skill_is_allowed(
        skill: str,
        policy_snapshot: WorkerSkillPolicySnapshot | None,
    ) -> bool:
        if policy_snapshot is None:
            return True
        return policy_snapshot.is_allowed(skill)

    async def reap_expired(
        self,
        *,
        maintenance_generation: int | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, int]:
        effective_generation = (
            maintenance_guard.generation
            if maintenance_guard is not None
            else maintenance_generation
        )
        if (
            maintenance_guard is not None
            and maintenance_generation is not None
            and maintenance_guard.generation != maintenance_generation
        ):
            raise RuntimeError("agent lease reaper received mismatched maintenance fencing")
        if self.maintenance_leases is not None and effective_generation is None:
            if self.owner_instance_id is None:
                raise RuntimeError("agent lease reaper maintenance owner is not configured")
            lease = await self.maintenance_leases.acquire(
                "agent-lease-reaper", self.owner_instance_id
            )
            if lease is None:
                return {
                    "expired": 0,
                    "requeued": 0,
                    "dead_lettered": 0,
                    "cancelled": 0,
                    "quarantined": 0,
                }
            effective_generation = lease.generation
        counts = {
            "expired": 0,
            "requeued": 0,
            "dead_lettered": 0,
            "cancelled": 0,
            "quarantined": 0,
        }
        while counts["expired"] < _MAX_REAPS_PER_INVOCATION:
            if maintenance_guard is not None:
                await maintenance_guard.renew_now()
            batch_limit = min(
                _REAP_BATCH_SIZE,
                _MAX_REAPS_PER_INVOCATION - counts["expired"],
            )
            batch_counts = {key: 0 for key in counts}
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                try:
                    now = self._now().isoformat()
                    if maintenance_guard is not None:
                        await maintenance_guard.require_current_locked(db)
                    elif self.maintenance_leases is not None:
                        if self.owner_instance_id is None or effective_generation is None:
                            raise RuntimeError(
                                "agent lease reaper requires a current maintenance lease"
                            )
                        await self.maintenance_leases.require_current_locked(
                            db,
                            "agent-lease-reaper",
                            self.owner_instance_id,
                            effective_generation,
                        )
                    policy_snapshot = await self.worker_skill_policy.load_locked(
                        db,
                        now=now,
                    )
                    batch_started = asyncio.get_running_loop().time()
                    rows = list(
                        await (
                            await db.execute(
                                """
                                SELECT j.*,t.status AS task_status
                                FROM agent_jobs AS j JOIN tasks AS t ON t.id=j.task_id
                                WHERE j.status IN ('claimed','running')
                                  AND j.lease_expires_at IS NOT NULL
                                  AND j.lease_expires_at<=?
                                ORDER BY j.lease_expires_at ASC,j.id ASC
                                LIMIT ?
                                """,
                                (now, batch_limit),
                            )
                        ).fetchall()
                    )
                    batch_examined = 0
                    for row in rows:
                        if (
                            batch_examined > 0
                            and maintenance_guard is not None
                            and asyncio.get_running_loop().time() - batch_started
                            >= maintenance_guard.mutation_batch_budget_seconds
                        ):
                            break
                        batch_examined += 1
                        outcome = await self._reap_row_locked(
                            db,
                            row,
                            now,
                            policy_snapshot,
                        )
                        batch_counts["expired"] += 1
                        batch_counts[outcome] += 1
                    if maintenance_guard is not None:
                        await maintenance_guard.require_current_locked(db)
                    elif self.maintenance_leases is not None:
                        if self.owner_instance_id is None or effective_generation is None:
                            raise RuntimeError(
                                "agent lease reaper lost its maintenance fencing context"
                            )
                        await self.maintenance_leases.require_current_locked(
                            db,
                            "agent-lease-reaper",
                            self.owner_instance_id,
                            effective_generation,
                        )
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
            for key, value in batch_counts.items():
                counts[key] += value
            if not rows or (batch_examined == len(rows) and len(rows) < batch_limit):
                break
            # Keep write transactions bounded so the runner can renew its
            # maintenance lease while a large expired-job backlog is drained.
            await asyncio.sleep(0)
        try:
            await self.outbox.drain(maintenance_guard=maintenance_guard)
        except MaintenanceLeaseLost:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, aiosqlite.Error):
            pass
        return counts

    async def _reap_row_locked(
        self,
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
        now: str,
        policy_snapshot: WorkerSkillPolicySnapshot | None,
    ) -> str:
        job_id = str(row["id"])
        task_id = str(row["task_id"])
        agent_id = str(row["claimed_by"]) if row["claimed_by"] else None
        generation = int(row["lease_generation"])
        task_status = str(row["task_status"])
        reason = "remote worker lease expired"
        capability_history = await (
            await db.execute(
                """
                SELECT 1 FROM iphone_capability_requests
                WHERE requesting_job_id=? AND lease_generation=?
                LIMIT 1
                """,
                (job_id, generation),
            )
        ).fetchone()
        if capability_history is not None:
            reason = (
                "remote worker lease expired after iPhone capability activity; "
                "outcome uncertain; not retried"
            )
        await append_audit_event(
            db,
            "agent.job.lease_expired",
            {
                "job_id": job_id,
                "agent_id": agent_id,
                "lease_generation": generation,
            },
            actor_type="control-plane",
            actor_id="lease-reaper",
            task_id=task_id,
            trace_id=task_id,
            created_at=now,
        )
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="agent_job",
            aggregate_id=job_id,
            topic="tasks.status",
            event_type="lease_expired",
            payload={"job_id": job_id, "lease_generation": generation},
            task_id=task_id,
            agent_id=agent_id,
            message_id=job_id,
            dedupe_key=f"agent-job:{job_id}:lease-expired:{generation}",
            created_at=now,
        )
        await self._increment_operational_metrics_locked(
            db,
            now=now,
            lease_expirations=1,
        )
        await self._cancel_capabilities_locked(
            db,
            job_id=job_id,
            lease_generation=generation,
            task_id=task_id,
            agent_id=agent_id,
            now=now,
        )

        if task_status in TERMINAL_TASK_STATUSES:
            await self._transition_job_locked(
                db, row, "cancelled", now, reason="parent task is terminal"
            )
            await self._record_outcome_locked(db, row, now, "cancelled", "parent task is terminal")
            return "cancelled"

        skill = str(row["required_skill"])
        skill_is_allowed = self._skill_is_allowed(skill, policy_snapshot)
        policy_allows_retry = self._can_redistribute(skill, policy_snapshot)
        retryable = (
            capability_history is None
            and policy_allows_retry
            and int(row["attempt_count"]) < int(row["max_attempts"])
        )
        if retryable and task_status == "running":
            await self._transition_job_locked(db, row, "queued", now, reason=reason)
            try:
                await TaskStateMachine.transition_locked(
                    db,
                    task_id=task_id,
                    current="running",
                    target="queued",
                    now=now,
                )
            except DistributedStateConflict as exc:
                raise RuntimeError("parent task changed during lease recovery") from exc
            await self._record_outcome_locked(db, row, now, "requeued", reason)
            return "requeued"

        if capability_history is not None:
            failure_error = reason
        elif not skill_is_allowed:
            reason = "remote worker skill revoked by current policy"
            failure_error = reason
            await self._transition_job_locked(
                db,
                row,
                "quarantined",
                now,
                reason=reason,
                failure_error=failure_error,
            )
            if task_status in {"queued", "running"}:
                try:
                    await TaskStateMachine.transition_locked(
                        db,
                        task_id=task_id,
                        current=task_status,
                        target="failed",
                        now=now,
                        error=failure_error,
                    )
                except DistributedStateConflict as exc:
                    raise RuntimeError("parent task changed during policy quarantine") from exc
            await append_audit_event(
                db,
                "agent.job.skill_revoked",
                {
                    "job_id": job_id,
                    "lease_generation": generation,
                    "required_skill": skill,
                },
                actor_type="control-plane",
                actor_id="lease-reaper",
                task_id=task_id,
                trace_id=task_id,
                created_at=now,
            )
            await self._record_outcome_locked(db, row, now, "quarantined", reason)
            return "quarantined"
        elif not policy_allows_retry:
            reason = (
                "remote worker lease expired; automatic redistribution disabled by policy; "
                "outcome uncertain; not retried"
            )
            failure_error = reason
        else:
            failure_error = "remote worker retry budget exhausted"
        await self._transition_job_locked(
            db,
            row,
            "failed",
            now,
            reason=reason,
            failure_error=failure_error,
        )
        if task_status in {"queued", "running"}:
            try:
                await TaskStateMachine.transition_locked(
                    db,
                    task_id=task_id,
                    current=task_status,
                    target="failed",
                    now=now,
                    error=failure_error,
                )
            except DistributedStateConflict as exc:
                raise RuntimeError("parent task changed during lease failure") from exc
        await self._record_outcome_locked(db, row, now, "dead_lettered", reason)
        return "dead_lettered"

    @staticmethod
    async def _cancel_capabilities_locked(
        db: aiosqlite.Connection,
        *,
        job_id: str,
        lease_generation: int,
        task_id: str,
        agent_id: str | None,
        now: str,
    ) -> None:
        requests = list(
            await (
                await db.execute(
                    """
                    SELECT id FROM iphone_capability_requests
                    WHERE requesting_job_id=? AND lease_generation=?
                      AND status NOT IN ('completed','denied','failed','cancelled','expired')
                    """,
                    (job_id, lease_generation),
                )
            ).fetchall()
        )
        for request in requests:
            request_id = str(request[0])
            await db.execute(
                """
                UPDATE iphone_capability_requests SET status='cancelled',completed_at=?
                WHERE id=? AND status NOT IN ('completed','denied','failed','cancelled','expired')
                """,
                (now, request_id),
            )
            await db.execute(
                "UPDATE iphone_capability_grants SET expires_at=? WHERE request_id=?",
                (now, request_id),
            )
            await append_audit_event(
                db,
                "iphone.capability.cancelled",
                {"request_id": request_id, "reason": "worker lease expired"},
                actor_type="control-plane",
                actor_id="lease-reaper",
                task_id=task_id,
                trace_id=task_id,
                created_at=now,
            )
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="iphone_capability_request",
                aggregate_id=request_id,
                topic="agent.job.capability.result",
                event_type="capability_result",
                payload={"request_id": request_id, "status": "cancelled"},
                task_id=task_id,
                agent_id=agent_id,
                message_id=request_id,
                dedupe_key=f"iphone-capability:{request_id}:lease-cancelled",
                created_at=now,
            )

    @staticmethod
    async def _transition_job_locked(
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
        target: str,
        now: str,
        *,
        reason: str,
        failure_error: str | None = None,
    ) -> None:
        completed_at = None if target == "queued" else now
        try:
            await AgentJobStateMachine.transition_locked(
                db,
                job_id=str(row["id"]),
                current=str(row["status"]),
                target=target,
                now=now,
                updates={
                    "claimed_by": None,
                    "claim_token": None,  # nosec B105 - clear legacy plaintext token
                    "lease_id": None,
                    "lease_token_hash": None,  # nosec B105 - revoke expired lease proof
                    "lease_expires_at": None,
                    "lease_generation": int(row["lease_generation"])
                    + (0 if target == "queued" else 1),
                    "completed_at": completed_at,
                    "last_agent_id": row["claimed_by"],
                    "last_failure_reason": reason,
                    "error": failure_error if target in {"failed", "quarantined"} else None,
                },
                extra_where=" AND lease_generation=? AND lease_expires_at=?",
                where_values=(int(row["lease_generation"]), row["lease_expires_at"]),
            )
        except DistributedStateConflict as exc:
            raise RuntimeError("job changed during lease recovery") from exc

    @staticmethod
    async def _record_outcome_locked(
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
        now: str,
        outcome: str,
        reason: str,
    ) -> None:
        job_id = str(row["id"])
        task_id = str(row["task_id"])
        generation = int(row["lease_generation"])
        event_name = f"agent.job.{outcome}"
        await append_audit_event(
            db,
            event_name,
            {"job_id": job_id, "lease_generation": generation, "reason": reason},
            actor_type="control-plane",
            actor_id="lease-reaper",
            task_id=task_id,
            trace_id=task_id,
            created_at=now,
        )
        is_requeue = outcome == "requeued"
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="agent_job",
            aggregate_id=job_id,
            topic="tasks.inbox" if is_requeue else "tasks.status",
            event_type="published" if is_requeue else outcome,
            payload={
                "job_id": job_id,
                "status": "queued" if is_requeue else outcome,
                "lease_generation": generation + (0 if is_requeue else 1),
            },
            task_id=task_id,
            message_id=job_id,
            dedupe_key=f"agent-job:{job_id}:{outcome}:{generation}",
            created_at=now,
        )
        await AgentLeaseReaper._increment_operational_metrics_locked(
            db,
            now=now,
            retries=1 if outcome == "requeued" else 0,
            dead_letter_events=1 if outcome == "dead_lettered" else 0,
        )

    @staticmethod
    async def _increment_operational_metrics_locked(
        db: aiosqlite.Connection,
        *,
        now: str,
        lease_expirations: int = 0,
        retries: int = 0,
        dead_letter_events: int = 0,
    ) -> None:
        cursor = await db.execute(
            """
            UPDATE agent_job_operational_metrics
            SET lease_expirations=lease_expirations+?,
                retries=retries+?,
                dead_letter_events=dead_letter_events+?,
                updated_at=?
            WHERE singleton_id=1
            """,
            (lease_expirations, retries, dead_letter_events, now),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("agent job operational metrics are unavailable")

    async def metrics(self) -> dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT
                      SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status IN ('claimed','running') THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='failed' AND
                           last_failure_reason LIKE 'remote worker lease expired%' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='quarantined' THEN 1 ELSE 0 END)
                    FROM agent_jobs
                    """
                )
            ).fetchone()
            operational = await (
                await db.execute(
                    """
                    SELECT lease_expirations,retries,dead_letter_events
                    FROM agent_job_operational_metrics WHERE singleton_id=1
                    """
                )
            ).fetchone()
        return {
            "queued_jobs": int(row[0] or 0) if row else 0,
            "leased_jobs": int(row[1] or 0) if row else 0,
            "dead_letter_jobs": int(row[2] or 0) if row else 0,
            "quarantined_jobs": int(row[3] or 0) if row else 0,
            "expired_leases": int(operational[0] or 0) if operational else 0,
            "retries": int(operational[1] or 0) if operational else 0,
            "dead_letter_events": int(operational[2] or 0) if operational else 0,
        }
