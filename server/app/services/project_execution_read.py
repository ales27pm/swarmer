"""Read-only qualification of server-bound, historically reported measurements."""

from __future__ import annotations

import json

import aiosqlite

from app.services.project_contracts import PROJECT_SKILL, ProjectResult
from app.services.project_execution_contracts import ProjectExecutionReceipt
from app.services.project_execution_receipts import canonical_sha
from app.services.project_execution_store import _digests, _one, _report, _scope

MAX_REPORT_BYTES = 4_000_000


async def read_project_execution_observation(
    db: aiosqlite.Connection,
    *,
    project_id: str,
    node_id: str,
    job_id: str,
    revision_id: str,
) -> ProjectExecutionReceipt | None:
    """Qualify exact identities in the caller's snapshot; never repair/backfill.

    A matching acceptance proves server binding to a lease holder, not execution
    attestation, present applicability, semantic correctness, or a validated lesson.
    The shared store helpers used here only select/validate/calculate digests.
    """
    if not db.in_transaction:
        raise ValueError("project execution read requires a transaction")
    if any(
        not isinstance(value, str) or not 1 <= len(value) <= 200
        for value in (project_id, node_id, job_id, revision_id)
    ):
        return None
    try:
        job = await _one(db, "SELECT * FROM agent_jobs WHERE id=?", (job_id,))
        if job is None or job["status"] != "completed" or job["required_skill"] != PROJECT_SKILL:
            return None
        if any(
            not isinstance(job[key], str) or len(job[key].encode()) > MAX_REPORT_BYTES
            for key in ("payload_json", "result_json")
        ):
            return None
        raw_result = json.loads(job["result_json"])
        result = _report(job, raw_result)
        scope = await _scope(db, job)
        if scope["project_id"] != project_id or scope["node_id"] != node_id:
            return None
        node = await _one(db, "SELECT status,node_type FROM plan_nodes WHERE id=?", (node_id,))
        task = await _one(db, "SELECT status FROM tasks WHERE id=?", (job["task_id"],))
        producer = await _one(db, "SELECT id FROM agents WHERE id=?", (job["claimed_by"],))
        if (
            node != {"status": "completed", "node_type": "worker"}
            or task != {"status": "completed"}
            or producer is None
        ):
            return None
        # More than one historical acceptance for a terminal job is ambiguous;
        # never pick whichever row happens to come first.
        cursor = await db.execute(
            "SELECT * FROM project_execution_acceptances WHERE job_id=? LIMIT 2", (job_id,)
        )
        cursor.row_factory = aiosqlite.Row
        candidates = list(await cursor.fetchall())
        if len(candidates) != 1:
            return None
        accepted = dict(candidates[0])
        expected = {
            "job_id": job_id,
            "task_id": job["task_id"],
            "producer_agent_id": job["claimed_by"],
            "lease_id": job["lease_id"],
            "lease_generation": job["lease_generation"],
            "claimed_at": job["claimed_at"],
            "accepted_at": job["completed_at"],
            **scope,
            **_digests(job, raw_result, result),
        }
        if any(accepted[key] != value for key, value in expected.items()):
            return None
        if (
            not isinstance(accepted["producer_json"], str)
            or len(accepted["producer_json"].encode()) > 2048
        ):
            return None
        producer_record = json.loads(accepted["producer_json"])
        if (
            not isinstance(producer_record, dict)
            or set(producer_record) != {"binding", "metadata_observed_at", "metadata"}
            or producer_record["binding"] != "authenticated_lease"
            or producer_record["metadata_observed_at"] != accepted["accepted_at"]
        ):
            return None
        metadata = producer_record["metadata"]
        if (
            not isinstance(metadata, dict)
            or set(metadata)
            != {"id", "version", "runtime", "supported_protocol_version", "created_at"}
            or metadata["id"] != accepted["producer_agent_id"]
            or any(
                not isinstance(value, str) or not 1 <= len(value) <= 200
                for value in metadata.values()
            )
        ):
            return None
        revision = await _one(db, "SELECT * FROM project_revisions WHERE id=?", (revision_id,))
        if revision is None or any(
            revision[key] != scope[key] for key in ("project_id", "goal_run_id", "node_id")
        ):
            return None
        if revision["worker_job_id"] != job_id or revision["sha256"] != accepted["source_sha256"]:
            return None
        raw_snapshot = revision["snapshot_json"]
        if not isinstance(raw_snapshot, str) or len(raw_snapshot.encode()) > MAX_REPORT_BYTES:
            return None
        snapshot = ProjectResult.model_validate_json(raw_snapshot)
        if (
            snapshot.execution_receipt is None
            or canonical_sha(snapshot.execution_receipt.model_dump()) != accepted["receipt_sha256"]
        ):
            return None
        link = await _one(
            db,
            "SELECT * FROM project_execution_revision_links WHERE acceptance_id=?",
            (accepted["id"],),
        )
        expected_link = {
            "acceptance_id": accepted["id"],
            "revision_id": revision_id,
            "project_id": project_id,
            "source_sha256": revision["sha256"],
            "snapshot_sha256": canonical_sha(json.loads(raw_snapshot)),
        }
        if link is None or any(link[key] != value for key, value in expected_link.items()):
            return None
        return result.execution_receipt
    except (TypeError, ValueError, KeyError, RecursionError):
        return None


def project_execution_summary(
    receipt: ProjectExecutionReceipt, *, max_chars: int = 600
) -> str | None:
    """All profile outcomes and caveats fit, or the entire observation is omitted."""
    profiles = []
    for profile in receipt.profiles:
        observation = profile.observation

        def flag(value: bool | None) -> str:
            return "unknown" if value is None else "same" if value else "changed"

        def number(value: int | None) -> str:
            return "unknown" if value is None else str(value)

        profiles.append(
            f"{profile.profile}: exit={number(profile.exit_code)}, tests={number(profile.tests_executed)}, "
            f"failed={number(profile.test_failures)}, "
            f"source={flag(observation.source_unchanged if observation else None)}, "
            f"deps={flag(observation.environment_unchanged if observation else None)}"
        )
    summary = (
        "Worker-reported measurements, server-bound to a historical revision; not attested, "
        "not proof for current code, task success or a validated lesson. "
        f"Measurement={receipt.observation_status}. "
        + "; ".join(profiles)
        + (
            ". Reasons=" + ",".join(receipt.incomplete_reasons)
            if receipt.incomplete_reasons
            else ""
        )
        + "."
    )
    return summary if len(summary) <= max_chars else None
