"""Durable project identity; callers own the write transaction and admission."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import aiosqlite

from app.services.audit_log import append_audit_event
from app.services.code_proposal import CODE_GENERATION_SKILL, validate_code_proposal_result
from app.services.project_contracts import ProjectResult, project_digest

MAX_IDENTITY_LINEAGE = 128


class ProjectIdentityConflict(RuntimeError):
    """Identity cannot be established without guessing or moving existing data."""


async def create_project_locked(db: aiosqlite.Connection, goal_id: str, now: str) -> str:
    """Create only for an admitted new goal, never as a read-side migration."""
    existing = await (
        await db.execute(
            "SELECT project_id FROM goal_project_links WHERE goal_run_id=?", (goal_id,)
        )
    ).fetchone()
    if existing is not None:
        raise ProjectIdentityConflict("new goal already has a project identity")
    if not await (await db.execute("SELECT 1 FROM goal_runs WHERE id=?", (goal_id,))).fetchone():
        raise ProjectIdentityConflict("goal not found")
    project_id = f"project_{uuid4().hex}"
    await db.execute("INSERT INTO coding_projects VALUES(?,?,?)", (project_id, now, now))
    await db.execute("INSERT INTO goal_project_links VALUES(?,?)", (goal_id, project_id))
    return project_id


async def _lineage_locked(
    db: aiosqlite.Connection, active_id: str, conversation_id: str
) -> list[aiosqlite.Row]:
    rows = list(
        await (
            await db.execute(
                """SELECT l.goal_run_id,l.parent_goal_id,g.id AS existing_goal,p.project_id,
                c.id AS existing_project
            FROM goal_conversation_links l LEFT JOIN goal_runs g ON g.id=l.goal_run_id
            LEFT JOIN goal_project_links p ON p.goal_run_id=l.goal_run_id
            LEFT JOIN coding_projects c ON c.id=p.project_id
            WHERE l.conversation_id=? LIMIT ?""",
                (conversation_id, MAX_IDENTITY_LINEAGE + 1),
            )
        ).fetchall()
    )
    if not rows or len(rows) > MAX_IDENTITY_LINEAGE:
        raise ProjectIdentityConflict("project lineage exceeds its reconciliation limit")
    by_id = {str(row["goal_run_id"]): row for row in rows}
    lineage: list[aiosqlite.Row] = []
    seen: set[str] = set()
    cursor: str | None = active_id
    while cursor is not None:
        row = by_id.get(cursor)
        if row is None or cursor in seen or row["existing_goal"] is None:
            raise ProjectIdentityConflict("project lineage is incomplete or cyclic")
        if row["project_id"] is not None and row["existing_project"] is None:
            raise ProjectIdentityConflict("project lineage references a missing identity")
        seen.add(cursor)
        lineage.append(row)
        cursor = row["parent_goal_id"]
    if len(seen) != len(rows):
        raise ProjectIdentityConflict("project conversation contains unrelated lineage members")
    return lineage


async def require_idle_lineage_locked(db: aiosqlite.Connection, goal_ids: list[str]) -> None:
    """Fail closed while any old-context consumer retains publication authority."""
    # The bounded IDs are bound values; only generated placeholder punctuation is SQL.
    marks = ",".join("?" for _ in goal_ids)
    for table in ("goal_model_calls", "goal_memory_queries", "project_memory_queries"):
        active = await (
            await db.execute(
                f"SELECT 1 FROM {table} WHERE goal_run_id IN ({marks}) AND status='started' LIMIT 1",  # nosec B608
                goal_ids,
            )
        ).fetchone()
        if active:
            raise ProjectIdentityConflict("project identity change requires no active work")
    if await (
        await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_context_compactions'"
        )
    ).fetchone():
        active = await (
            await db.execute(
                f"SELECT 1 FROM project_context_compactions WHERE goal_run_id IN ({marks}) AND status='started' LIMIT 1",  # nosec B608
                goal_ids,
            )
        ).fetchone()
        if active:
            raise ProjectIdentityConflict("project identity change requires no active work")
    # Resolve tasks in SQL rather than a second, potentially unbounded parameter list.
    for table, terminal in (
        ("agent_jobs", "'completed','failed','cancelled'"),
        ("tool_calls", "'completed','failed','cancelled','denied','expired'"),
        ("iphone_capability_requests", "'completed','failed','cancelled','denied','expired'"),
    ):
        active = await (
            await db.execute(
                f"""SELECT 1 FROM {table} WHERE status NOT IN ({terminal}) AND task_id IN (
            SELECT root_task_id FROM goal_runs WHERE id IN ({marks})
            UNION SELECT task_id FROM plan_nodes WHERE goal_run_id IN ({marks})
            UNION SELECT apply_task_id FROM goal_code_proposals WHERE goal_run_id IN ({marks})
            UNION SELECT apply_task_id FROM project_revisions WHERE goal_run_id IN ({marks})) LIMIT 1""",  # nosec B608
                goal_ids * 4,
            )
        ).fetchone()
        if active:
            raise ProjectIdentityConflict("project identity change requires no active work")


async def continuation_project_locked(
    db: aiosqlite.Connection, active_id: str, conversation_id: str, now: str, *, actor_id: str
) -> tuple[str, list[str]]:
    """Only an explicit accepted reply may reconcile a bounded legacy lineage."""
    lineage = await _lineage_locked(db, active_id, conversation_id)
    goal_ids = [str(row["goal_run_id"]) for row in lineage]
    identities = {str(row["project_id"]) for row in lineage if row["project_id"] is not None}
    if len(identities) > 1:
        raise ProjectIdentityConflict("project lineage contains conflicting identities")
    missing = [str(row["goal_run_id"]) for row in lineage if row["project_id"] is None]
    if missing:
        await require_idle_lineage_locked(db, goal_ids)
    needs_reconciliation = bool(missing)
    if identities:
        project_id = next(iter(identities))
    else:
        project_id = await create_project_locked(db, active_id, now)
        missing.remove(active_id)
    if needs_reconciliation:
        marks = ",".join("?" for _ in goal_ids)
        for table, owner in (
            ("project_revisions", "goal_run_id"),
            ("project_context_snapshots", "goal_run_id"),
            ("project_memory_items", "source_goal_id"),
            ("project_memory_queries", "goal_run_id"),
            ("goal_memory_queries", "goal_run_id"),
        ):
            conflict = await (
                await db.execute(
                    f"SELECT 1 FROM {table} WHERE {owner} IN ({marks}) AND project_id<>? LIMIT 1",  # nosec B608
                    [*goal_ids, project_id],
                )
            ).fetchone()
            if conflict:
                raise ProjectIdentityConflict("project lineage has conflicting recorded provenance")
        shared = await (
            await db.execute(
                "SELECT goal_run_id FROM goal_project_links WHERE project_id=? LIMIT ?",
                (project_id, MAX_IDENTITY_LINEAGE + 1),
            )
        ).fetchall()
        affected = list(set(goal_ids) | {str(row[0]) for row in shared})
        if len(affected) > MAX_IDENTITY_LINEAGE:
            raise ProjectIdentityConflict("project lineage exceeds its reconciliation limit")
        await require_idle_lineage_locked(db, affected)
    for member in missing:
        await db.execute("INSERT INTO goal_project_links VALUES(?,?)", (member, project_id))
    if any(row["project_id"] is None for row in lineage):
        await append_audit_event(
            db,
            "project.identity.reconciled",
            {"project_id": project_id, "goal_run_ids": goal_ids},
            actor_type="device",
            actor_id=actor_id,
            trace_id=active_id,
            created_at=now,
        )
    return project_id, goal_ids


async def seed_legacy_proposal_locked(
    db: aiosqlite.Connection, project_id: str, goal_ids: list[str], now: str
) -> None:
    """Retain an exact inert proposal on explicit continuation; never overwrite files."""
    if await (
        await db.execute(
            "SELECT 1 FROM project_revisions WHERE project_id=? LIMIT 1", (project_id,)
        )
    ).fetchone():
        return
    marks = ",".join("?" for _ in goal_ids)
    proposals = list(
        await (
            await db.execute(
                f"""SELECT p.*,n.goal_run_id AS node_goal,n.worker_job_id AS node_job,
        n.required_skill AS node_skill,j.task_id AS job_task,n.task_id AS node_task,
        j.required_skill AS job_skill,j.status AS job_status,j.result_json,t.source AS task_source
        FROM goal_code_proposals p LEFT JOIN plan_nodes n ON n.id=p.node_id
        LEFT JOIN agent_jobs j ON j.id=p.worker_job_id LEFT JOIN tasks t ON t.id=j.task_id
        WHERE p.goal_run_id IN ({marks}) ORDER BY p.created_at DESC,p.node_id LIMIT 129""",  # nosec B608
                goal_ids,
            )
        ).fetchall()
    )
    if not proposals:
        return
    if len(proposals) > MAX_IDENTITY_LINEAGE:
        raise ProjectIdentityConflict("project legacy proposals exceed reconciliation limit")
    await require_idle_lineage_locked(db, goal_ids)
    contents: set[str] = set()
    for row in proposals:
        if (
            row["node_goal"] != row["goal_run_id"]
            or row["node_job"] != row["worker_job_id"]
            or row["node_skill"] != CODE_GENERATION_SKILL
            or row["job_skill"] != CODE_GENERATION_SKILL
            or row["job_task"] != row["node_task"]
            or row["job_status"] != "completed"
            or row["task_source"] != f"goal:{row['goal_run_id']}"
        ):
            raise ProjectIdentityConflict("project legacy proposal provenance is invalid")
        try:
            result = validate_code_proposal_result(json.loads(row["result_json"]))
        except (ValueError, TypeError) as exc:
            raise ProjectIdentityConflict("project legacy proposal evidence is invalid") from exc
        content = str(row["content"])
        if (
            result["content"] != content
            or hashlib.sha256(content.encode()).hexdigest() != row["sha256"]
            or row["path"] != f"generated/{row['goal_run_id']}/{row['node_id']}/app.py"
        ):
            raise ProjectIdentityConflict("project legacy proposal content changed")
        contents.add(content)
    if len(contents) > 1:
        raise ProjectIdentityConflict(
            "project legacy proposals contain conflicting unversioned files"
        )
    legacy = proposals[0]
    seed = ProjectResult.model_validate(
        {
            "schema_version": "1.0",
            "action": "continue",
            "message": "Existing Python source retained for project continuation.",
            "plan": ["Extend the existing application and verify it with tests."],
            "files": [{"path": "app.py", "content": legacy["content"]}],
            "checks": [],
            "run_instructions": "",
            "runtime": "python",
            "base_revision_id": None,
            "base_sha256": None,
        }
    )
    await db.execute(
        """INSERT INTO project_revisions(id,project_id,goal_run_id,node_id,worker_job_id,
        revision,snapshot_json,sha256,created_at) VALUES(?,?,?,?,?,1,?,?,?)""",
        (
            f"revision_{uuid4().hex}",
            project_id,
            legacy["goal_run_id"],
            legacy["node_id"],
            legacy["worker_job_id"],
            seed.model_dump_json(),
            project_digest(seed.files),
            now,
        ),
    )


async def has_project_artifact_locked(db: aiosqlite.Connection, project_id: str) -> bool:
    row = await (
        await db.execute(
            """SELECT r.snapshot_json,r.sha256,p.project_id AS source_project,
        n.goal_run_id,n.worker_job_id,n.task_id,j.task_id,j.status,r.goal_run_id,r.worker_job_id
        FROM project_revisions r LEFT JOIN goal_project_links p ON p.goal_run_id=r.goal_run_id
        LEFT JOIN plan_nodes n ON n.id=r.node_id LEFT JOIN agent_jobs j ON j.id=r.worker_job_id
        WHERE r.project_id=? ORDER BY r.revision DESC LIMIT 1""",
            (project_id,),
        )
    ).fetchone()
    if (
        row is None
        or row[2] != project_id
        or row[3] != row[8]
        or row[4] != row[9]
        or row[5] != row[6]
        or row[7] != "completed"
    ):
        return False
    try:
        result = ProjectResult.model_validate_json(row[0])
        return bool(result.files) and project_digest(result.files) == row[1]
    except (ValueError, TypeError):
        return False
