"""Shared local-model occupancy using the control plane's existing durable leases.

Callers must hold BEGIN IMMEDIATE and create their reservation/claim before
committing that same transaction. This predicate does not queue requests or
provide fairness by itself; it also never owns a second lease or charges an
attempt. Existing model-call and worker-job recovery remains authoritative.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

LOCAL_MODEL_WORKER_SKILLS = frozenset(
    {"writing.draft", "code.generate_python", "code.build_project"}
)


def requires_local_model_resource(required_skill: str) -> bool:
    return required_skill in LOCAL_MODEL_WORKER_SKILLS


async def active_local_model_work_locked(db: aiosqlite.Connection, *, now: datetime) -> bool:
    """Observe existing local generation; sample ``now`` after acquiring the lock.

    Expired/missing leases do not hold admission indefinitely after a crash.
    This does not cancel the old execution: its existing transport deadline and
    lease checks must fence it, as for the current per-goal/worker lease protocol.
    Research, file operations and non-Ubuntu model providers do not hold this
    resource. Embedding admission is outside this generation-only predicate.
    """
    if not db.in_transaction:
        raise RuntimeError("model resource admission requires a transaction")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("model resource admission clock must be timezone-aware")
    timestamp = now.astimezone(UTC).isoformat()
    row = await (
        await db.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM goal_model_calls
                WHERE provider_source='ubuntu_local' AND status='started'
                  AND lease_expires_at>?
                UNION ALL
                SELECT 1 FROM agent_jobs
                WHERE required_skill IN ('writing.draft','code.generate_python','code.build_project')
                  AND status IN ('claimed','running') AND lease_expires_at>?
            )
            """,
            (timestamp, timestamp),
        )
    ).fetchone()
    return bool(row and row[0])
