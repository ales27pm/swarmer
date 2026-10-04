"""Revalidate included historical cards without replacing their selection."""

from __future__ import annotations

from typing import Any, Literal

import aiosqlite

from app.services.agent_capsule import validate_agent_capsule
from app.services.media_contracts import MEDIA_SKILLS
from app.services.worker_experiences import require_selected_worker_experiences_locked

EXPERIENCE_CONTEXT_CHANGED: Literal["worker_experience_context_changed"] = (
    "worker_experience_context_changed"
)
_TOP_LEVEL_CARRIERS = frozenset({"writing.draft", "code.generate_python", "code.build_project"})


async def require_worker_experience_context_locked(
    db: aiosqlite.Connection,
    payload: dict[str, Any],
    *,
    task_id: str,
    required_skill: str,
    task_statuses: tuple[str, ...],
    job_id: str | None = None,
) -> None:
    """Authenticate only included cards in the caller's write transaction.

    Empty/absent legacy selections remain optional. Neither a capsule's
    fingerprint nor caller-provided project text establishes its SQL scope.
    The immutable queued payload is reused at claim/result; no cards or
    mandatory instructions are removed, refreshed, or silently substituted.
    """
    if not db.in_transaction:
        raise ValueError(EXPERIENCE_CONTEXT_CHANGED)
    try:
        if required_skill in _TOP_LEVEL_CARRIERS:
            raw = payload.get("durable_context")
        elif required_skill in MEDIA_SKILLS:
            context = payload.get("context")
            raw = context.get("durable_context") if isinstance(context, dict) else None
        else:
            return
        if raw is None:
            return
        capsule = validate_agent_capsule(raw)
        items = capsule.get("experiences", {}).get("items", [])
        if not items:
            return
        cursor = await db.execute(
            """SELECT p.project_id,t.status,n.worker_job_id
            FROM tasks t
            JOIN plan_nodes n ON n.task_id=t.id AND n.required_skill=? AND n.node_type='worker'
            JOIN goal_runs g ON g.id=n.goal_run_id AND t.source='goal:' || g.id
            JOIN goal_project_links p ON p.goal_run_id=g.id
            WHERE t.id=? AND (n.worker_job_id IS NULL OR n.worker_job_id=?) LIMIT 2""",
            (required_skill, task_id, job_id),
        )
        cursor.row_factory = aiosqlite.Row
        rows = list(await cursor.fetchall())
        if len(rows) != 1 or rows[0]["status"] not in task_statuses:
            raise ValueError(EXPERIENCE_CONTEXT_CHANGED)
        if job_id is not None and rows[0]["worker_job_id"] is None:
            # GoalManager publishes this link after queue_job commits. Only
            # the unique active job for this task may use that short window.
            cursor = await db.execute(
                """SELECT id,required_skill FROM agent_jobs WHERE task_id=?
                AND status IN ('queued','claimed','running') LIMIT 2""",
                (task_id,),
            )
            cursor.row_factory = aiosqlite.Row
            jobs = list(await cursor.fetchall())
            if (
                len(jobs) != 1
                or jobs[0]["id"] != job_id
                or jobs[0]["required_skill"] != required_skill
            ):
                raise ValueError(EXPERIENCE_CONTEXT_CHANGED)
        await require_selected_worker_experiences_locked(
            db, project_id=str(rows[0]["project_id"]), items=items
        )
    except (TypeError, ValueError, KeyError, RecursionError) as exc:
        raise ValueError(EXPERIENCE_CONTEXT_CHANGED) from exc
