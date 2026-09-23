"""Revision-bound device consent and transport for the opt-in native compiler."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import aiosqlite
from pydantic import field_validator, model_validator

from app.models import TaskCreate, TaskRecord
from app.services.audit_log import append_audit_event
from app.services.distributed_state import AgentJobStateMachine, TaskStateMachine
from app.services.project_contracts import (
    Digest,
    Identifier,
    ProjectFile,
    ProjectResult,
    StrictModel,
    project_digest,
    validate_files,
)
from app.services.swift_contracts import valid_swift_receipt, validate_swift_payload

if TYPE_CHECKING:
    from app.services.agent_dispatcher import AgentDispatcher
    from app.services.outbox import OutboxService

SWIFT_PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS swift_project_validations (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goal_runs(id),
 project_id TEXT NOT NULL REFERENCES coding_projects(id),
 revision_id TEXT NOT NULL REFERENCES project_revisions(id),
 sha256 TEXT NOT NULL, source_sha256 TEXT NOT NULL, conversation_revision INTEGER NOT NULL,
 requester_id TEXT NOT NULL, agent_id TEXT NOT NULL REFERENCES agents(id),
 operation TEXT NOT NULL, target_json TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
 job_id TEXT UNIQUE REFERENCES agent_jobs(id), status TEXT NOT NULL,
 expires_at TEXT NOT NULL, idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(requester_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_swift_project_revision ON swift_project_validations(revision_id,created_at);
"""
TERMINAL_GOALS = {"completed", "failed", "cancelled", "budget_exhausted"}


class SwiftProjectConflict(ValueError):
    """Consent, source or execution identity no longer matches."""


class SwiftProjectValidationRequest(StrictModel):
    revision_id: Identifier
    sha256: Digest
    agent_id: Identifier
    operation: Literal["build", "test"] = "test"
    target: dict[str, str]
    execution_consent: Literal[True]
    idempotency_key: Identifier

    @field_validator("execution_consent", mode="before")
    @classmethod
    def require_explicit_consent(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("explicit execution consent must be true")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> SwiftProjectValidationRequest:
        if "source_sha256" in self.target:
            raise ValueError("source digest is computed from the accepted revision")
        validate_swift_payload({**self.target, "source_sha256": "0" * 64})
        return self


def snapshot_source_digest(files: list[ProjectFile]) -> str:
    """Same preorder as Swift's os.walk: parent files, then sorted subtrees."""
    validate_files(files)
    if not files:
        raise SwiftProjectConflict("empty project cannot be compiled")
    for file in files:
        if any(p.casefold() in {".build", ".swarmer-swift-runs"} for p in file.path.split("/")):
            raise SwiftProjectConflict("project contains reserved Swift runtime paths")
    by_parent: dict[str, list[ProjectFile]] = {}
    directories: dict[str, set[str]] = {}
    for file in files:
        parent = str(PurePosixPath(file.path).parent)
        by_parent.setdefault(parent, []).append(file)
        parts = file.path.split("/")[:-1]
        for i, part in enumerate(parts):
            directories.setdefault("/".join(parts[:i]) or ".", set()).add(part)
    digest = hashlib.sha256()

    def visit(parent: str) -> None:
        for file in sorted(by_parent.get(parent, []), key=lambda f: f.path):
            path, content = file.path.encode(), file.content.encode()
            digest.update(len(path).to_bytes(4, "big") + path)
            digest.update(len(content).to_bytes(8, "big") + content)
        for directory in sorted(directories.get(parent, set())):
            visit(directory if parent == "." else parent + "/" + directory)

    visit(".")
    return digest.hexdigest()


def project_reference(row: dict[str, Any]) -> dict[str, str]:
    return {
        "validation_id": row["id"],
        "project_id": row["project_id"],
        "revision_id": row["revision_id"],
        "sha256": row["sha256"],
    }


async def require_project_grant_locked(
    db: aiosqlite.Connection,
    payload: dict[str, Any],
    *,
    task_id: str,
    skill: str,
    job_id: str | None = None,
    agent_id: str | None = None,
    active: bool = True,
) -> dict[str, Any] | None:
    reference = payload.get("project_revision")
    if reference is None:
        return None
    if not isinstance(reference, dict):
        raise SwiftProjectConflict("invalid native execution grant")
    cursor = await db.execute(
        "SELECT * FROM swift_project_validations WHERE id=?", (reference.get("validation_id"),)
    )
    row = await cursor.fetchone()
    if row is None:
        raise SwiftProjectConflict("native execution consent missing")
    grant = dict(zip([c[0] for c in cursor.description], row, strict=True))
    expected = {
        **json.loads(grant["target_json"]),
        "source_sha256": grant["source_sha256"],
        "project_revision": project_reference(grant),
    }
    if (
        payload != expected
        or grant["task_id"] != task_id
        or skill != "code.swift." + grant["operation"]
        or grant["status"] != "approved"
        or (job_id is not None and grant["job_id"] != job_id)
        or (agent_id is not None and grant["agent_id"] != agent_id)
    ):
        raise SwiftProjectConflict("native execution grant does not match this job")
    context = await (
        await db.execute(
            """SELECT g.status,g.conversation_revision,r.sha256,
        (SELECT id FROM project_revisions WHERE project_id=r.project_id ORDER BY revision DESC LIMIT 1),
        EXISTS(SELECT 1 FROM devices WHERE id=?)
        FROM goal_runs g JOIN goal_project_links l ON l.goal_run_id=g.id
        JOIN project_revisions r ON r.project_id=l.project_id
        WHERE g.id=? AND r.id=?""",
            (grant["requester_id"], grant["goal_id"], grant["revision_id"]),
        )
    ).fetchone()
    if (
        context is None
        or context[1] != grant["conversation_revision"]
        or context[2] != grant["sha256"]
        or context[3] != grant["revision_id"]
        or not context[4]
        or (
            active
            and (
                context[0] in TERMINAL_GOALS or grant["expires_at"] <= datetime.now(UTC).isoformat()
            )
        )
    ):
        raise SwiftProjectConflict("native execution consent is stale, expired or revoked")
    return grant


async def cancel_native_job_locked(
    db: aiosqlite.Connection, *, task_id: str, reason: str, now: str, outbox: OutboxService
) -> None:
    """Revoke execution atomically without touching the immutable project files."""
    job = await (
        await db.execute("SELECT id,status FROM agent_jobs WHERE task_id=?", (task_id,))
    ).fetchone()
    if job and job[1] in {"queued", "claimed", "running"}:
        await AgentJobStateMachine.transition_locked(
            db,
            job_id=job[0],
            current=job[1],
            target="cancelled",
            now=now,
            updates={"completed_at": now, "error": reason, "last_failure_reason": reason},
        )
    task = await (await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))).fetchone()
    if task and "cancelled" in TaskStateMachine.ALLOWED.get(task[0], frozenset()):
        await TaskStateMachine.transition_locked(
            db,
            task_id=task_id,
            current=task[0],
            target="cancelled",
            now=now,
            error=reason,
        )
    if job and job[1] in {"queued", "claimed", "running"}:
        await append_audit_event(
            db,
            "project.swift.execution_revoked",
            {"job_id": job[0], "reason": reason},
            actor_type="control-plane",
            actor_id="swift-validation",
            task_id=task_id,
            trace_id=task_id,
            created_at=now,
        )
        await outbox.enqueue_locked(
            db,
            aggregate_type="agent_job",
            aggregate_id=job[0],
            topic="tasks.status",
            event_type="cancelled",
            payload={"job_id": job[0], "status": "cancelled"},
            task_id=task_id,
            message_id=job[0],
            dedupe_key=f"agent-job:{job[0]}:swift-revoked",
            created_at=now,
        )


class SwiftProjectValidationService:
    def __init__(self, db_path: Path, dispatcher: AgentDispatcher) -> None:
        self.db_path, self.dispatcher = db_path, dispatcher

    async def request(
        self, goal_id: str, request: SwiftProjectValidationRequest, requester_id: str
    ) -> dict[str, Any]:
        from app.services.state_service import StateService

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            old = await (
                await db.execute(
                    "SELECT * FROM swift_project_validations WHERE requester_id=? AND idempotency_key=?",
                    (requester_id, request.idempotency_key),
                )
            ).fetchone()
            if old:
                grant = dict(old)
                if any(
                    grant[k] != v
                    for k, v in {
                        "goal_id": goal_id,
                        "revision_id": request.revision_id,
                        "sha256": request.sha256,
                        "agent_id": request.agent_id,
                        "operation": request.operation,
                        "target_json": json.dumps(
                            request.target, sort_keys=True, separators=(",", ":")
                        ),
                    }.items()
                ):
                    raise SwiftProjectConflict(
                        "idempotency key belongs to another native execution request"
                    )
            else:
                row = await (
                    await db.execute(
                        """SELECT r.*,g.status AS goal_status,g.conversation_revision,
                    (SELECT id FROM project_revisions WHERE project_id=r.project_id ORDER BY revision DESC LIMIT 1) AS latest_id
                    FROM project_revisions r JOIN goal_project_links l ON l.project_id=r.project_id
                    JOIN goal_runs g ON g.id=l.goal_run_id WHERE g.id=? AND r.id=?""",
                        (goal_id, request.revision_id),
                    )
                ).fetchone()
                if (
                    row is None
                    or row["sha256"] != request.sha256
                    or row["latest_id"] != request.revision_id
                    or row["goal_status"] in TERMINAL_GOALS
                ):
                    raise SwiftProjectConflict("reviewed native revision is no longer current")
                agent = await (
                    await db.execute(
                        "SELECT skills_json FROM agents WHERE id=?", (request.agent_id,)
                    )
                ).fetchone()
                if agent is None or "code.swift." + request.operation not in json.loads(agent[0]):
                    raise SwiftProjectConflict(
                        "selected worker cannot execute this Swift operation"
                    )
                snapshot = ProjectResult.model_validate_json(row["snapshot_json"])
                if project_digest(snapshot.files) != request.sha256:
                    raise SwiftProjectConflict("stored native project digest mismatch")
                digest = snapshot_source_digest(snapshot.files)
                paths = {f.path for f in snapshot.files}
                if request.target["kind"] == "swiftpm" and "Package.swift" not in paths:
                    raise SwiftProjectConflict("Package.swift missing from reviewed revision")
                if request.target["kind"] == "xcode" and not any(
                    p.startswith(request.target["project"] + "/") for p in paths
                ):
                    raise SwiftProjectConflict("reviewed Xcode project missing")
                now = datetime.now(UTC)
                child = TaskRecord.new(
                    TaskCreate(input="Compile and execute the explicitly approved Swift revision"),
                    source=requester_id,
                )
                await StateService._insert_task(db, child)
                grant = {
                    "id": "swiftval_" + uuid4().hex,
                    "goal_id": goal_id,
                    "project_id": row["project_id"],
                    "revision_id": request.revision_id,
                    "sha256": request.sha256,
                    "source_sha256": digest,
                    "conversation_revision": row["conversation_revision"],
                    "requester_id": requester_id,
                    "agent_id": request.agent_id,
                    "operation": request.operation,
                    "target_json": json.dumps(
                        request.target, sort_keys=True, separators=(",", ":")
                    ),
                    "task_id": child.id,
                    "job_id": None,
                    "status": "approved",
                    "expires_at": (now + timedelta(minutes=15)).isoformat(),
                    "idempotency_key": request.idempotency_key,
                    "created_at": now.isoformat(),
                }
                columns = ",".join(grant)
                await db.execute(
                    f"INSERT INTO swift_project_validations({columns}) VALUES({','.join('?' for _ in grant)})",
                    tuple(grant.values()),
                )  # nosec B608 - fixed local column names
                await append_audit_event(
                    db,
                    "project.swift.execution_approved",
                    {
                        "validation_id": grant["id"],
                        "revision_id": request.revision_id,
                        "sha256": request.sha256,
                        "agent_id": request.agent_id,
                        "operation": request.operation,
                        "target": request.target,
                    },
                    actor_type="device",
                    actor_id=requester_id,
                    task_id=child.id,
                    trace_id=goal_id,
                    created_at=now.isoformat(),
                )
            await db.commit()
        if grant["job_id"] is None:
            payload = {
                **request.target,
                "source_sha256": grant["source_sha256"],
                "project_revision": project_reference(grant),
            }
            await self.dispatcher.queue_job(
                grant["task_id"],
                "code.swift." + request.operation,
                payload,
                project_validation_id=grant["id"],
            )
        return await self.get(goal_id, grant["id"])

    async def get(self, goal_id: str, validation_id: str | None = None) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM swift_project_validations WHERE goal_id=? AND (? IS NULL OR id=?) ORDER BY created_at DESC LIMIT 1",
                    (goal_id, validation_id, validation_id),
                )
            ).fetchone()
            if row is None:
                raise SwiftProjectConflict("native validation not found")
            grant = dict(row)
            status = "queued"
            receipt = None
            job = await (
                await db.execute("SELECT * FROM agent_jobs WHERE id=?", (grant["job_id"],))
            ).fetchone()
            payload = {
                **json.loads(grant["target_json"]),
                "source_sha256": grant["source_sha256"],
                "project_revision": project_reference(grant),
            }
            try:
                await require_project_grant_locked(
                    db,
                    payload,
                    task_id=grant["task_id"],
                    skill="code.swift." + grant["operation"],
                    active=not job or job["status"] not in {"completed", "failed"},
                )
            except SwiftProjectConflict:
                status = "cancelled" if grant["status"] == "cancelled" else "stale"
            else:
                if job:
                    status = str(job["status"])
                    candidate = json.loads(job["result_json"]) if job["result_json"] else None
                    if status == "completed" and valid_swift_receipt(
                        job["required_skill"], candidate, payload
                    ):
                        receipt = candidate
                    if status == "completed":
                        status = "passed" if receipt is not None else "failed"
            return {
                "validation_id": grant["id"],
                "job_id": grant["job_id"],
                "status": status,
                **{
                    key: grant[key]
                    for key in (
                        "revision_id",
                        "sha256",
                        "source_sha256",
                        "conversation_revision",
                        "operation",
                        "agent_id",
                    )
                },
                "target": json.loads(grant["target_json"]),
                "receipt": receipt,
            }

    async def source(self, agent_id: str, job_id: str, proof: Any) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            row = await (
                await db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,))
            ).fetchone()
            if (
                row is None
                or row["status"] not in {"claimed", "running"}
                or not self.dispatcher._lease_matches(
                    row,
                    agent_id=agent_id,
                    claim_token=proof.claim_token,
                    lease_id=proof.lease_id,
                    lease_generation=proof.lease_generation,
                    now=datetime.now(UTC).isoformat(),
                    require_unexpired=True,
                )
            ):
                raise SwiftProjectConflict("native source requires the current authenticated lease")
            payload = json.loads(row["payload_json"])
            task = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (row["task_id"],))
            ).fetchone()
            if task is None or task[0] != "running":
                raise SwiftProjectConflict("native source requires an active task")
            grant = await require_project_grant_locked(
                db,
                payload,
                task_id=row["task_id"],
                skill=row["required_skill"],
                job_id=job_id,
                agent_id=agent_id,
            )
            if grant is None:
                raise SwiftProjectConflict("job has no approved project source")
            snapshot = await (
                await db.execute(
                    "SELECT snapshot_json FROM project_revisions WHERE id=?",
                    (grant["revision_id"],),
                )
            ).fetchone()
            if snapshot is None:
                raise SwiftProjectConflict("approved project source is unavailable")
            files = ProjectResult.model_validate_json(snapshot[0]).files
            if (
                project_digest(files) != grant["sha256"]
                or snapshot_source_digest(files) != grant["source_sha256"]
            ):
                raise SwiftProjectConflict("stored source no longer matches the approved revision")
            return {
                "project_revision": project_reference(grant),
                "source_sha256": grant["source_sha256"],
                "target": {
                    **json.loads(grant["target_json"]),
                    "source_sha256": grant["source_sha256"],
                },
                "files": [f.model_dump() for f in files],
            }

    async def cancel(self, goal_id: str, validation_id: str, requester_id: str) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            found = await (
                await db.execute(
                    "SELECT task_id FROM swift_project_validations WHERE id=? AND goal_id=?",
                    (validation_id, goal_id),
                )
            ).fetchone()
            if found is None:
                raise SwiftProjectConflict("native validation not found")
            await db.execute(
                "UPDATE swift_project_validations SET status='cancelled' WHERE id=?",
                (validation_id,),
            )
            await cancel_native_job_locked(
                db,
                task_id=found[0],
                reason="native execution consent cancelled",
                now=datetime.now(UTC).isoformat(),
                outbox=self.dispatcher.outbox,
            )
            await append_audit_event(
                db,
                "project.swift.execution_cancelled",
                {"validation_id": validation_id},
                actor_type="device",
                actor_id=requester_id,
                task_id=found[0],
                trace_id=goal_id,
                created_at=datetime.now(UTC).isoformat(),
            )
            await db.commit()
        await self.dispatcher._drain_outbox()
        return await self.get(goal_id, validation_id)
