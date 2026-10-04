"""Project-scoped historical observations, not automatically learned policies."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from app.services.context_builder import safe_context_text
from app.services.project_execution_read import (
    MAX_REPORT_BYTES,
    project_execution_summary,
    read_project_execution_observation,
)
from app.services.result_aggregator import validate_worker_evidence

_LIMIT = 6
_SCAN_LIMIT = 32
_CONTEXT_CHANGED = "worker_experience_context_changed"

_SELECT = """SELECT n.id AS node_id,n.goal_run_id,n.status AS node_status,
    j.id AS job_id,j.required_skill,j.status,j.result_json,j.payload_json,j.error,
    n.error_summary AS node_error_summary,
    r.id AS revision_id,r.sha256 AS revision_sha256,r.snapshot_json AS revision_snapshot_json,
    EXISTS(SELECT 1 FROM project_execution_acceptances a WHERE a.job_id=j.id)
        OR EXISTS(SELECT 1 FROM project_execution_revision_links l WHERE l.revision_id=r.id)
        AS execution_acceptance_present,
    COUNT(*) OVER() AS total_count
FROM plan_nodes n
JOIN goal_project_links p ON p.goal_run_id=n.goal_run_id
JOIN agent_jobs j ON j.id=n.worker_job_id AND j.task_id=n.task_id
    AND j.required_skill=n.required_skill
JOIN tasks t ON t.id=j.task_id AND t.source='goal:' || n.goal_run_id
LEFT JOIN project_revisions r ON r.worker_job_id=j.id
    AND r.node_id=n.id AND r.goal_run_id=n.goal_run_id AND r.project_id=p.project_id
WHERE p.project_id=? AND n.node_type='worker'
    AND n.status=j.status AND j.status IN ('completed','failed')"""


async def _rows(
    db: aiosqlite.Connection, project_id: str, *, selected: tuple[str, str] | None = None
) -> list[aiosqlite.Row]:
    args: tuple[Any, ...]
    if selected is None:
        sql = _SELECT + " ORDER BY j.created_at DESC,j.id DESC LIMIT ?"
        args = (project_id, _SCAN_LIMIT)
    else:
        sql = _SELECT + " AND n.id=? AND j.id=? LIMIT 2"
        args = (project_id, *selected)
    cursor = await db.execute(sql, args)
    cursor.row_factory = aiosqlite.Row
    return list(await cursor.fetchall())


async def _item(
    db: aiosqlite.Connection, project_id: str, row: aiosqlite.Row
) -> dict[str, Any] | None:
    raw = row["result_json"]
    if raw is not None and (not isinstance(raw, str) or len(raw.encode()) > 524_288):
        return None
    try:
        result = json.loads(raw) if raw is not None else None
        has_receipt = isinstance(result, dict) and result.get("execution_receipt") is not None
        if row["required_skill"] == "code.build_project" and row["revision_id"] is not None:
            snapshot_raw = row["revision_snapshot_json"]
            if not isinstance(snapshot_raw, str) or len(snapshot_raw.encode()) > MAX_REPORT_BYTES:
                return None
            snapshot = json.loads(snapshot_raw)
            if not isinstance(snapshot, dict):
                return None
            has_receipt = has_receipt or snapshot.get("execution_receipt") is not None
        if row["execution_acceptance_present"] or has_receipt:
            receipt = await read_project_execution_observation(
                db,
                project_id=project_id,
                node_id=row["node_id"],
                job_id=row["job_id"],
                revision_id=row["revision_id"],
            )
            if receipt is None:
                return None
            summary = project_execution_summary(receipt)
            if summary is None:
                return None
            kind = "accepted_result"
        else:
            kind, summary = _observation(dict(row), result)
            summary = safe_context_text(summary, max_chars=600)
    except (TypeError, ValueError, KeyError, RecursionError):
        return None
    return {
        "source_id": str(row["node_id"]),
        "worker_job_id": str(row["job_id"]),
        "required_skill": str(row["required_skill"]),
        "outcome": str(row["status"]),
        "observation_kind": kind,
        "source_revision_id": row["revision_id"],
        "source_sha256": row["revision_sha256"],
        "summary": summary,
        "content_trust": "untrusted",
        "applicability": "historical",
    }


async def require_selected_worker_experiences_locked(
    db: aiosqlite.Connection, *, project_id: str, items: list[dict[str, Any]]
) -> None:
    """Requalify exact proposed cards, not the latest ranked selection; no writes."""
    if (
        not db.in_transaction
        or not isinstance(project_id, str)
        or not 1 <= len(project_id) <= 200
        or not isinstance(items, list)
        or len(items) > _LIMIT
    ):
        raise ValueError(_CONTEXT_CHANGED)
    # Freeze and bound caller input before the first await. The wire shape has
    # only scalar values; reject nested data instead of recursively decoding it.
    selected = []
    identities = set()
    for item in items:
        if (
            not isinstance(item, dict)
            or len(item) != 10
            or any(
                not isinstance(key, str)
                or len(key) > 50
                or (value is not None and not isinstance(value, str))
                or (isinstance(value, str) and len(value) > 600)
                for key, value in item.items()
            )
        ):
            raise ValueError(_CONTEXT_CHANGED)
        identity = (item.get("source_id"), item.get("worker_job_id"))
        if (
            any(not isinstance(value, str) or not 1 <= len(value) <= 200 for value in identity)
            or identity in identities
        ):
            raise ValueError(_CONTEXT_CHANGED)
        identities.add(identity)
        selected.append(dict(item))
    for item in selected:
        rows = await _rows(db, project_id, selected=(item["source_id"], item["worker_job_id"]))
        if len(rows) != 1 or await _item(db, project_id, rows[0]) != item:
            raise ValueError(_CONTEXT_CHANGED)


async def read_worker_experiences(db: aiosqlite.Connection, project_id: str) -> dict[str, Any]:
    """Use the caller's snapshot and authoritative goal/task/job linkage.

    Only structured observations are projected. Arbitrary narration, source
    content and rejected drafts cannot become a reusable lesson here. Receipts
    remain in the database; bounded omissions are counted rather than hidden.
    """
    if not db.in_transaction:
        raise ValueError("worker experience read requires a transaction")
    rows = await _rows(db, project_id)
    total = int(rows[0]["total_count"]) if rows else 0
    items: list[dict[str, Any]] = []
    for row in rows:
        if len(items) == _LIMIT:
            break
        item = await _item(db, project_id, row)
        if item is not None:
            items.append(item)
    return {"items": items, "omitted_count": max(0, total - len(items))}


def _observation(row: dict[str, Any], result: Any) -> tuple[str, str]:
    skill = row["required_skill"]
    if row["status"] == "failed":
        if skill == "writing.draft" and isinstance(result, dict):
            from app.services.writing_contracts import (
                validate_writing_failure_diagnostics,
                writing_failure_summary,
            )

            raw_payload = row["payload_json"]
            if len(raw_payload.encode()) > 32_000:
                raise ValueError("historical payload exceeds its bound")
            measured = validate_writing_failure_diagnostics(result, payload=json.loads(raw_payload))
            return "measured_failure", writing_failure_summary(measured)
        if (
            skill == "writing.draft"
            and result is None
            and row.get("error") == row.get("node_error_summary") == "invalid_output"
        ):
            return (
                "reported_failure",
                (
                    "The writing worker reported invalid_output; no valid draft was accepted. "
                    "No word-count or citation measurements were recorded. The specific defect "
                    "and a successful remedy are not established; this is historical feedback."
                ),
            )
        return (
            "reported_failure",
            (
                "A worker failure was recorded. Its cause and a successful remedy are not established; "
                "inspect the referenced job before repeating the operation."
            ),
        )
    if not validate_worker_evidence(skill, result):
        raise ValueError("historical result contract is invalid")
    if skill == "code.build_project":
        if row["revision_id"] is None:
            raise ValueError("project result has no accepted revision")
        checks = result.get("checks", [])
        details = [
            {
                "command": check["command"],
                "status": check["status"],
                "exit_code": check["exit_code"],
            }
            for check in checks[:2]
        ]
        return (
            "accepted_result",
            "Accepted project snapshot. Historical recorded checks, not proof for current code "
            "(original execution revision is not established by the containing revision): "
            + json.dumps(details, ensure_ascii=False, separators=(",", ":")),
        )
    if skill == "research.collect":
        from app.services.research_contracts import valid_research_collect_receipt

        if not valid_research_collect_receipt(result, json.loads(row["payload_json"])):
            raise ValueError("historical research receipt changed")
        count = sum(page["status"] == "read" for page in result["pages"])
        return (
            "accepted_result",
            f"Recorded {count} page reads. This does not establish task completion.",
        )
    if skill == "writing.draft":
        return (
            "accepted_result",
            "A textual draft was accepted. Recheck its content against current requirements.",
        )
    return (
        "accepted_result",
        f"A {skill} result was accepted. This establishes an operation outcome, not overall task correctness.",
    )
