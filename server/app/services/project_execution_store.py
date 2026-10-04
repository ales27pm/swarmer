"""Server-bound receipt history. A lease holder is not an attested producer."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import aiosqlite

from app.services.project_contracts import PROJECT_SKILL, ProjectPayload, ProjectResult
from app.services.project_execution_receipts import canonical_sha


class ProjectExecutionConflict(ValueError):
    """A report cannot be bound to its authoritative execution context."""


def _require(condition: bool) -> None:
    if not condition:
        raise ProjectExecutionConflict("project execution binding is invalid")


def _transaction(db: aiosqlite.Connection) -> None:
    _require(db.in_transaction)


async def _one(db: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
    cursor = await db.execute(sql, args)
    cursor.row_factory = aiosqlite.Row
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def _scope(db: aiosqlite.Connection, job: dict[str, Any]) -> dict[str, Any]:
    task = await _one(db, "SELECT source FROM tasks WHERE id=?", (job["task_id"],))
    _require(task is not None)
    assert task is not None
    node = await _one(
        db,
        """SELECT n.id AS node_id,n.goal_run_id,n.worker_job_id,n.required_skill,
        n.conversation_revision,l.project_id,g.id AS existing_goal,c.id AS existing_project
        FROM plan_nodes n LEFT JOIN goal_runs g ON g.id=n.goal_run_id
        LEFT JOIN goal_project_links l ON l.goal_run_id=n.goal_run_id
        LEFT JOIN coding_projects c ON c.id=l.project_id WHERE n.task_id=?""",
        (job["task_id"],),
    )
    if not str(task["source"]).startswith("goal:"):
        _require(node is None)
        return {"project_id": None, "goal_run_id": None, "node_id": None, "conversation_revision": None}
    _require(
        node is not None
        and task["source"] == "goal:" + str(node["goal_run_id"])
        and node["worker_job_id"] == job["id"]
        and node["required_skill"] == PROJECT_SKILL
        and node["existing_goal"] == node["goal_run_id"]
        and node["existing_project"] == node["project_id"]
        and node["project_id"] is not None
        and type(node["conversation_revision"]) is int
        and node["conversation_revision"] >= 0
    )
    assert node is not None
    return {
        key: node[key] for key in ("project_id", "goal_run_id", "node_id", "conversation_revision")
    }


def _report(job: dict[str, Any], raw_result: dict[str, Any]) -> ProjectResult:
    _require(job["required_skill"] == PROJECT_SKILL)
    result = ProjectResult.model_validate(raw_result)
    payload = ProjectPayload.model_validate_json(job["payload_json"])
    _require(result.execution_receipt is not None)
    _require(
        (result.base_revision_id, result.base_sha256)
        == (payload.base_revision_id, payload.base_sha256)
    )
    return result


def _digests(
    job: dict[str, Any], raw_result: dict[str, Any], result: ProjectResult
) -> dict[str, str]:
    assert result.execution_receipt is not None
    receipt = result.execution_receipt
    return {
        "payload_sha256": canonical_sha(json.loads(job["payload_json"])),
        "result_sha256": canonical_sha(raw_result),
        "receipt_sha256": canonical_sha(receipt.model_dump()),
        "source_sha256": receipt.source_sha256,
        "run_id": receipt.run_id,
        "observation_status": receipt.observation_status,
    }


async def accept_project_execution_locked(
    db: aiosqlite.Connection,
    job: dict[str, Any],
    raw_result: dict[str, Any] | None,
    *,
    agent_id: str,
    now: str,
) -> str | None:
    """Call after lease validation, inside the transaction terminating the job.

    No reconstruction from historical terminal results is allowed. Metadata is
    observed at acceptance, not claimed to describe the code actually executed.
    """
    _transaction(db)
    if raw_result is None or raw_result.get("execution_receipt") is None:
        return None
    result = _report(job, raw_result)
    _require(job["status"] in {"claimed", "running"} and job["claimed_by"] == agent_id)
    _require(
        isinstance(job["lease_id"], str)
        and bool(job["lease_id"])
        and type(job["lease_generation"]) is int
        and job["lease_generation"] > 0
        and isinstance(job["claimed_at"], str)
        and bool(job["claimed_at"])
    )
    producer = await _one(
        db,
        "SELECT id,version,runtime,supported_protocol_version,created_at FROM agents WHERE id=?",
        (agent_id,),
    )
    _require(producer is not None)
    assert producer is not None
    _require(all(isinstance(value, str) and 0 < len(value) <= 200 for value in producer.values()))
    producer_json = json.dumps(
        {"binding": "authenticated_lease", "metadata_observed_at": now, "metadata": producer},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    _require(len(producer_json.encode()) <= 2048)
    values = {
        "id": "execution_" + uuid4().hex,
        "job_id": job["id"],
        "task_id": job["task_id"],
        "producer_agent_id": agent_id,
        "lease_id": job["lease_id"],
        "lease_generation": job["lease_generation"],
        "claimed_at": job["claimed_at"],
        "accepted_at": now,
        **await _scope(db, job),
        **_digests(job, raw_result, result),
        "producer_json": producer_json,
    }
    try:
        await db.execute(
            f"INSERT INTO project_execution_acceptances({','.join(values)}) "
            f"VALUES({','.join('?' for _ in values)})",
            tuple(values.values()),
        )
    except aiosqlite.IntegrityError as exc:
        raise ProjectExecutionConflict("project execution report was already bound") from exc
    return str(values["id"])


async def link_project_execution_locked(
    db: aiosqlite.Connection,
    *,
    revision_id: str,
    now: str,
    replay: bool = False,
) -> None:
    """Bind only an existing server acceptance to an authoritative revision.

    Absence is historical/erased evidence, never permission to backfill it.
    """
    _transaction(db)
    revision = await _one(db, "SELECT * FROM project_revisions WHERE id=?", (revision_id,))
    _require(revision is not None)
    assert revision is not None
    accepted = await _one(
        db,
        "SELECT * FROM project_execution_acceptances WHERE job_id=?",
        (revision["worker_job_id"],),
    )
    if accepted is None:
        snapshot = ProjectResult.model_validate_json(revision["snapshot_json"])
        _require(replay or snapshot.execution_receipt is None)
        return
    job = await _one(db, "SELECT * FROM agent_jobs WHERE id=?", (revision["worker_job_id"],))
    _require(job is not None)
    assert job is not None
    _require(job["status"] == "completed")
    raw_result = json.loads(job["result_json"])
    result = _report(job, raw_result)
    expected = {
        "job_id": job["id"],
        "task_id": job["task_id"],
        "producer_agent_id": job["claimed_by"],
        "lease_id": job["lease_id"],
        "lease_generation": job["lease_generation"],
        "claimed_at": job["claimed_at"],
        **await _scope(db, job),
        **_digests(job, raw_result, result),
    }
    _require(all(accepted[key] == value for key, value in expected.items()))
    _require(accepted["accepted_at"] == job["completed_at"])
    _require(
        all(accepted[key] == revision[key] for key in ("project_id", "goal_run_id", "node_id"))
    )
    snapshot = ProjectResult.model_validate_json(revision["snapshot_json"])
    _require(snapshot.execution_receipt is not None)
    assert snapshot.execution_receipt is not None
    _require(canonical_sha(snapshot.execution_receipt.model_dump()) == accepted["receipt_sha256"])
    _require(revision["sha256"] == accepted["source_sha256"])
    link = {
        "acceptance_id": accepted["id"],
        "revision_id": revision_id,
        "project_id": revision["project_id"],
        "source_sha256": revision["sha256"],
        "snapshot_sha256": canonical_sha(json.loads(revision["snapshot_json"])),
    }
    existing = await _one(
        db,
        "SELECT * FROM project_execution_revision_links WHERE acceptance_id=?",
        (accepted["id"],),
    )
    if existing is not None:
        _require(all(existing[key] == value for key, value in link.items()))
        return
    _require(not replay)
    await db.execute(
        f"INSERT INTO project_execution_revision_links({','.join(link)},linked_at) "
        f"VALUES({','.join('?' for _ in link)},?)",
        (*link.values(), now),
    )


async def require_project_execution_replay_locked(
    db: aiosqlite.Connection,
    job: dict[str, Any],
    raw_result: dict[str, Any] | None,
) -> None:
    """A terminal replay never recreates a missing or erased acceptance."""
    _transaction(db)
    if (
        job["status"] != "completed"
        or raw_result is None
        or raw_result.get("execution_receipt") is None
    ):
        return
    result = _report(job, raw_result)
    accepted = await _one(
        db,
        "SELECT * FROM project_execution_acceptances WHERE job_id=? AND lease_generation=?",
        (job["id"], job["lease_generation"]),
    )
    _require(accepted is not None)
    assert accepted is not None
    expected = {
        "job_id": job["id"],
        "task_id": job["task_id"],
        "producer_agent_id": job["claimed_by"],
        "lease_id": job["lease_id"],
        "lease_generation": job["lease_generation"],
        "claimed_at": job["claimed_at"],
        "accepted_at": job["completed_at"],
        **await _scope(db, job),
        **_digests(job, raw_result, result),
    }
    _require(all(accepted[key] == value for key, value in expected.items()))


async def delete_project_execution_records_locked(
    db: aiosqlite.Connection, project_id: str
) -> None:
    """Erase these registries as part of an authorized project erasure.

    The caller must also erase the project's existing job/snapshot JSON copies;
    this internal helper is not an end-user deletion operation or a new route.
    """
    _transaction(db)
    await db.execute(
        """DELETE FROM project_execution_revision_links WHERE project_id=? OR acceptance_id IN
        (SELECT id FROM project_execution_acceptances WHERE project_id=?)""",
        (project_id, project_id),
    )
    await db.execute("DELETE FROM project_execution_acceptances WHERE project_id=?", (project_id,))
