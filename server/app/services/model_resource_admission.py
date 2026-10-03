"""Shared local-model occupancy using the control plane's existing durable leases.

Callers must hold BEGIN IMMEDIATE and create their reservation/claim before
committing that same transaction. This predicate does not queue requests or
provide fairness by itself; it also never owns a second lease or charges an
attempt. Existing model-call and worker-job recovery remains authoritative.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

LOGGER = logging.getLogger(__name__)


class ExternalGPUAdmission:
    """Bridge a Studio flock to a durable SQLite generation reservation.

    The descriptor must survive COMMIT (or connection close on failure). It is
    deliberately not retained for inference: the committed worker/model lease
    then excludes Studio, which rechecks SQLite under its own flock. Nonempty
    lock contents fence a crashed Studio whose Docker renderer may still live.
    Only Studio's confirmed cleanup may erase that marker.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.descriptor: int | None = None

    def try_acquire(self) -> bool:
        if self.path is None or self.descriptor is not None:
            return True
        descriptor = None
        try:
            path = self.path
            if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
                return False
            descriptor = os.open(
                path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                return False
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.pread(descriptor, 1, 0):
                return False
            self.descriptor, descriptor = descriptor, None
            return True
        except BlockingIOError:
            return False
        except OSError as exc:
            LOGGER.warning("External GPU admission unavailable: %s", type(exc).__name__)
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


@asynccontextmanager
async def model_admission_connection(
    db_path: Path, gpu_lock_path: Path | None
) -> AsyncIterator[tuple[aiosqlite.Connection, ExternalGPUAdmission]]:
    """Never release the host slot before SQLite has finished the transaction."""
    admission = ExternalGPUAdmission(gpu_lock_path)
    db = await aiosqlite.connect(db_path)
    try:
        yield db, admission
    finally:
        # SQLite operations keep running on aiosqlite's thread after their
        # awaiting coroutine is cancelled. Drain them and close in a separate
        # task; repeated caller cancellation must not interrupt that cleanup.
        close_task = asyncio.create_task(db.close())
        cancellation: asyncio.CancelledError | None = None
        try:
            while not close_task.done():
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError as exc:
                    cancellation = exc
            close_task.result()
        finally:
            admission.close()
        if cancellation is not None:
            raise cancellation


LOCAL_MODEL_WORKER_SKILLS = frozenset(
    {"writing.draft", "code.generate_python", "code.build_project", "image.generate"}
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
                WHERE required_skill IN ('writing.draft','code.generate_python','code.build_project','image.generate')
                  AND status IN ('claimed','running') AND lease_expires_at>?
            )
            """,
            (timestamp, timestamp),
        )
    ).fetchone()
    return bool(row and row[0])
