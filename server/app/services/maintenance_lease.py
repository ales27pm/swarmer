from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

_LEASE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,99}$")


class MaintenanceLeaseConflict(RuntimeError):
    """Raised when a caller attempts an invalid or stale lease operation."""


@dataclass(frozen=True, slots=True)
class MaintenanceLease:
    name: str
    owner_instance_id: str
    generation: int
    acquired_at: str
    renewed_at: str
    expires_at: str


class MaintenanceLeaseService:
    """Coordinate fenced singleton maintenance using authoritative SQLite state."""

    def __init__(
        self,
        db_path: Path,
        *,
        lease_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("maintenance lease duration must be positive")
        self.db_path = db_path
        self.lease_seconds = lease_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    def _time_window(self) -> tuple[str, str]:
        value = self.clock()
        if value.tzinfo is None:
            raise RuntimeError("maintenance lease clock must be timezone-aware")
        now = value.astimezone(UTC)
        return now.isoformat(), (now + timedelta(seconds=self.lease_seconds)).isoformat()

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _LEASE_NAME_PATTERN.fullmatch(name):
            raise ValueError("maintenance lease name has an invalid format")

    async def acquire(self, name: str, owner_instance_id: str) -> MaintenanceLease | None:
        self._validate_name(name)
        now, expires_at = self._time_window()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            await self._require_active_instance_locked(db, owner_instance_id)
            current = await self._fetch_locked(db, name)
            if current is None:
                await db.execute(
                    """
                    INSERT INTO maintenance_leases(
                        name,owner_instance_id,generation,acquired_at,renewed_at,expires_at
                    ) VALUES(?,?,1,?,?,?)
                    """,
                    (name, owner_instance_id, now, now, expires_at),
                )
            elif (
                str(current["owner_instance_id"]) == owner_instance_id
                and str(current["expires_at"]) > now
            ):
                cursor = await db.execute(
                    """
                    UPDATE maintenance_leases SET renewed_at=?,expires_at=?
                    WHERE name=? AND owner_instance_id=? AND generation=? AND expires_at>?
                    """,
                    (
                        now,
                        expires_at,
                        name,
                        owner_instance_id,
                        int(current["generation"]),
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise MaintenanceLeaseConflict("maintenance lease changed during renewal")
            elif str(current["expires_at"]) <= now:
                generation = int(current["generation"]) + 1
                cursor = await db.execute(
                    """
                    UPDATE maintenance_leases
                    SET owner_instance_id=?,generation=?,acquired_at=?,renewed_at=?,expires_at=?
                    WHERE name=? AND generation=? AND expires_at=?
                    """,
                    (
                        owner_instance_id,
                        generation,
                        now,
                        now,
                        expires_at,
                        name,
                        int(current["generation"]),
                        str(current["expires_at"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise MaintenanceLeaseConflict("maintenance lease changed during takeover")
            else:
                await db.commit()
                return None
            await db.commit()
            acquired = await self._fetch_locked(db, name)
        if acquired is None:  # pragma: no cover - protected by the same transaction
            raise RuntimeError("maintenance lease acquisition was not persisted")
        return self._from_row(acquired)

    async def renew(
        self, name: str, owner_instance_id: str, generation: int
    ) -> MaintenanceLease | None:
        self._validate_name(name)
        now, expires_at = self._time_window()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE maintenance_leases SET renewed_at=?,expires_at=?
                WHERE name=? AND owner_instance_id=? AND generation=? AND expires_at>?
                  AND EXISTS(
                      SELECT 1 FROM control_plane_instances
                      WHERE instance_id=? AND stopped_at IS NULL
                  )
                """,
                (
                    now,
                    expires_at,
                    name,
                    owner_instance_id,
                    generation,
                    now,
                    owner_instance_id,
                ),
            )
            await db.commit()
            if cursor.rowcount != 1:
                return None
            renewed = await self._fetch_locked(db, name)
        return self._from_row(renewed) if renewed is not None else None

    async def release(self, name: str, owner_instance_id: str, generation: int) -> bool:
        self._validate_name(name)
        now, _ = self._time_window()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE maintenance_leases SET renewed_at=?,expires_at=?
                WHERE name=? AND owner_instance_id=? AND generation=? AND expires_at>?
                """,
                (now, now, name, owner_instance_id, generation, now),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def is_current(self, name: str, owner_instance_id: str, generation: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await self.require_current_locked(db, name, owner_instance_id, generation)
            except MaintenanceLeaseConflict:
                return False
        return True

    async def assert_current(self, name: str, owner_instance_id: str, generation: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.require_current_locked(db, name, owner_instance_id, generation)
            await db.commit()

    async def require_current_locked(
        self,
        db: aiosqlite.Connection,
        name: str,
        owner_instance_id: str,
        generation: int,
    ) -> None:
        """Fence a maintenance mutation inside the caller's SQLite transaction."""

        self._validate_name(name)
        now, _ = self._time_window()
        row = await (
            await db.execute(
                """
                SELECT 1 FROM maintenance_leases AS lease
                JOIN control_plane_instances AS instance
                  ON instance.instance_id=lease.owner_instance_id
                WHERE lease.name=? AND lease.owner_instance_id=? AND lease.generation=?
                  AND lease.expires_at>? AND instance.stopped_at IS NULL
                """,
                (name, owner_instance_id, generation, now),
            )
        ).fetchone()
        if row is None:
            raise MaintenanceLeaseConflict("maintenance lease is stale or expired")

    async def get(self, name: str) -> MaintenanceLease | None:
        self._validate_name(name)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._fetch_locked(db, name)
        return self._from_row(row) if row is not None else None

    async def current_owners(self) -> dict[str, str]:
        """Return only live lease labels and non-secret boot-scoped owner IDs."""

        now, _ = self._time_window()
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT lease.name,lease.owner_instance_id
                    FROM maintenance_leases AS lease
                    JOIN control_plane_instances AS instance
                      ON instance.instance_id=lease.owner_instance_id
                    WHERE lease.expires_at>? AND instance.stopped_at IS NULL
                    ORDER BY lease.name ASC
                    """,
                    (now,),
                )
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    @staticmethod
    async def _require_active_instance_locked(
        db: aiosqlite.Connection, owner_instance_id: str
    ) -> None:
        row = await (
            await db.execute(
                """
                SELECT 1 FROM control_plane_instances
                WHERE instance_id=? AND stopped_at IS NULL
                """,
                (owner_instance_id,),
            )
        ).fetchone()
        if row is None:
            raise MaintenanceLeaseConflict("maintenance owner is not an active instance")

    @staticmethod
    async def _fetch_locked(db: aiosqlite.Connection, name: str) -> aiosqlite.Row | None:
        return await (
            await db.execute(
                """
                SELECT name,owner_instance_id,generation,acquired_at,renewed_at,expires_at
                FROM maintenance_leases WHERE name=?
                """,
                (name,),
            )
        ).fetchone()

    @staticmethod
    def _from_row(row: aiosqlite.Row) -> MaintenanceLease:
        return MaintenanceLease(
            name=str(row["name"]),
            owner_instance_id=str(row["owner_instance_id"]),
            generation=int(row["generation"]),
            acquired_at=str(row["acquired_at"]),
            renewed_at=str(row["renewed_at"]),
            expires_at=str(row["expires_at"]),
        )
