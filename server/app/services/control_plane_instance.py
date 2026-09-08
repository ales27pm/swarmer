from __future__ import annotations

import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

_INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HOSTNAME_UNSAFE_PATTERN = re.compile(r"[^a-z0-9]+")


class ControlPlaneInstanceConflict(RuntimeError):
    """Raised when a boot-scoped instance identity cannot advance safely."""


@dataclass(frozen=True, slots=True)
class ControlPlaneInstance:
    instance_id: str
    hostname_label: str
    version: str
    started_at: str
    heartbeat_at: str
    stopped_at: str | None

    def public_status(self) -> dict[str, str | None]:
        return {
            "instance_id": self.instance_id,
            "hostname_label": self.hostname_label,
            "version": self.version,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "stopped_at": self.stopped_at,
        }


def safe_hostname_label(hostname: str) -> str:
    """Return a bounded label, never a raw or path-like hostname."""

    normalized = _HOSTNAME_UNSAFE_PATTERN.sub("-", hostname.casefold()).strip("-")
    return normalized[:48].rstrip("-") or "host"


class ControlPlaneInstanceService:
    """Persist a random identity for one control-plane boot/session."""

    def __init__(
        self,
        db_path: Path,
        *,
        version: str,
        instance_id: str | None = None,
        hostname: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_version = version.strip()
        if (
            not normalized_version
            or len(normalized_version) > 64
            or any(ord(character) < 32 for character in normalized_version)
        ):
            raise ValueError("control-plane version must be a short printable value")
        generated_id = instance_id or f"cp_{uuid4().hex}"
        if not _INSTANCE_ID_PATTERN.fullmatch(generated_id):
            raise ValueError("control-plane instance ID has an invalid format")
        self.db_path = db_path
        self.version = normalized_version
        self.instance_id = generated_id
        self.hostname_label = safe_hostname_label(hostname or socket.gethostname())
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            raise RuntimeError("control-plane instance clock must be timezone-aware")
        return value.astimezone(UTC).isoformat()

    async def start(self) -> ControlPlaneInstance:
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await self._fetch_locked(db, self.instance_id)
            if row is None:
                await db.execute(
                    """
                    INSERT INTO control_plane_instances(
                        instance_id,hostname_label,version,started_at,heartbeat_at,stopped_at
                    ) VALUES(?,?,?,?,?,NULL)
                    """,
                    (self.instance_id, self.hostname_label, self.version, now, now),
                )
            else:
                if row["stopped_at"] is not None:
                    raise ControlPlaneInstanceConflict("stopped instance identity cannot restart")
                if (
                    str(row["hostname_label"]) != self.hostname_label
                    or str(row["version"]) != self.version
                ):
                    raise ControlPlaneInstanceConflict("instance identity metadata does not match")
                await db.execute(
                    """
                    UPDATE control_plane_instances SET heartbeat_at=?
                    WHERE instance_id=? AND stopped_at IS NULL
                    """,
                    (now, self.instance_id),
                )
            await db.commit()
            current = await self._fetch_locked(db, self.instance_id)
        if current is None:  # pragma: no cover - protected by the same transaction
            raise RuntimeError("control-plane instance registration was not persisted")
        return self._from_row(current)

    async def heartbeat(self) -> ControlPlaneInstance:
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE control_plane_instances SET heartbeat_at=?
                WHERE instance_id=? AND stopped_at IS NULL
                """,
                (now, self.instance_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneInstanceConflict("instance is missing or already stopped")
            await db.commit()
            current = await self._fetch_locked(db, self.instance_id)
        if current is None:  # pragma: no cover - protected by the same transaction
            raise RuntimeError("control-plane instance heartbeat was not persisted")
        return self._from_row(current)

    async def stop(self) -> ControlPlaneInstance:
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await self._fetch_locked(db, self.instance_id)
            if row is None:
                raise ControlPlaneInstanceConflict("instance is not registered")
            if row["stopped_at"] is None:
                await db.execute(
                    """
                    UPDATE control_plane_instances SET heartbeat_at=?,stopped_at=?
                    WHERE instance_id=? AND stopped_at IS NULL
                    """,
                    (now, now, self.instance_id),
                )
            await db.commit()
            current = await self._fetch_locked(db, self.instance_id)
        if current is None:  # pragma: no cover - protected by the same transaction
            raise RuntimeError("control-plane instance stop was not persisted")
        return self._from_row(current)

    async def get(self, instance_id: str | None = None) -> ControlPlaneInstance | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._fetch_locked(db, instance_id or self.instance_id)
        return self._from_row(row) if row is not None else None

    @staticmethod
    async def _fetch_locked(db: aiosqlite.Connection, instance_id: str) -> aiosqlite.Row | None:
        return await (
            await db.execute(
                """
                SELECT instance_id,hostname_label,version,started_at,heartbeat_at,stopped_at
                FROM control_plane_instances WHERE instance_id=?
                """,
                (instance_id,),
            )
        ).fetchone()

    @staticmethod
    def _from_row(row: aiosqlite.Row) -> ControlPlaneInstance:
        return ControlPlaneInstance(
            instance_id=str(row["instance_id"]),
            hostname_label=str(row["hostname_label"]),
            version=str(row["version"]),
            started_at=str(row["started_at"]),
            heartbeat_at=str(row["heartbeat_at"]),
            stopped_at=str(row["stopped_at"]) if row["stopped_at"] is not None else None,
        )
