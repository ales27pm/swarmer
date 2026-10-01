"""Project-scoped historical observations, not automatically learned policies."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from app.services.context_builder import safe_context_text
from app.services.result_aggregator import validate_worker_evidence

_LIMIT = 6
_SCAN_LIMIT = 32


async def read_worker_experiences(db: aiosqlite.Connection, project_id: str) -> dict[str, Any]:
    """Use the caller's snapshot and authoritative goal/task/job linkage.

    Only structured observations are projected. Arbitrary narration, source
    content and rejected drafts cannot become a reusable lesson here. Receipts
    remain in the database; bounded omissions are counted rather than hidden.
    """
    rows = list(
        await (
            await db.execute(
                """SELECT n.id AS node_id,n.goal_run_id,n.status AS node_status,
                j.id AS job_id,j.required_skill,j.status,j.result_json,j.payload_json,j.error,
                n.error_summary AS node_error_summary,
                r.id AS revision_id,r.sha256 AS revision_sha256,
                COUNT(*) OVER() AS total_count
            FROM plan_nodes n
            JOIN goal_project_links p ON p.goal_run_id=n.goal_run_id
            JOIN agent_jobs j ON j.id=n.worker_job_id AND j.task_id=n.task_id
                AND j.required_skill=n.required_skill
            JOIN tasks t ON t.id=j.task_id AND t.source='goal:' || n.goal_run_id
            LEFT JOIN project_revisions r ON r.worker_job_id=j.id
                AND r.node_id=n.id AND r.goal_run_id=n.goal_run_id AND r.project_id=p.project_id
            WHERE p.project_id=? AND n.node_type='worker'
                AND n.status=j.status AND j.status IN ('completed','failed')
            ORDER BY j.created_at DESC,j.id DESC LIMIT ?""",
                (project_id, _SCAN_LIMIT),
            )
        ).fetchall()
    )
    total = int(rows[0]["total_count"]) if rows else 0
    items: list[dict[str, Any]] = []
    for row in rows:
        if len(items) == _LIMIT:
            break
        raw = row["result_json"]
        if raw is not None and len(str(raw).encode()) > 524_288:
            continue
        try:
            result = json.loads(raw) if raw is not None else None
            kind, summary = _observation(dict(row), result)
        except (TypeError, ValueError, KeyError):
            continue
        items.append(
            {
                "source_id": str(row["node_id"]),
                "worker_job_id": str(row["job_id"]),
                "required_skill": str(row["required_skill"]),
                "outcome": str(row["status"]),
                "observation_kind": kind,
                # The containing revision is not necessarily the tested one:
                # old workers could carry checks across an inspection step.
                "source_revision_id": row["revision_id"],
                "source_sha256": row["revision_sha256"],
                "summary": safe_context_text(summary, max_chars=600),
                "content_trust": "untrusted",
                "applicability": "historical",
            }
        )
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
