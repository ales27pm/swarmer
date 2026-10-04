"""Bounded read observations, never execution attestation or current-file truth."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

import aiosqlite

ERROR = "invalid_workspace_read_receipt"
MAX_TEXT_BYTES = 1_000_000


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def normalized_read_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValueError(ERROR)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(ERROR)
    return path.as_posix()


def read_receipt(text: str, path: str, *, truncated: bool, provenance: str) -> dict[str, Any]:
    """The digest covers returned text re-encoded strictly, not unread source bytes."""
    raw = text.encode("utf-8")
    if (
        len(raw) > MAX_TEXT_BYTES
        or type(truncated) is not bool
        or provenance not in {"local_executor_measurement", "worker_reported_measurement"}
    ):
        raise ValueError(ERROR)
    return {
        "schema_version": "workspace-read-v1",
        "path": normalized_read_path(path),
        "encoding": "utf-8",
        "byte_scope": "returned_utf8_text",
        "returned_bytes": len(raw),
        "returned_sha256": hashlib.sha256(raw).hexdigest(),
        "complete": not truncated,
        "truncated": truncated,
        "provenance": provenance,
        "source_snapshot": "not_captured",
        "execution_attested": False,
    }


def validate_read_receipt(
    result: dict[str, Any],
    *,
    path: object | None = None,
    local: bool = False,
) -> dict[str, Any] | None:
    """Legacy omission stays omission; present malformed reports never downgrade."""
    if "read_receipt" not in result:
        return None
    receipt = result["read_receipt"]
    text = result.get("text" if local else "content")
    if not isinstance(receipt, dict) or not isinstance(text, str):
        raise ValueError(ERROR)  # noqa: TRY004 - fixed malformed-receipt diagnostic
    truncated = receipt.get("truncated")
    if not isinstance(truncated, bool):
        raise ValueError(ERROR)  # noqa: TRY004 - fixed malformed-receipt diagnostic
    if (
        type(receipt.get("returned_bytes")) is not int
        or type(receipt.get("complete")) is not bool
        or receipt.get("execution_attested") is not False
    ):
        raise ValueError(ERROR)
    try:
        expected = read_receipt(
            text,
            normalized_read_path(receipt.get("path") if path is None else path),
            truncated=truncated,
            provenance="local_executor_measurement" if local else "worker_reported_measurement",
        )
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError(ERROR) from exc
    if receipt != expected or (local and result.get("truncated") is not receipt["truncated"]):
        raise ValueError(ERROR)
    return expected


async def read_binding_locked(
    db: aiosqlite.Connection,
    *,
    task_id: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    tool_call_id: str | None = None,
    job: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Caller has authenticated execution ownership and atomically persists this audit.

    No source text, filesystem root, bearer credential or claim token enters the audit.
    The existing job/call is the identity: no invented run ID, snapshot or attestation.
    """
    if not db.in_transaction or (tool_call_id is None) == (job is None):
        raise ValueError(ERROR)
    normalized_read_path(arguments.get("path"))
    receipt = validate_read_receipt(result, path=arguments.get("path"), local=job is None)
    if receipt is None:
        return None
    cursor = await db.execute(
        """SELECT t.source,n.id AS node_id,n.goal_run_id,n.required_skill,n.worker_job_id,
        n.conversation_revision,g.id AS existing_goal,l.project_id,c.id AS existing_project
        FROM tasks t LEFT JOIN plan_nodes n ON n.task_id=t.id
        LEFT JOIN goal_runs g ON g.id=n.goal_run_id
        LEFT JOIN goal_project_links l ON l.goal_run_id=g.id
        LEFT JOIN coding_projects c ON c.id=l.project_id WHERE t.id=?""",
        (task_id,),
    )
    cursor.row_factory = aiosqlite.Row
    rows = list(await cursor.fetchall())
    if len(rows) != 1:
        raise ValueError(ERROR)
    row = dict(rows[0])
    if str(row["source"]).startswith("goal:"):
        if (
            row["existing_goal"] is None
            or row["source"] != "goal:" + str(row["goal_run_id"])
            or (row["project_id"] is not None and row["existing_project"] != row["project_id"])
            or (
                job is not None
                and (
                    row["worker_job_id"] != job["id"]
                    or row["required_skill"] != "workspace.read_text"
                )
            )
        ):
            raise ValueError(ERROR)
    elif row["node_id"] is not None:
        raise ValueError(ERROR)
    binding = {
        "schema_version": "workspace-read-binding-v1",
        "task_id": task_id,
        "project_id": row["project_id"],
        "goal_run_id": row["goal_run_id"],
        "node_id": row["node_id"],
        "conversation_revision": row["conversation_revision"],
        "arguments_sha256": canonical_sha(arguments),
        "result_sha256": canonical_sha(result),
        "receipt_sha256": canonical_sha(receipt),
        "execution_attested": False,
    }
    if job is None:
        binding.update(tool_call_id=tool_call_id, identity_evidence="local_executor")
    else:
        binding.update(
            job_id=job["id"],
            agent_id=job["claimed_by"],
            lease_id=job["lease_id"],
            lease_generation=job["lease_generation"],
            identity_evidence="authenticated_lease_only",
        )
    return binding
