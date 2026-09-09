from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.services.agent_lease import lease_matches
from app.services.approval_binding import ApprovalBindingError, canonical_action_digest
from app.services.audit_log import append_audit_event
from app.services.iphone_capability_binding import (
    CapabilityRequestBindingError,
    canonical_capability_request_fingerprint,
)
from app.services.maintenance_lease import MaintenanceLeaseGuard, MaintenanceLeaseService
from app.services.message_board import MessageBoard
from app.services.outbox import OutboxService
from app.services.permission_policy import PermissionPolicy, PermissionPolicyError

TERMINAL_CAPABILITY_STATUSES = frozenset({"completed", "denied", "failed", "cancelled", "expired"})
_EXPIRY_BATCH_SIZE = 25
_MAX_EXPIRATIONS_PER_INVOCATION = 250


class IPhoneCapabilityConflict(RuntimeError):
    pass


class IPhoneCapabilityService:
    """Permission-gated, one-use transport between leased workers and paired phones."""

    def __init__(
        self,
        db_path: Path,
        board: MessageBoard,
        policy: PermissionPolicy,
        *,
        grant_ttl_seconds: int = 90,
        maintenance_leases: MaintenanceLeaseService | None = None,
        owner_instance_id: str | None = None,
        outbox_instance_id: str | None = None,
        outbox_publication_lease_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self.policy = policy
        self.grant_ttl_seconds = grant_ttl_seconds
        self.outbox = OutboxService(
            db_path,
            board,
            instance_id=outbox_instance_id,
            publication_lease_seconds=outbox_publication_lease_seconds,
        )
        self.maintenance_leases = maintenance_leases
        self.owner_instance_id = owner_instance_id
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise RuntimeError("iPhone capability clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _grant_hash(token: str) -> str:
        return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"

    async def _drain_outbox(self) -> None:
        try:
            await self.outbox.drain()
        except (OSError, RuntimeError, TypeError, ValueError, aiosqlite.Error):
            return

    async def create_request(
        self,
        *,
        agent_id: str,
        job_id: str,
        claim_token: str,
        lease_id: str,
        lease_generation: int,
        capability_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            rule = self.policy.evaluate_capability(capability_name)
        except PermissionPolicyError as exc:
            raise IPhoneCapabilityConflict("capability is not authorized by policy") from exc
        if rule.decision != "ask" or rule.approval_ttl_seconds is None:
            raise IPhoneCapabilityConflict(
                "sensitive iPhone capabilities require an explicit approval rule"
            )
        try:
            arguments_json = json.dumps(
                arguments,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            request_fingerprint = canonical_capability_request_fingerprint(
                job_id=job_id,
                lease_generation=lease_generation,
                capability_name=capability_name,
                arguments=arguments,
            )
            request_id = f"iphreq_{uuid4().hex}"
            approval_id = f"icapr_{uuid4().hex}"
            action_digest = canonical_action_digest(
                tool_call_id=request_id,
                tool_name=capability_name,
                arguments=arguments,
            )
        except (ApprovalBindingError, CapabilityRequestBindingError, TypeError, ValueError) as exc:
            raise IPhoneCapabilityConflict("capability arguments are not canonical JSON") from exc
        if len(arguments_json.encode("utf-8")) > 64_000:
            raise IPhoneCapabilityConflict("capability arguments are too large")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            created = self._now()
            now = created.isoformat()
            expires_at = (created + timedelta(seconds=rule.approval_ttl_seconds)).isoformat()
            job = await self._require_active_lease_locked(
                db,
                agent_id=agent_id,
                job_id=job_id,
                claim_token=claim_token,
                lease_id=lease_id,
                lease_generation=lease_generation,
                now=now,
            )
            existing = await (
                await db.execute(
                    """
                    SELECT * FROM iphone_capability_requests
                    WHERE request_fingerprint=?
                      AND status NOT IN ('completed','denied','failed','cancelled','expired')
                    ORDER BY created_at ASC,id ASC LIMIT 1
                    """,
                    (request_fingerprint,),
                )
            ).fetchone()
            if existing is not None:
                await db.rollback()
                return self._public_request(existing)
            device_id = await self._select_device_locked(db, str(job["task_source"]))
            audit_event = await append_audit_event(
                db,
                "iphone.capability.requested",
                {
                    "request_id": request_id,
                    "approval_id": approval_id,
                    "capability_name": capability_name,
                    "requesting_agent_id": agent_id,
                    "requesting_job_id": job_id,
                    "action_digest": action_digest,
                    "policy": rule.approval_context(),
                    "arguments_redacted": True,
                },
                actor_type="agent",
                actor_id=agent_id,
                task_id=str(job["task_id"]),
                trace_id=str(job["task_id"]),
                created_at=now,
            )
            await db.execute(
                """
                INSERT INTO iphone_capability_requests(
                    id,task_id,requesting_agent_id,requesting_job_id,lease_generation,
                    device_id,capability_name,arguments_json,request_fingerprint,
                    action_digest,status,
                    approval_id,request_audit_id,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    request_id,
                    str(job["task_id"]),
                    agent_id,
                    job_id,
                    lease_generation,
                    device_id,
                    capability_name,
                    arguments_json,
                    request_fingerprint,
                    action_digest,
                    "waiting_approval",
                    approval_id,
                    int(audit_event["id"]),
                    now,
                    expires_at,
                ),
            )
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="iphone_capability_request",
                aggregate_id=request_id,
                topic="iphone.capabilities",
                event_type="capability_requested",
                payload={
                    "request_id": request_id,
                    "capability_name": capability_name,
                    "device_id": device_id,
                    "status": "waiting_approval",
                },
                task_id=str(job["task_id"]),
                agent_id=agent_id,
                message_id=request_id,
                dedupe_key=f"iphone-capability:{request_id}:requested",
                created_at=now,
            )
            await db.commit()
        await self._drain_outbox()
        request = await self.get_request_for_device(request_id, device_id, include_arguments=False)
        if request is None:
            raise RuntimeError("capability request disappeared")
        return request

    async def list_for_device(self, device_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        await self.expire_requests()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = list(
                await (
                    await db.execute(
                        """
                        SELECT * FROM iphone_capability_requests
                        WHERE device_id=? ORDER BY created_at DESC LIMIT ?
                        """,
                        (device_id, max(1, min(limit, 200))),
                    )
                ).fetchall()
            )
        return [self._public_request(row) for row in rows]

    async def pending_delivery_previews(
        self,
        device_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return safe reconnect hints without replaying a consumed native effect."""

        now = self._now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = list(
                await (
                    await db.execute(
                        """
                        SELECT id,capability_name,expires_at
                        FROM iphone_capability_requests
                        WHERE device_id=?
                          AND status IN ('waiting_approval','approved')
                          AND expires_at>?
                        ORDER BY created_at ASC,id ASC LIMIT ?
                        """,
                        (device_id, now, max(1, min(limit, 200))),
                    )
                ).fetchall()
            )
        return [
            {
                "request_id": str(row["id"]),
                "capability_name": str(row["capability_name"]),
                "expires_at": str(row["expires_at"]),
                "preview": {"arguments_redacted": True},
            }
            for row in rows
        ]

    async def get_request_for_device(
        self, request_id: str, device_id: str, *, include_arguments: bool = True
    ) -> dict[str, Any] | None:
        await self.expire_requests()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM iphone_capability_requests WHERE id=? AND device_id=?",
                    (request_id, device_id),
                )
            ).fetchone()
        if row is None:
            return None
        return self._device_request(row) if include_arguments else self._public_request(row)

    async def device_for_request(self, request_id: str) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT device_id FROM iphone_capability_requests WHERE id=?",
                    (request_id,),
                )
            ).fetchone()
        return str(row[0]) if row else None

    async def authorize(
        self,
        request_id: str,
        device_id: str,
        *,
        decision: str,
        user_note: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "deny"}:
            raise IPhoneCapabilityConflict("invalid capability decision")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            issued = self._now()
            now = issued.isoformat()
            row = await (
                await db.execute(
                    "SELECT * FROM iphone_capability_requests WHERE id=? AND device_id=?",
                    (request_id, device_id),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise IPhoneCapabilityConflict("capability request not found for this device")
            request_status = str(row["status"])
            allowed_statuses = (
                {"waiting_approval", "approved"} if decision == "approve" else {"waiting_approval"}
            )
            if request_status not in allowed_statuses:
                await db.rollback()
                raise IPhoneCapabilityConflict("capability request was already decided")
            if str(row["expires_at"]) <= now:
                await self._expire_locked(db, row, now)
                await db.commit()
                raise IPhoneCapabilityConflict("capability request expired")
            if not await self._request_job_is_active_locked(db, row, now):
                await self._cancel_locked(
                    db, row, now, "requesting worker lease is no longer active"
                )
                await db.commit()
                raise IPhoneCapabilityConflict("requesting worker lease is no longer active")
            if decision == "deny":
                await db.execute(
                    """
                    UPDATE iphone_capability_requests
                    SET status='denied',completed_at=?
                    WHERE id=? AND status='waiting_approval'
                    """,
                    (now, request_id),
                )
                await self._record_decision_locked(
                    db, row, now, "denied", device_id, user_note=user_note
                )
                await db.commit()
                await self._drain_outbox()
                denied = dict(row)
                denied.update(status="denied", completed_at=now)
                return self._device_request(denied)

            request_expiry = datetime.fromisoformat(str(row["expires_at"]))
            grant_expiry = min(
                request_expiry, issued + timedelta(seconds=self.grant_ttl_seconds)
            ).isoformat()
            raw_grant = f"grt_{secrets.token_urlsafe(48)}"
            grant_hash = self._grant_hash(raw_grant)
            if request_status == "approved":
                grant = await (
                    await db.execute(
                        """
                        SELECT id,consumed_at FROM iphone_capability_grants
                        WHERE request_id=? AND device_id=?
                        """,
                        (request_id, device_id),
                    )
                ).fetchone()
                if grant is None or grant["consumed_at"] is not None:
                    await db.rollback()
                    raise IPhoneCapabilityConflict("capability grant cannot be rotated")
                cursor = await db.execute(
                    """
                    UPDATE iphone_capability_grants
                    SET token_hash=?,issued_at=?,expires_at=?
                    WHERE id=? AND consumed_at IS NULL
                    """,
                    (grant_hash, now, grant_expiry, str(grant["id"])),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    raise IPhoneCapabilityConflict("capability grant changed during rotation")
                rotation_id = f"icgr_{uuid4().hex}"
                await append_audit_event(
                    db,
                    "iphone.capability.grant_rotated",
                    {"request_id": request_id, "rotation_id": rotation_id},
                    actor_type="device",
                    actor_id=device_id,
                    task_id=str(row["task_id"]),
                    trace_id=str(row["task_id"]),
                    created_at=now,
                )
                await OutboxService.enqueue_locked(
                    db,
                    aggregate_type="iphone_capability_request",
                    aggregate_id=request_id,
                    topic="iphone.capabilities",
                    event_type="capability_authorized",
                    payload={"request_id": request_id, "status": "approved"},
                    task_id=str(row["task_id"]),
                    agent_id=str(row["requesting_agent_id"]),
                    message_id=request_id,
                    dedupe_key=f"iphone-capability:{request_id}:grant-rotated:{rotation_id}",
                    created_at=now,
                )
            else:
                grant_row_id = f"icg_{uuid4().hex}"
                await db.execute(
                    """
                    INSERT INTO iphone_capability_grants(
                        id,request_id,device_id,capability_name,approval_id,token_hash,
                        action_digest,issued_at,expires_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        grant_row_id,
                        request_id,
                        device_id,
                        str(row["capability_name"]),
                        str(row["approval_id"]),
                        grant_hash,
                        str(row["action_digest"]),
                        now,
                        grant_expiry,
                    ),
                )
                cursor = await db.execute(
                    """
                    UPDATE iphone_capability_requests SET status='approved'
                    WHERE id=? AND status='waiting_approval'
                    """,
                    (request_id,),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    raise IPhoneCapabilityConflict(
                        "capability request changed during authorization"
                    )
                await self._record_decision_locked(
                    db, row, now, "approved", device_id, user_note=user_note
                )
            await db.commit()
        await self._drain_outbox()
        approved = dict(row)
        approved["status"] = "approved"
        detail = self._device_request(approved)
        detail["grant"] = {
            "schema_version": "0.9",
            "grant_id": raw_grant,
            "request_id": request_id,
            "task_id": str(row["task_id"]),
            "agent_id": str(row["requesting_agent_id"]),
            "target_device_id": device_id,
            "approval_id": str(row["approval_id"]),
            "audit_id": int(row["request_audit_id"]),
            "capability": str(row["capability_name"]),
            "action_digest": str(row["action_digest"]),
            "issued_at": now,
            "expires_at": grant_expiry,
            "use": "once",
        }
        return detail

    async def consume(
        self,
        request_id: str,
        device_id: str,
        *,
        grant_id: str,
        action_digest: str,
    ) -> dict[str, Any]:
        token_hash = self._grant_hash(grant_id)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now = self._now().isoformat()
            row = await (
                await db.execute(
                    """
                    SELECT r.*,g.id AS grant_row_id,g.token_hash,g.action_digest AS grant_digest,
                           g.device_id AS grant_device_id,g.capability_name AS grant_capability,
                           g.approval_id AS grant_approval_id,g.expires_at AS grant_expires_at,
                           g.consumed_at
                    FROM iphone_capability_requests r
                    JOIN iphone_capability_grants g ON g.request_id=r.id
                    WHERE r.id=? AND g.token_hash=?
                    """,
                    (request_id, token_hash),
                )
            ).fetchone()
            if row is None or not self._grant_binding_matches(row, device_id, action_digest):
                await db.rollback()
                raise IPhoneCapabilityConflict("capability grant binding is invalid")
            if row["consumed_at"] is not None or str(row["status"]) != "approved":
                await db.rollback()
                raise IPhoneCapabilityConflict("capability grant was already consumed")
            if str(row["grant_expires_at"]) <= now:
                await self._expire_locked(db, row, now)
                await db.commit()
                raise IPhoneCapabilityConflict("capability grant expired")
            if not await self._request_job_is_active_locked(db, row, now):
                await self._cancel_locked(
                    db, row, now, "requesting worker lease is no longer active"
                )
                await db.commit()
                raise IPhoneCapabilityConflict("requesting worker lease is no longer active")
            grant_cursor = await db.execute(
                """
                UPDATE iphone_capability_grants SET consumed_at=?
                WHERE id=? AND consumed_at IS NULL AND expires_at>?
                """,
                (now, row["grant_row_id"], now),
            )
            request_cursor = await db.execute(
                """
                UPDATE iphone_capability_requests SET status='consumed',delivered_at=?
                WHERE id=? AND status='approved'
                """,
                (now, request_id),
            )
            if grant_cursor.rowcount != 1 or request_cursor.rowcount != 1:
                await db.rollback()
                raise IPhoneCapabilityConflict("capability grant lost a concurrent transition")
            await append_audit_event(
                db,
                "iphone.capability.grant_consumed",
                {"request_id": request_id, "action_digest": action_digest},
                actor_type="device",
                actor_id=device_id,
                task_id=str(row["task_id"]),
                trace_id=str(row["task_id"]),
                created_at=now,
            )
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="iphone_capability_request",
                aggregate_id=request_id,
                topic="iphone.capabilities",
                event_type="capability_consumed",
                payload={"request_id": request_id, "status": "consumed"},
                task_id=str(row["task_id"]),
                agent_id=str(row["requesting_agent_id"]),
                message_id=request_id,
                dedupe_key=f"iphone-capability:{request_id}:consumed",
                created_at=now,
            )
            await db.commit()
        await self._drain_outbox()
        return {
            "status": "consumed",
            "request_id": request_id,
            "grant_id": grant_id,
            "action_digest": action_digest,
            "consumed_at": now,
        }

    async def submit_result(
        self,
        request_id: str,
        device_id: str,
        *,
        grant_id: str,
        action_digest: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result_json = json.dumps(
                result,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise IPhoneCapabilityConflict("capability result is not canonical JSON") from exc
        if len(result_json.encode("utf-8")) > 1_000_000:
            raise IPhoneCapabilityConflict("capability result is too large")
        token_hash = self._grant_hash(grant_id)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now = self._now().isoformat()
            row = await (
                await db.execute(
                    """
                    SELECT r.*,g.token_hash,g.action_digest AS grant_digest,
                           g.device_id AS grant_device_id,g.capability_name AS grant_capability,
                           g.approval_id AS grant_approval_id,g.consumed_at,
                           x.status AS stored_result_status,x.result_json AS stored_result_json
                    FROM iphone_capability_requests r
                    JOIN iphone_capability_grants g ON g.request_id=r.id
                    LEFT JOIN iphone_capability_results x ON x.request_id=r.id
                    WHERE r.id=? AND g.token_hash=?
                    """,
                    (request_id, token_hash),
                )
            ).fetchone()
            if row is None or not self._grant_binding_matches(row, device_id, action_digest):
                await db.rollback()
                raise IPhoneCapabilityConflict("capability grant binding is invalid")
            if row["consumed_at"] is None:
                await db.rollback()
                raise IPhoneCapabilityConflict("capability grant was not consumed")
            if str(result.get("name")) != str(row["capability_name"]):
                await db.rollback()
                raise IPhoneCapabilityConflict("capability result name does not match its grant")
            result_status = str(result.get("status"))
            if result_status not in {"completed", "cancelled", "denied", "failed"}:
                await db.rollback()
                raise IPhoneCapabilityConflict("capability result status is invalid")
            if str(row["status"]) in TERMINAL_CAPABILITY_STATUSES:
                same = row["stored_result_json"] is not None and hmac.compare_digest(
                    str(row["stored_result_json"]), result_json
                )
                await db.rollback()
                if not same:
                    raise IPhoneCapabilityConflict("terminal capability result differs")
                return {
                    "status": "duplicate",
                    "request_id": request_id,
                    "grant_id": grant_id,
                }
            if str(row["status"]) != "consumed":
                await db.rollback()
                raise IPhoneCapabilityConflict("capability request is not executing")
            if not await self._request_job_is_active_locked(db, row, now):
                await self._cancel_locked(
                    db, row, now, "requesting worker lease is no longer active"
                )
                await db.commit()
                raise IPhoneCapabilityConflict("requesting worker lease is no longer active")
            if str(row["expires_at"]) <= now:
                await self._expire_locked(db, row, now)
                await db.commit()
                raise IPhoneCapabilityConflict("capability request expired before result delivery")
            await db.execute(
                """
                INSERT INTO iphone_capability_results(request_id,status,result_json,created_at)
                VALUES(?,?,?,?)
                """,
                (request_id, result_status, result_json, now),
            )
            cursor = await db.execute(
                """
                UPDATE iphone_capability_requests SET status=?,completed_at=?
                WHERE id=? AND status='consumed'
                """,
                (result_status, now, request_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise IPhoneCapabilityConflict("capability request changed during result")
            await append_audit_event(
                db,
                "iphone.capability.result",
                {"request_id": request_id, "status": result_status, "result_redacted": True},
                actor_type="device",
                actor_id=device_id,
                task_id=str(row["task_id"]),
                trace_id=str(row["task_id"]),
                created_at=now,
            )
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="iphone_capability_request",
                aggregate_id=request_id,
                topic="agent.job.capability.result",
                event_type="capability_result",
                payload={"request_id": request_id, "status": result_status},
                task_id=str(row["task_id"]),
                agent_id=str(row["requesting_agent_id"]),
                message_id=request_id,
                dedupe_key=f"iphone-capability:{request_id}:result",
                created_at=now,
            )
            await db.commit()
        await self._drain_outbox()
        return {
            "status": "accepted",
            "request_id": request_id,
            "grant_id": grant_id,
        }

    async def poll_for_worker(
        self,
        request_id: str,
        *,
        agent_id: str,
        job_id: str,
        claim_token: str,
        lease_id: str,
        lease_generation: int,
    ) -> dict[str, Any]:
        await self.expire_requests()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now = self._now().isoformat()
            await self._require_active_lease_locked(
                db,
                agent_id=agent_id,
                job_id=job_id,
                claim_token=claim_token,
                lease_id=lease_id,
                lease_generation=lease_generation,
                now=now,
            )
            row = await (
                await db.execute(
                    """
                    SELECT r.*,x.result_json
                    FROM iphone_capability_requests r
                    LEFT JOIN iphone_capability_results x ON x.request_id=r.id
                    WHERE r.id=? AND r.requesting_job_id=? AND r.requesting_agent_id=?
                      AND r.lease_generation=?
                    """,
                    (request_id, job_id, agent_id, lease_generation),
                )
            ).fetchone()
            await db.rollback()
        if row is None:
            raise IPhoneCapabilityConflict("capability request not found for this lease")
        return {
            "request_id": request_id,
            "capability_name": str(row["capability_name"]),
            "status": str(row["status"]),
            "result": json.loads(str(row["result_json"])) if row["result_json"] else None,
            "created_at": str(row["created_at"]),
            "expires_at": str(row["expires_at"]),
            "completed_at": str(row["completed_at"]) if row["completed_at"] else None,
        }

    async def expire_requests(
        self,
        *,
        maintenance_generation: int | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> int:
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
            raise RuntimeError("capability expiry received mismatched maintenance fencing")
        if self.maintenance_leases is not None and effective_generation is None:
            if self.owner_instance_id is None:
                raise RuntimeError("capability expiry maintenance owner is not configured")
            lease = await self.maintenance_leases.acquire(
                "capability-expirer", self.owner_instance_id
            )
            if lease is None:
                return 0
            effective_generation = lease.generation
        now = self._now().isoformat()
        expired_count = 0
        while expired_count < _MAX_EXPIRATIONS_PER_INVOCATION:
            if maintenance_guard is not None:
                await maintenance_guard.renew_now()
            batch_limit = min(
                _EXPIRY_BATCH_SIZE,
                _MAX_EXPIRATIONS_PER_INVOCATION - expired_count,
            )
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                try:
                    if maintenance_guard is not None:
                        await maintenance_guard.require_current_locked(db)
                    elif self.maintenance_leases is not None:
                        if self.owner_instance_id is None or effective_generation is None:
                            raise RuntimeError(
                                "capability expiry requires a current maintenance lease"
                            )
                        await self.maintenance_leases.require_current_locked(
                            db,
                            "capability-expirer",
                            self.owner_instance_id,
                            effective_generation,
                        )
                    batch_started = asyncio.get_running_loop().time()
                    rows = list(
                        await (
                            await db.execute(
                                """
                                SELECT r.* FROM iphone_capability_requests AS r
                                LEFT JOIN iphone_capability_grants AS g ON g.request_id=r.id
                                WHERE (r.status='waiting_approval' AND r.expires_at<=?)
                                   OR (r.status='approved' AND
                                       COALESCE(g.expires_at,r.expires_at)<=?)
                                   OR (r.status='consumed' AND r.expires_at<=?)
                                ORDER BY r.expires_at ASC,r.id ASC
                                LIMIT ?
                                """,
                                (now, now, now, batch_limit),
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
                        await self._expire_locked(db, row, now)
                    if maintenance_guard is not None:
                        await maintenance_guard.require_current_locked(db)
                    elif self.maintenance_leases is not None:
                        if self.owner_instance_id is None or effective_generation is None:
                            raise RuntimeError(
                                "capability expiry lost its maintenance fencing context"
                            )
                        await self.maintenance_leases.require_current_locked(
                            db,
                            "capability-expirer",
                            self.owner_instance_id,
                            effective_generation,
                        )
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
            expired_count += batch_examined
            if not rows or (batch_examined == len(rows) and len(rows) < batch_limit):
                break
            # Release SQLite's writer lock between batches so the runner can
            # renew and other authoritative work can proceed.
            await asyncio.sleep(0)
        if expired_count:
            await self.outbox.drain(maintenance_guard=maintenance_guard)
        return expired_count

    async def pending_count(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT COUNT(*) FROM iphone_capability_requests
                    WHERE status IN ('waiting_approval','approved','consumed')
                    """
                )
            ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def _require_active_lease_locked(
        db: aiosqlite.Connection,
        *,
        agent_id: str,
        job_id: str,
        claim_token: str,
        lease_id: str,
        lease_generation: int,
        now: str,
    ) -> aiosqlite.Row:
        row = await (
            await db.execute(
                """
                SELECT j.*,t.status AS task_status,t.source AS task_source
                FROM agent_jobs j JOIN tasks t ON t.id=j.task_id
                WHERE j.id=?
                """,
                (job_id,),
            )
        ).fetchone()
        if (
            row is None
            or str(row["status"]) not in {"claimed", "running"}
            or str(row["task_status"]) != "running"
            or not lease_matches(
                dict(row),
                agent_id=agent_id,
                token=claim_token,
                lease_id=lease_id,
                lease_generation=lease_generation,
                now=now,
                require_unexpired=True,
            )
        ):
            raise IPhoneCapabilityConflict("job lease is stale, expired, or inactive")
        return row

    @staticmethod
    async def _request_job_is_active_locked(
        db: aiosqlite.Connection, request: aiosqlite.Row, now: str
    ) -> bool:
        row = await (
            await db.execute(
                """
                SELECT j.status,j.claimed_by,j.lease_generation,j.lease_expires_at,t.status
                FROM agent_jobs j JOIN tasks t ON t.id=j.task_id WHERE j.id=?
                """,
                (request["requesting_job_id"],),
            )
        ).fetchone()
        return bool(
            row is not None
            and str(row[0]) in {"claimed", "running"}
            and str(row[1]) == str(request["requesting_agent_id"])
            and int(row[2]) == int(request["lease_generation"])
            and isinstance(row[3], str)
            and str(row[3]) > now
            and str(row[4]) == "running"
        )

    @staticmethod
    async def _select_device_locked(db: aiosqlite.Connection, task_source: str) -> str:
        exact = await (
            await db.execute("SELECT id FROM devices WHERE id=?", (task_source,))
        ).fetchone()
        if exact is not None:
            return str(exact[0])
        candidates = list(
            await (
                await db.execute(
                    """
                    SELECT id FROM devices
                    ORDER BY COALESCE(last_seen_at,created_at) DESC,id ASC LIMIT 2
                    """
                )
            ).fetchall()
        )
        if len(candidates) != 1:
            raise IPhoneCapabilityConflict("task has no unambiguous paired iPhone")
        return str(candidates[0][0])

    @staticmethod
    def _grant_binding_matches(row: aiosqlite.Row, device_id: str, action_digest: str) -> bool:
        return bool(
            str(row["device_id"]) == device_id
            and str(row["grant_device_id"]) == device_id
            and str(row["capability_name"]) == str(row["grant_capability"])
            and str(row["approval_id"]) == str(row["grant_approval_id"])
            and hmac.compare_digest(str(row["action_digest"]), action_digest)
            and hmac.compare_digest(str(row["grant_digest"]), action_digest)
        )

    async def _record_decision_locked(
        self,
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
        now: str,
        outcome: str,
        device_id: str,
        *,
        user_note: str | None,
    ) -> None:
        request_id = str(row["id"])
        await append_audit_event(
            db,
            "iphone.capability.approval_decided",
            {
                "request_id": request_id,
                "approval_id": str(row["approval_id"]),
                "decision": outcome,
                "user_note_present": bool(user_note),
            },
            actor_type="device",
            actor_id=device_id,
            task_id=str(row["task_id"]),
            trace_id=str(row["task_id"]),
            created_at=now,
        )
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="iphone_capability_request",
            aggregate_id=request_id,
            topic="iphone.capabilities",
            event_type="capability_authorized" if outcome == "approved" else "capability_result",
            payload={"request_id": request_id, "status": outcome},
            task_id=str(row["task_id"]),
            agent_id=str(row["requesting_agent_id"]),
            message_id=request_id,
            dedupe_key=f"iphone-capability:{request_id}:{outcome}",
            created_at=now,
        )

    async def _expire_locked(self, db: aiosqlite.Connection, row: aiosqlite.Row, now: str) -> None:
        request_id = str(row["id"])
        await db.execute(
            """
            UPDATE iphone_capability_requests SET status='expired',completed_at=?
            WHERE id=? AND status IN ('waiting_approval','approved','consumed')
            """,
            (now, request_id),
        )
        await db.execute(
            "UPDATE iphone_capability_grants SET expires_at=? WHERE request_id=?",
            (now, request_id),
        )
        await append_audit_event(
            db,
            "iphone.capability.expired",
            {"request_id": request_id},
            actor_type="control-plane",
            actor_id="capability-gateway",
            task_id=str(row["task_id"]),
            trace_id=str(row["task_id"]),
            created_at=now,
        )
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="iphone_capability_request",
            aggregate_id=request_id,
            topic="agent.job.capability.result",
            event_type="capability_result",
            payload={"request_id": request_id, "status": "expired"},
            task_id=str(row["task_id"]),
            agent_id=str(row["requesting_agent_id"]),
            message_id=request_id,
            dedupe_key=f"iphone-capability:{request_id}:expired",
            created_at=now,
        )

    async def _cancel_locked(
        self, db: aiosqlite.Connection, row: aiosqlite.Row, now: str, reason: str
    ) -> None:
        request_id = str(row["id"])
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
            {"request_id": request_id, "reason": reason},
            actor_type="control-plane",
            actor_id="capability-gateway",
            task_id=str(row["task_id"]),
            trace_id=str(row["task_id"]),
            created_at=now,
        )
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="iphone_capability_request",
            aggregate_id=request_id,
            topic="agent.job.capability.result",
            event_type="capability_result",
            payload={"request_id": request_id, "status": "cancelled"},
            task_id=str(row["task_id"]),
            agent_id=str(row["requesting_agent_id"]),
            message_id=request_id,
            dedupe_key=f"iphone-capability:{request_id}:cancelled",
            created_at=now,
        )

    @classmethod
    def _public_request(cls, row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "0.9",
            "request_id": str(row["id"]),
            "task_id": str(row["task_id"]),
            "agent_id": str(row["requesting_agent_id"]),
            "target_device_id": str(row["device_id"]),
            "capability": str(row["capability_name"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "expires_at": str(row["expires_at"]),
        }

    @classmethod
    def _device_request(cls, row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            **cls._public_request(row),
            "arguments": json.loads(str(row["arguments_json"])),
            "action_digest": str(row["action_digest"]),
            "grant": None,
        }
