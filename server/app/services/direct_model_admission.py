"""Host admission for local generation outside the goal lease protocol."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.services.model_resource_admission import (
    active_local_model_work_locked,
    model_admission_connection,
)


class LocalGPUUnavailable(RuntimeError):
    """Retryable contention; no model request or budget has been consumed."""


@asynccontextmanager
async def direct_model_admission(db_path: Path, gpu_lock_path: Path | None) -> AsyncIterator[None]:
    if gpu_lock_path is None:
        yield
        return
    # Same ordering as goal/worker admission: SQLite writer, then host flock.
    # No durable goal lease exists for chat or translation, so retain the flock
    # for the request. Close SQLite before inference so ordinary writes continue.
    async with model_admission_connection(db_path, gpu_lock_path) as (db, admission):
        await db.execute("BEGIN IMMEDIATE")
        if not admission.try_acquire() or await active_local_model_work_locked(
            db, now=datetime.now(UTC)
        ):
            raise LocalGPUUnavailable("local GPU is occupied or unavailable")
        await db.rollback()
        await db.close()
        yield
