from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.services.agent_lease import lease_matches, lease_token_hash
from app.services.agent_liveness import DEFAULT_AGENT_OFFLINE_TIMEOUT_SECONDS
from app.services.agent_scheduler import SchedulerService
from app.services.audit_log import append_audit_event
from app.services.distributed_state import (
    AgentJobStateMachine,
    DistributedStateConflict,
    TaskStateMachine,
)
from app.services.maintenance_lease import MaintenanceLeaseGuard
from app.services.message_board import MessageBoard
from app.services.outbox import OutboxService
from app.services.permission_policy import PermissionPolicy, PermissionPolicyError
from app.services.remote_job_policy import RemoteJobPolicyError, validate_remote_job
from app.services.worker_skill_policy import WorkerSkillPolicyStore

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "quarantined"})
DISPATCHABLE_TASK_STATUSES = frozenset({"created", "planned"})


class AgentDispatchConflict(RuntimeError):
    pass


class AgentDispatcher:
    QUARANTINE_BATCH_SIZE = 32
    CLAIM_QUARANTINE_BATCH_SIZE = 1
    CLAIM_CANDIDATE_LIMIT = 64

    def __init__(
        self,
        db_path: Path,
        board: MessageBoard,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 3,
        outbox_instance_id: str | None = None,
        outbox_publication_lease_seconds: int = 30,
        agent_offline_timeout_seconds: int = DEFAULT_AGENT_OFFLINE_TIMEOUT_SECONDS,
        permission_policy: PermissionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self.board = board
        self.outbox = OutboxService(
            db_path,
            board,
            instance_id=outbox_instance_id,
            publication_lease_seconds=outbox_publication_lease_seconds,
        )
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.permission_policy = permission_policy
        self.worker_skill_policy = WorkerSkillPolicyStore(db_path, permission_policy)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.scheduler = SchedulerService(
            db_path,
            offline_timeout_seconds=agent_offline_timeout_seconds,
            permission_policy=permission_policy,
            clock=self.clock,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise RuntimeError("agent dispatcher clock must be timezone-aware")
        return value.astimezone(UTC)

    async def _drain_outbox(self) -> None:
        try:
            await self.outbox.drain()
        except (OSError, RuntimeError, TypeError, ValueError, aiosqlite.Error):
            # Domain state is already committed. Startup and periodic maintenance
            # retry the durable entry instead of failing the worker request.
            return

    @staticmethod
    def _credential_hash(credential: str) -> str:
        return f"sha256:{hashlib.sha256(credential.encode('utf-8')).hexdigest()}"

    async def authenticate(self, agent_id: str, credential: str) -> dict[str, Any] | None:
        if not credential:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT id,skills_json,status,max_concurrency
                    FROM agents WHERE id=? AND auth_token_hash=?
                    """,
                    (agent_id, self._credential_hash(credential)),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "skills": json.loads(str(row["skills_json"])),
            "status": str(row["status"]),
            "max_concurrency": int(row["max_concurrency"]),
        }

    async def queue_job(
        self,
        task_id: str,
        required_skill: str,
        payload: dict[str, Any],
        *,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> dict[str, Any]:
        try:
            payload = validate_remote_job(required_skill, payload)
        except RemoteJobPolicyError as exc:
            raise AgentDispatchConflict(str(exc)) from exc
        try:
            encoded_payload = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise AgentDispatchConflict("job payload must be canonical JSON") from exc
        max_payload_bytes = 4_000_000 if required_skill == "code.build_project" else 1_000_000
        if len(encoded_payload.encode("utf-8")) > max_payload_bytes:
            raise AgentDispatchConflict("job payload is too large")
        job_id = f"job_{uuid4().hex}"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            now = self._now().isoformat()
            try:
                policy_snapshot = await self.worker_skill_policy.load_locked(db, now=now)
            except PermissionPolicyError as exc:
                await db.rollback()
                raise AgentDispatchConflict(
                    "authoritative remote worker policy is unavailable"
                ) from exc
            if policy_snapshot is not None and not policy_snapshot.is_allowed(required_skill):
                await db.rollback()
                raise AgentDispatchConflict("remote worker skill is denied by policy")
            task = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
            ).fetchone()
            if task is None:
                await db.rollback()
                raise AgentDispatchConflict("task not found")
            task_status = str(task["status"])
            if task_status not in DISPATCHABLE_TASK_STATUSES:
                await db.rollback()
                raise AgentDispatchConflict(
                    f"task cannot receive a remote job from status {task_status}"
                )
            active = await (
                await db.execute(
                    """
                    SELECT id FROM agent_jobs
                    WHERE task_id=? AND status IN ('queued','claimed','running')
                    LIMIT 1
                    """,
                    (task_id,),
                )
            ).fetchone()
            if active is not None:
                await db.rollback()
                raise AgentDispatchConflict("task already owns an active remote job")
            try:
                await db.execute(
                    """
                    INSERT INTO agent_jobs(
                        id,task_id,required_skill,payload_json,status,max_attempts,
                        created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        job_id,
                        task_id,
                        required_skill,
                        encoded_payload,
                        "queued",
                        1
                        if required_skill in {"code.generate_python", "code.build_project"}
                        else self.max_attempts,
                        now,
                        now,
                    ),
                )
                await TaskStateMachine.transition_locked(
                    db,
                    task_id=task_id,
                    current=task_status,
                    target="queued",
                    now=now,
                )
            except (aiosqlite.IntegrityError, DistributedStateConflict) as exc:
                await db.rollback()
                raise AgentDispatchConflict(
                    "task cannot acquire another active remote job"
                ) from exc
            await append_audit_event(
                db,
                "agent.job.queued",
                {"job_id": job_id, "required_skill": required_skill},
                actor_type="control-plane",
                actor_id="dispatcher",
                task_id=task_id,
                trace_id=task_id,
                created_at=now,
            )
            await self.outbox.enqueue_locked(
                db,
                aggregate_type="agent_job",
                aggregate_id=job_id,
                topic="tasks.inbox",
                event_type="published",
                payload={"job_id": job_id, "required_skill": required_skill},
                task_id=task_id,
                message_id=job_id,
                dedupe_key=f"agent-job:{job_id}:queued",
                created_at=now,
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        await self._drain_outbox()
        record = await self.get_job(job_id)
        if record is None:
            raise RuntimeError("queued job disappeared")
        return record

    async def install_worker_skill_policy(self, candidate: PermissionPolicy) -> bool:
        """Persist one validated worker policy epoch and quarantine revoked queues."""

        _, changed = await self.worker_skill_policy.replace(candidate)
        await self.quarantine_revoked_jobs()
        return changed

    async def reload_worker_skill_policy(self, path: Path) -> bool:
        """Validate and persist the worker policy, retaining the last valid epoch on error."""

        _, changed = await self.worker_skill_policy.reload_from_path(path)
        await self.quarantine_revoked_jobs()
        return changed

    async def _quarantine_denied_queued_locked(
        self,
        db: aiosqlite.Connection,
        *,
        now: str,
        denied_skills: tuple[str, ...],
        limit: int,
    ) -> int:
        """Terminally quarantine one bounded batch of denied queued jobs."""

        if not denied_skills:
            return 0
        if limit < 1:
            raise ValueError("quarantine batch limit must be positive")
        placeholders = ",".join("?" for _ in denied_skills)
        rows = await (
            await db.execute(
                f"""
                SELECT j.id,j.task_id,j.required_skill,t.status AS task_status
                FROM agent_jobs AS j JOIN tasks AS t ON t.id=j.task_id
                WHERE j.status='queued' AND j.required_skill IN ({placeholders})
                ORDER BY j.created_at ASC,j.id ASC
                LIMIT ?
                """,  # nosec B608 - placeholders derive only from bounded policy rules
                (*denied_skills, limit),
            )
        ).fetchall()
        quarantined = 0
        reason = "remote worker skill revoked by current policy"
        for row in rows:
            skill = str(row["required_skill"])
            task_id = str(row["task_id"])
            job_id = str(row["id"])
            try:
                await AgentJobStateMachine.transition_locked(
                    db,
                    job_id=job_id,
                    current="queued",
                    target="quarantined",
                    now=now,
                    updates={
                        "completed_at": now,
                        "last_failure_reason": reason,
                        "error": reason,
                    },
                )
                if str(row["task_status"]) == "queued":
                    await TaskStateMachine.transition_locked(
                        db,
                        task_id=task_id,
                        current="queued",
                        target="failed",
                        now=now,
                        error=reason,
                    )
            except DistributedStateConflict as exc:
                raise AgentDispatchConflict("job changed during policy quarantine") from exc
            for event_type in ("agent.job.skill_revoked", "agent.job.quarantined"):
                await append_audit_event(
                    db,
                    event_type,
                    {"job_id": job_id, "required_skill": skill, "reason": reason},
                    actor_type="control-plane",
                    actor_id="dispatcher",
                    task_id=task_id,
                    trace_id=task_id,
                    created_at=now,
                )
            await self.outbox.enqueue_locked(
                db,
                aggregate_type="agent_job",
                aggregate_id=job_id,
                topic="tasks.status",
                event_type="quarantined",
                payload={"job_id": job_id, "status": "quarantined"},
                task_id=task_id,
                message_id=job_id,
                dedupe_key=f"agent-job:{job_id}:skill-revoked",
                created_at=now,
            )
            quarantined += 1
        return quarantined

    @staticmethod
    async def _yield_quarantine_batch() -> None:
        # Give unrelated authoritative writers a scheduling opportunity after
        # every committed batch in a large revoked backlog.
        await asyncio.sleep(0)

    async def quarantine_revoked_jobs(self) -> int:
        """Quarantine revoked queued work in short, writer-friendly batches."""

        total_quarantined = 0
        while True:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                try:
                    now = self._now().isoformat()
                    policy_snapshot = await self.worker_skill_policy.load_locked(db, now=now)
                    denied_skills = (
                        policy_snapshot.denied_skills if policy_snapshot is not None else ()
                    )
                    quarantined = await self._quarantine_denied_queued_locked(
                        db,
                        now=now,
                        denied_skills=denied_skills,
                        limit=self.QUARANTINE_BATCH_SIZE,
                    )
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
            total_quarantined += quarantined
            if quarantined == 0:
                break
            await self._yield_quarantine_batch()
            if quarantined < self.QUARANTINE_BATCH_SIZE:
                break
        if total_quarantined:
            await self._drain_outbox()
        return total_quarantined

    async def claim(self, agent_id: str) -> dict[str, Any] | None:
        lease_id = f"lease_{uuid4().hex}"
        lease_token = secrets.token_urlsafe(32)
        token_hash = lease_token_hash(lease_token)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            # Writer contention may have delayed this transaction beyond an
            # entire lease TTL. Authoritative claim/expiry timestamps must be
            # derived only after the write lock is actually held.
            claimed_at = self._now()
            now = claimed_at.isoformat()
            lease_expires_at = (claimed_at + timedelta(seconds=self.lease_seconds)).isoformat()
            try:
                policy_snapshot = await self.worker_skill_policy.load_locked(db, now=now)
            except PermissionPolicyError as exc:
                await db.rollback()
                raise AgentDispatchConflict(
                    "authoritative remote worker policy is unavailable"
                ) from exc
            allowed_skills = policy_snapshot.allowed_skills if policy_snapshot is not None else None
            denied_skills = policy_snapshot.denied_skills if policy_snapshot is not None else ()
            agent = await self.scheduler.agent_state_locked(db, agent_id)
            if agent is None:
                await db.rollback()
                raise AgentDispatchConflict("agent not found")
            if str(agent["status"]) != "online" or not self.scheduler.is_fresh(
                agent, now=claimed_at
            ):
                await db.rollback()
                raise AgentDispatchConflict("agent is not online and eligible for new work")
            quarantined = await self._quarantine_denied_queued_locked(
                db,
                now=now,
                denied_skills=denied_skills,
                limit=self.CLAIM_QUARANTINE_BATCH_SIZE,
            )
            if int(agent["active_jobs"]) >= int(agent["max_concurrency"]):
                if quarantined:
                    await db.commit()
                else:
                    await db.rollback()
                return None
            skills = agent["skills"]
            if not isinstance(skills, list) or not skills:
                if quarantined:
                    await db.commit()
                else:
                    await db.rollback()
                return None
            if allowed_skills is not None:
                skills = [skill for skill in skills if skill in allowed_skills]
            if not skills:
                if quarantined:
                    await db.commit()
                else:
                    await db.rollback()
                return None
            placeholders = ",".join("?" for _ in skills)
            rows = await (
                await db.execute(
                    f"""
                    WITH ranked_candidates AS (
                        SELECT j.*,t.priority AS scheduler_task_priority,
                               ROW_NUMBER() OVER (
                                   PARTITION BY j.required_skill
                                   ORDER BY t.priority DESC,j.created_at ASC,j.id ASC
                               ) AS scheduler_skill_rank
                        FROM agent_jobs j JOIN tasks t ON t.id=j.task_id
                        WHERE j.status='queued' AND j.required_skill IN ({placeholders})
                          AND j.attempt_count<j.max_attempts AND t.status='queued'
                    )
                    SELECT * FROM ranked_candidates
                    WHERE scheduler_skill_rank<=?
                    ORDER BY scheduler_task_priority DESC,created_at ASC,id ASC
                    """,  # nosec B608 - placeholders derive only from the list length
                    (*skills, self.CLAIM_CANDIDATE_LIMIT),
                )
            ).fetchall()
            row: aiosqlite.Row | None = None
            selection = None
            for candidate in rows:
                candidate_selection = await self.scheduler.select_for_job_locked(
                    db, str(candidate["id"]), now=claimed_at
                )
                if (
                    candidate_selection is not None
                    and candidate_selection.selected_agent_id == agent_id
                ):
                    row = candidate
                    selection = candidate_selection
                    break
            if row is None:
                if quarantined:
                    await db.commit()
                else:
                    await db.rollback()
                return None
            if selection is None:  # pragma: no cover - row and selection are assigned together
                await db.rollback()
                raise RuntimeError("scheduler selection disappeared")
            decision_id = await self.scheduler.record_decision_locked(
                db,
                job_id=str(row["id"]),
                selection=selection,
                created_at=now,
            )
            generation = int(row["lease_generation"]) + 1
            attempt_count = int(row["attempt_count"]) + 1
            try:
                await AgentJobStateMachine.transition_locked(
                    db,
                    job_id=str(row["id"]),
                    current="queued",
                    target="claimed",
                    now=now,
                    updates={
                        "claimed_by": agent_id,
                        "claim_token": None,  # nosec B105 - clear legacy plaintext token
                        "lease_id": lease_id,
                        "lease_token_hash": token_hash,
                        "lease_expires_at": lease_expires_at,
                        "lease_generation": generation,
                        "claimed_at": now,
                        "heartbeat_at": now,
                        "attempt_count": attempt_count,
                        "last_agent_id": agent_id,
                        "last_failure_reason": None,
                    },
                )
                await TaskStateMachine.transition_locked(
                    db,
                    task_id=str(row["task_id"]),
                    current="queued",
                    target="running",
                    now=now,
                )
            except DistributedStateConflict as exc:
                await db.rollback()
                raise AgentDispatchConflict(str(exc)) from exc
            await append_audit_event(
                db,
                "agent.job.claimed",
                {
                    "job_id": str(row["id"]),
                    "agent_id": agent_id,
                    "lease_generation": generation,
                    "scheduler_decision_id": decision_id,
                },
                actor_type="agent",
                actor_id=agent_id,
                task_id=str(row["task_id"]),
                trace_id=str(row["task_id"]),
                created_at=now,
            )
            await self.outbox.enqueue_locked(
                db,
                aggregate_type="agent_job",
                aggregate_id=str(row["id"]),
                topic="tasks.inbox",
                event_type="claimed",
                payload={
                    "job_id": str(row["id"]),
                    "agent_id": agent_id,
                    "lease_generation": generation,
                },
                task_id=str(row["task_id"]),
                agent_id=agent_id,
                message_id=str(row["id"]),
                dedupe_key=f"agent-job:{row['id']}:claimed:{generation}",
                created_at=now,
            )
            await db.commit()
        await self._drain_outbox()
        record = await self.get_job(str(row["id"]))
        if record is None:
            raise RuntimeError("claimed job disappeared")
        return {**record, "claim_token": lease_token}

    def _lease_matches(
        self,
        row: aiosqlite.Row,
        *,
        agent_id: str,
        claim_token: str,
        lease_id: str,
        lease_generation: int,
        now: str,
        require_unexpired: bool,
    ) -> bool:
        return lease_matches(
            dict(row),
            agent_id=agent_id,
            token=claim_token,
            lease_id=lease_id,
            lease_generation=lease_generation,
            now=now,
            require_unexpired=require_unexpired,
        )

    async def heartbeat(
        self,
        agent_id: str,
        job_id: str,
        claim_token: str,
        *,
        lease_id: str,
        lease_generation: int,
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            heartbeat_at = self._now()
            now = heartbeat_at.isoformat()
            renewed_until = (heartbeat_at + timedelta(seconds=self.lease_seconds)).isoformat()
            row = await (
                await db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,))
            ).fetchone()
            if row is None or str(row["status"]) not in {"claimed", "running"}:
                await db.rollback()
                raise AgentDispatchConflict("job is not active")
            if not self._lease_matches(
                row,
                agent_id=agent_id,
                claim_token=claim_token,
                lease_id=lease_id,
                lease_generation=lease_generation,
                now=now,
                require_unexpired=True,
            ):
                await db.rollback()
                raise AgentDispatchConflict(
                    "job lease is stale, expired, or owned by another agent"
                )
            task = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (row["task_id"],))
            ).fetchone()
            if task is None or str(task["status"]) != "running":
                await db.rollback()
                raise AgentDispatchConflict("parent task is not active")
            try:
                await AgentJobStateMachine.transition_locked(
                    db,
                    job_id=job_id,
                    current=str(row["status"]),
                    target="running",
                    now=now,
                    updates={"heartbeat_at": now, "lease_expires_at": renewed_until},
                    extra_where=" AND lease_generation=? AND lease_token_hash=?",
                    where_values=(int(row["lease_generation"]), row["lease_token_hash"]),
                )
            except DistributedStateConflict as exc:
                await db.rollback()
                raise AgentDispatchConflict(str(exc)) from exc
            await db.execute(
                """
                UPDATE agents
                SET last_heartbeat_at=?,last_seen_at=?,updated_at=?
                WHERE id=?
                """,
                (now, now, now, agent_id),
            )
            await self.outbox.enqueue_locked(
                db,
                aggregate_type="agent_job",
                aggregate_id=job_id,
                topic="agents.heartbeat",
                event_type="heartbeat",
                payload={
                    "job_id": job_id,
                    "agent_id": agent_id,
                    "lease_generation": int(row["lease_generation"]),
                },
                agent_id=agent_id,
                message_id=job_id,
                dedupe_key=(f"agent-job:{job_id}:heartbeat:{int(row['lease_generation'])}"),
                created_at=now,
            )
            await db.commit()
        await self._drain_outbox()
        record = await self.get_job(job_id)
        if record is None:
            raise RuntimeError("active job disappeared")
        return record

    async def submit_result(
        self,
        agent_id: str,
        job_id: str,
        claim_token: str,
        *,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
        lease_id: str,
        lease_generation: int,
    ) -> tuple[dict[str, Any], bool]:
        if status not in {"completed", "failed"}:
            raise AgentDispatchConflict("result status must be completed or failed")
        try:
            result_json = (
                json.dumps(
                    result,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if result is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise AgentDispatchConflict("job result must be canonical JSON") from exc
        if result_json is not None and len(result_json.encode("utf-8")) > 4_000_000:
            raise AgentDispatchConflict("job result is too large")
        public_error = "remote worker reported failure" if status == "failed" else None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now = self._now().isoformat()
            row = await (
                await db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,))
            ).fetchone()
            if row is None or not self._lease_matches(
                row,
                agent_id=agent_id,
                claim_token=claim_token,
                lease_id=lease_id,
                lease_generation=lease_generation,
                now=now,
                require_unexpired=str(row["status"]) not in TERMINAL_JOB_STATUSES,
            ):
                await db.rollback()
                raise AgentDispatchConflict(
                    "job lease is stale, expired, or owned by another agent"
                )
            result_limit = 4_000_000 if row["required_skill"] == "code.build_project" else 1_000_000
            if result_json is not None and len(result_json.encode("utf-8")) > result_limit:
                raise AgentDispatchConflict("job result is too large")
            if str(row["status"]) in TERMINAL_JOB_STATUSES:
                same = False
                try:
                    recorded_result_json = (
                        json.dumps(
                            json.loads(str(row["result_json"])),
                            allow_nan=False,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        if row["result_json"] is not None
                        else None
                    )
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
                else:
                    same = str(row["status"]) == status and recorded_result_json == result_json
                await db.rollback()
                if not same:
                    raise AgentDispatchConflict("terminal job result differs from recorded result")
                return self._job_from_row(row), False
            if str(row["status"]) not in {"claimed", "running"}:
                await db.rollback()
                raise AgentDispatchConflict("job is not active")
            task = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (row["task_id"],))
            ).fetchone()
            if task is None or str(task["status"]) != "running":
                await db.rollback()
                raise AgentDispatchConflict("terminal or inactive task cannot be resurrected")
            pending_capability = await (
                await db.execute(
                    """
                    SELECT 1 FROM iphone_capability_requests
                    WHERE requesting_job_id=? AND lease_generation=?
                      AND status NOT IN ('completed','denied','failed','cancelled','expired')
                    LIMIT 1
                    """,
                    (job_id, int(row["lease_generation"])),
                )
            ).fetchone()
            if pending_capability is not None:
                await db.rollback()
                raise AgentDispatchConflict(
                    "job cannot finish while an iPhone capability request is pending"
                )
            try:
                await AgentJobStateMachine.transition_locked(
                    db,
                    job_id=job_id,
                    current=str(row["status"]),
                    target=status,
                    now=now,
                    updates={
                        "result_json": result_json,
                        "error": public_error,
                        "completed_at": now,
                    },
                    extra_where=" AND lease_generation=? AND lease_token_hash=?",
                    where_values=(int(row["lease_generation"]), row["lease_token_hash"]),
                )
                await TaskStateMachine.transition_locked(
                    db,
                    task_id=str(row["task_id"]),
                    current="running",
                    target=status,
                    now=now,
                    error=public_error,
                )
            except DistributedStateConflict as exc:
                await db.rollback()
                raise AgentDispatchConflict(str(exc)) from exc
            await append_audit_event(
                db,
                f"agent.job.{status}",
                {
                    "job_id": job_id,
                    "agent_id": agent_id,
                    "lease_generation": int(row["lease_generation"]),
                },
                actor_type="agent",
                actor_id=agent_id,
                task_id=str(row["task_id"]),
                trace_id=str(row["task_id"]),
                created_at=now,
            )
            await self.outbox.enqueue_locked(
                db,
                aggregate_type="agent_job",
                aggregate_id=job_id,
                topic="tasks.status",
                event_type="acked" if status == "completed" else "failed",
                payload={
                    "job_id": job_id,
                    "agent_id": agent_id,
                    "status": status,
                    "lease_generation": int(row["lease_generation"]),
                },
                task_id=str(row["task_id"]),
                agent_id=agent_id,
                message_id=job_id,
                dedupe_key=f"agent-job:{job_id}:{status}:{row['lease_generation']}",
                created_at=now,
            )
            await db.commit()
        await self._drain_outbox()
        completed_record = await self.get_job(job_id)
        if completed_record is None:
            raise RuntimeError("completed job disappeared")
        return completed_record, True

    async def list_jobs(self, agent_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM agent_jobs
                    WHERE claimed_by=? OR last_agent_id=?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (agent_id, agent_id, max(1, min(limit, 500))),
                )
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,))
            ).fetchone()
        return self._job_from_row(row) if row is not None else None

    @staticmethod
    def _job_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        raw_result = row["result_json"]
        return {
            "id": str(row["id"]),
            "task_id": str(row["task_id"]),
            "required_skill": str(row["required_skill"]),
            "payload": json.loads(str(row["payload_json"])),
            "status": str(row["status"]),
            "claimed_by": str(row["claimed_by"]) if row["claimed_by"] else None,
            "result": json.loads(str(raw_result)) if raw_result else None,
            "error": str(row["error"]) if row["error"] else None,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "claimed_at": str(row["claimed_at"]) if row["claimed_at"] else None,
            "heartbeat_at": str(row["heartbeat_at"]) if row["heartbeat_at"] else None,
            "completed_at": str(row["completed_at"]) if row["completed_at"] else None,
            "lease_id": str(row["lease_id"]) if row["lease_id"] else None,
            "lease_expires_at": (str(row["lease_expires_at"]) if row["lease_expires_at"] else None),
            "lease_generation": int(row["lease_generation"]),
            "attempt_count": int(row["attempt_count"]),
            "max_attempts": int(row["max_attempts"]),
            "last_agent_id": str(row["last_agent_id"]) if row["last_agent_id"] else None,
            "last_failure_reason": (
                str(row["last_failure_reason"]) if row["last_failure_reason"] else None
            ),
        }
