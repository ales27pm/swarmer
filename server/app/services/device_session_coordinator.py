from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class DeviceSessionCoordinationError(RuntimeError):
    pass


class DeviceSessionCoordinator:
    """Serialize session cutover and WebSocket sends for one device.

    The lock is a local-filesystem advisory lock rather than an in-memory lock so
    independent control-plane processes using the same authoritative SQLite file
    observe the same ordering.  A process exit closes the descriptor and releases
    the lock.  Different devices use different lock files and do not block one
    another.
    """

    def __init__(self, db_path: Path, *, retry_seconds: float = 0.005) -> None:
        if retry_seconds <= 0:
            raise ValueError("device session lock retry interval must be positive")
        self._lock_root = db_path.with_name(f".{db_path.name}.device-session-locks")
        self._retry_seconds = retry_seconds

    @asynccontextmanager
    async def hold(self, device_id: str) -> AsyncIterator[None]:
        if not device_id:
            raise DeviceSessionCoordinationError("device session lock requires a device id")

        descriptor = self._open_lock(device_id)
        acquired = False
        try:
            while not acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    await asyncio.sleep(self._retry_seconds)
            yield
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _open_lock(self, device_id: str) -> int:
        self._ensure_private_lock_root()
        filename = hashlib.sha256(device_id.encode("utf-8"), usedforsecurity=False).hexdigest()
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._lock_root / filename, flags, 0o600)
        except OSError as exc:
            raise DeviceSessionCoordinationError("could not open device session lock") from exc

        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise DeviceSessionCoordinationError("device session lock is not private")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _ensure_private_lock_root(self) -> None:
        try:
            self._lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self._lock_root.lstat()
        except OSError as exc:
            raise DeviceSessionCoordinationError(
                "could not prepare device session lock directory"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise DeviceSessionCoordinationError("device session lock directory is not private")
