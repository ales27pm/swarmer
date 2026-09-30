"""Single-host process locks for website jobs; SQLite remains the durable authority.

Never unlink a lock file: a second inode would allow two simultaneous owners.
These locks require local storage with flock support, not a distributed filesystem.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import stat
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path


class FileLease:
    def __init__(self, fd: int):
        self.fd: int | None = fd

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def try_lease(path: Path, *, shared: bool = False) -> FileLease | None:
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("invalid_website_lock")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return FileLease(fd)
    except BaseException:
        os.close(fd)
        raise


class WebsiteLocks:
    def __init__(self, root: Path):
        self.root = root

    def project(self, project_id: str) -> FileLease | None:
        key = hashlib.sha256(project_id.encode()).hexdigest()
        return try_lease(self.root / f".project-{key}.lock")

    @asynccontextmanager
    async def execution(self) -> AsyncIterator[None]:
        # Queued operations hold their project lock. Admission is bounded by
        # one SQLite transaction across all API processes; only two execute.
        while True:
            for index in range(2):
                lease = try_lease(self.root / f".execution-{index}.lock")
                if lease is not None:
                    try:
                        yield
                    finally:
                        lease.close()
                    return
            await asyncio.sleep(0.05)


async def joined_thread[T, **P](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Cancellation must not release a job's locks while its thread still writes."""
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - consume result; original cancellation wins
                break
        if not task.cancelled():
            task.exception()
        raise
