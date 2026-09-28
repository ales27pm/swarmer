"""Transactional, explicit requirement-to-evidence links; never infer verification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.services.audit_log import append_audit_event
from app.services.feedback_dataset import redact_dataset_text
from app.services.project_evidence_contracts import (
    EvidenceCriterion,
    EvidenceMapping,
    EvidenceMappingRequest,
    RequirementEvidenceView,
    StaleReason,
)
from app.services.project_graph import ProjectGraphEvidenceError, _goal, _read_nodes, _read_revision
from app.services.project_graph_contracts import GraphRevision


class EvidenceMappingConflict(ValueError):
    """The submitted selection no longer names the exact current evidence."""


class EvidenceGoalNotFound(ValueError):
    """The requested goal is not present."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def criterion_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe(text: str, limit: int = 500) -> str:
    return (redact_dataset_text(text) or "Texte masqué.")[:limit]


@dataclass(frozen=True)
class _Context:
    goal_id: str
    project_id: str | None
    conversation_revision: int
    active_goal_id: str | None
    criteria: list[str]
    goal_sha256: str
    context_sha256: str
    revision: GraphRevision | None
    evidence_sha256: str | None


async def _context(db: aiosqlite.Connection, goal_id: str) -> _Context | None:
    row = await (
        await db.execute(
            """SELECT g.*,l.project_id,c.active_goal_id FROM goal_runs g
        LEFT JOIN goal_project_links l ON l.goal_run_id=g.id
        LEFT JOIN goal_conversation_links cl ON cl.goal_run_id=g.id
        LEFT JOIN goal_conversations c ON c.id=cl.conversation_id WHERE g.id=?""",
            (goal_id,),
        )
    ).fetchone()
    if row is None:
        return None
    _goal(row)  # Validate the same bounded requirement/goal contract as the public graph.
    criteria = json.loads(row["completion_criteria_json"])
    nodes = await _read_nodes(db, goal_id)
    # _read_nodes validates before redacting. Bind the stored objectives from this
    # same transaction too: different private targets can share one public label.
    raw_objectives = {
        item["id"]: item["objective"]
        for item in await (
            await db.execute(
                "SELECT id,objective FROM plan_nodes WHERE goal_run_id=? "
                "ORDER BY created_at,id LIMIT 21",
                (goal_id,),
            )
        ).fetchall()
    }
    if set(raw_objectives) != {node.id for node in nodes} or any(
        not isinstance(objective, str) or not 1 <= len(objective) <= 4_000
        for objective in raw_objectives.values()
    ):
        raise ProjectGraphEvidenceError("stored plan objectives are invalid")
    revision = await _read_revision(db, row["project_id"])
    evidence_sha = None
    if revision is not None:
        snapshot_row = await (
            await db.execute(
                "SELECT snapshot_json FROM project_revisions WHERE id=?",
                (revision.id,),
            )
        ).fetchone()
        if snapshot_row is None:
            raise ProjectGraphEvidenceError("project evidence snapshot unavailable")
        snapshot = json.loads(snapshot_row["snapshot_json"])
        # Bind the complete check receipts as well as source digests. File digest alone does
        # not detect a changed/forged receipt or output on the same source revision.
        evidence_sha = _digest(
            {
                "revision": revision.model_dump(),
                "checks": snapshot["checks"],
            }
        )
    goal_sha = _digest(
        {
            "goal_id": goal_id,
            "project_id": row["project_id"],
            "objective": row["objective"],
            "criteria": criteria,
            "conversation_revision": row["conversation_revision"],
            "replan_count": row["replan_count"],
            "active_goal_id": row["active_goal_id"],
            "plan": [
                {
                    "id": node.id,
                    "objective": raw_objectives[node.id],
                    "depends_on": node.depends_on,
                }
                for node in nodes
            ],
        }
    )
    return _Context(
        goal_id=goal_id,
        project_id=row["project_id"],
        conversation_revision=row["conversation_revision"],
        active_goal_id=row["active_goal_id"],
        criteria=criteria,
        goal_sha256=goal_sha,
        context_sha256=_digest({"goal": goal_sha, "evidence": evidence_sha}),
        revision=revision,
        evidence_sha256=evidence_sha,
    )


async def _latest_mappings(db: aiosqlite.Connection, goal_id: str) -> list[aiosqlite.Row]:
    return list(
        await (
            await db.execute(
                """SELECT e.* FROM project_requirement_evidence e JOIN
        (SELECT criterion_index,MAX(version) AS version FROM project_requirement_evidence
        WHERE goal_run_id=? GROUP BY criterion_index) latest
        ON latest.criterion_index=e.criterion_index AND latest.version=e.version
        WHERE e.goal_run_id=? ORDER BY e.criterion_index LIMIT 21""",
                (goal_id, goal_id),
            )
        ).fetchall()
    )


def _mapping(row: aiosqlite.Row, context: _Context) -> EvidenceMapping:
    item = EvidenceMapping.model_validate_json(row["mapping_json"])
    if (
        item.id != row["id"]
        or item.goal_run_id != context.goal_id
        or item.project_id != row["project_id"]
        or item.revision_id != row["revision_id"]
        or item.criterion_index != row["criterion_index"]
        or item.version != row["version"]
        or item.criterion_sha256 != criterion_digest(row["criterion_text"])
        or item.criterion_id != f"criterion:{context.goal_id}:{item.criterion_index}"
        or item.file_ids != [file.id for file in item.files]
        or item.check_ids != [check.id for check in item.checks]
        or not (item.file_ids or item.check_ids)
        or (item.review_status == "reviewed") != (item.reviewed_at is not None)
        or (
            item.review_status == "reviewed"
            and (
                not item.checks
                or any(check.status != "passed" or check.exit_code != 0 for check in item.checks)
            )
        )
    ):
        raise ProjectGraphEvidenceError("stored requirement evidence is inconsistent")
    reasons: list[StaleReason] = []
    if (
        row["goal_sha256"] != context.goal_sha256
        or item.criterion_index >= len(context.criteria)
        or item.criterion_sha256 != criterion_digest(context.criteria[item.criterion_index])
    ):
        reasons.append("goal_changed")
    revision = context.revision
    if revision is None or (revision.id, revision.sha256) != (
        item.revision_id,
        item.revision_sha256,
    ):
        reasons.append("revision_changed")
    if revision is None or (
        revision.project_id,
        revision.goal_run_id,
        revision.node_id,
        revision.worker_job_id,
    ) != (item.project_id, item.goal_run_id, item.node_id, item.worker_job_id):
        reasons.append("producer_changed")
    if row["evidence_sha256"] != context.evidence_sha256:
        reasons.append("evidence_changed")
    if not reasons and revision is not None:
        files = {file.id: file for file in revision.files}
        checks = {check.id: check for check in revision.checks}
        if (
            any(files.get(file.id) != file for file in item.files)
            or any(checks.get(check.id) != check for check in item.checks)
            or len(set(item.file_ids)) != len(item.file_ids)
            or len(set(item.check_ids)) != len(item.check_ids)
        ):
            raise ProjectGraphEvidenceError("mapping selections do not match recorded evidence")
    return item.model_copy(
        update={
            "stale_reasons": reasons,
            "criterion_text": _safe(row["criterion_text"]),
            "public_explanation": (redact_dataset_text(item.public_explanation) or "")[:2_000],
        }
    )


async def _view(db: aiosqlite.Connection, context: _Context) -> RequirementEvidenceView:
    rows = await _latest_mappings(db, context.goal_id)
    if len(rows) > 20:
        raise ProjectGraphEvidenceError("requirement evidence exceeds criterion bound")
    mappings = {row["criterion_index"]: _mapping(row, context) for row in rows}
    criteria = []
    for index, text in enumerate(context.criteria):
        mapping = mappings.get(index)
        status = (
            "unmapped"
            if mapping is None
            else ("stale" if mapping.stale_reasons else mapping.review_status)
        )
        criteria.append(
            EvidenceCriterion(
                id=f"criterion:{context.goal_id}:{index}",
                index=index,
                text=_safe(text),
                sha256=criterion_digest(text),
                status=status,
                mapping=mapping,
            )
        )
    return RequirementEvidenceView(
        observed_at=datetime.now(UTC).isoformat(),
        goal_run_id=context.goal_id,
        project_id=context.project_id,
        context_sha256=context.context_sha256,
        conversation_revision=context.conversation_revision,
        current_revision=context.revision,
        criteria=criteria,
        unmatched_mappings=[item for index, item in mappings.items() if index >= len(criteria)],
    )


async def read_project_evidence(db_path: Path, goal_id: str) -> RequirementEvidenceView | None:
    async with aiosqlite.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        try:
            context = await _context(db, goal_id)
            return None if context is None else await _view(db, context)
        except (ValueError, TypeError, KeyError) as exc:
            raise ProjectGraphEvidenceError("stored requirement evidence is invalid") from exc
        finally:
            await db.rollback()


async def save_project_evidence(
    db_path: Path,
    goal_id: str,
    criterion_index: int,
    request: EvidenceMappingRequest,
    actor_id: str,
) -> RequirementEvidenceView:
    async with aiosqlite.connect(db_path, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("BEGIN IMMEDIATE")
        try:
            context = await _context(db, goal_id)
            if context is None:
                raise EvidenceGoalNotFound("goal not found")
            request_sha = _digest({"criterion_index": criterion_index, **request.model_dump()})
            replay = await (
                await db.execute(
                    "SELECT request_sha256 FROM project_requirement_evidence "
                    "WHERE goal_run_id=? AND request_id=?",
                    (goal_id, request.request_id),
                )
            ).fetchone()
            if replay is not None:
                if replay["request_sha256"] != request_sha:
                    raise EvidenceMappingConflict("request identifier already used")
                return await _view(db, context)
            if not 0 <= criterion_index < len(context.criteria):
                raise EvidenceMappingConflict("criterion no longer exists")
            if (
                request.context_sha256 != context.context_sha256
                or request.conversation_revision != context.conversation_revision
                or request.criterion_sha256 != criterion_digest(context.criteria[criterion_index])
                or context.active_goal_id not in (None, goal_id)
            ):
                raise EvidenceMappingConflict("goal or criterion changed; refresh evidence")
            revision = context.revision
            if revision is None or (
                request.project_id,
                request.revision_id,
                request.revision_sha256,
                goal_id,
                request.node_id,
            ) != (
                revision.project_id,
                revision.id,
                revision.sha256,
                revision.goal_run_id,
                revision.node_id,
            ):
                raise EvidenceMappingConflict("revision or producer does not match this goal")
            previous = await (
                await db.execute(
                    "SELECT MAX(version) FROM project_requirement_evidence "
                    "WHERE goal_run_id=? AND criterion_index=?",
                    (goal_id, criterion_index),
                )
            ).fetchone()
            version = int(previous[0] or 0) if previous else 0
            if request.expected_version != version:
                raise EvidenceMappingConflict("mapping changed; refresh evidence")
            files = {file.id: file for file in revision.files}
            checks = {check.id: check for check in revision.checks}
            if (
                not set(request.file_ids) <= files.keys()
                or not set(request.check_ids) <= checks.keys()
            ):
                raise EvidenceMappingConflict("selected evidence does not belong to this revision")
            selected_checks = [checks[key] for key in request.check_ids]
            if request.review_status == "reviewed" and any(
                check.status != "passed" or check.exit_code != 0 for check in selected_checks
            ):
                raise EvidenceMappingConflict("explicit review requires passing selected checks")
            now = datetime.now(UTC).isoformat()
            mapping = EvidenceMapping(
                id=f"evidence_{uuid4().hex}",
                version=version + 1,
                criterion_id=f"criterion:{goal_id}:{criterion_index}",
                criterion_index=criterion_index,
                criterion_text=_safe(context.criteria[criterion_index]),
                criterion_sha256=request.criterion_sha256,
                goal_run_id=goal_id,
                project_id=revision.project_id,
                node_id=revision.node_id,
                worker_job_id=revision.worker_job_id,
                revision_id=revision.id,
                revision_sha256=revision.sha256,
                context_sha256=context.context_sha256,
                conversation_revision=context.conversation_revision,
                file_ids=request.file_ids,
                check_ids=request.check_ids,
                files=[files[key] for key in request.file_ids],
                checks=selected_checks,
                review_status=request.review_status,
                public_explanation=(redact_dataset_text(request.public_explanation) or "")[:2_000],
                recorded_at=now,
                reviewed_at=now if request.review_status == "reviewed" else None,
            )
            await db.execute(
                """INSERT INTO project_requirement_evidence(id,goal_run_id,project_id,revision_id,
                criterion_index,version,criterion_text,mapping_json,goal_sha256,evidence_sha256,
                request_id,request_sha256,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    mapping.id,
                    goal_id,
                    mapping.project_id,
                    mapping.revision_id,
                    criterion_index,
                    mapping.version,
                    context.criteria[criterion_index],
                    mapping.model_dump_json(),
                    context.goal_sha256,
                    context.evidence_sha256,
                    request.request_id,
                    request_sha,
                    actor_id,
                    now,
                ),
            )
            await append_audit_event(
                db,
                "goal.requirement_evidence.recorded",
                {
                    "goal_run_id": goal_id,
                    "mapping_id": mapping.id,
                    "version": mapping.version,
                    "criterion_sha256": mapping.criterion_sha256,
                    "revision_id": mapping.revision_id,
                    "review_status": mapping.review_status,
                },
                trace_id=goal_id,
                actor_type="device",
                actor_id=actor_id,
                created_at=now,
            )
            result = await _view(db, context)
            await db.commit()
            return result
        except (EvidenceMappingConflict, EvidenceGoalNotFound):
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise ProjectGraphEvidenceError("stored requirement evidence is invalid") from exc
        finally:
            await db.rollback()
