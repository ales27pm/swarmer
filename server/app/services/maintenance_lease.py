from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

_LEASE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,99}$")


class MaintenanceLeaseConflict(RuntimeError):
    """Raised when a caller attempts an invalid or stale lease operation."""


class MaintenanceLeaseLost(MaintenanceLeaseConflict):
    """Raised when a running singleton operation loses its fenced lease."""


@dataclass(frozen=True, slots=True)
class MaintenanceLease:
    name: str
    owner_instance_id: str
    generation: int
    acquired_at: str
    renewed_at: str
    expires_at: str


_ResultT = TypeVar("_ResultT")


class MaintenanceLeaseGuard:
    """Lease proof passed to one trusted maintenance operation.

    The guard deliberately does not make an initial check reusable. Callers must
    fence every authoritative mutation by invoking ``require_current_locked``
    inside that mutation's SQLite transaction, or use ``run_locked``.
    """

    def __init__(
        self,
        service: MaintenanceLeaseService,
        lease: MaintenanceLease,
    ) -> None:
        self._service = service
        self._lease = lease
        self._lost_reason: str | None = None
        self._lost = asyncio.Event()

    @property
    def name(self) -> str:
        return self._lease.name

    @property
    def owner_instance_id(self) -> str:
        return self._lease.owner_instance_id

    @property
    def generation(self) -> int:
        return self._lease.generation

    @property
    def expires_at(self) -> str:
        return self._lease.expires_at

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    @property
    def mutation_batch_budget_seconds(self) -> float:
        """Maximum target duration for one authoritative write batch.

        Maintenance code still verifies the durable expiry immediately before
        commit. This shorter target prevents a large result set from holding
        SQLite's writer lock until the renewal deadline.
        """

        return min(2.0, self._service.lease_seconds * 0.25)

    async def wait_lost(self) -> None:
        await self._lost.wait()

    def _mark_lost(self, reason: str) -> None:
        if not self._lost.is_set():
            self._lost_reason = reason
            self._lost.set()

    def _replace_lease(self, lease: MaintenanceLease) -> None:
        if (
            lease.name != self.name
            or lease.owner_instance_id != self.owner_instance_id
            or lease.generation != self.generation
        ):
            self._mark_lost("maintenance lease renewal changed its identity")
            raise MaintenanceLeaseLost(self._lost_reason)
        self._lease = lease

    def raise_if_lost(self) -> None:
        if self._lost.is_set():
            raise MaintenanceLeaseLost(self._lost_reason or "maintenance lease was lost")

    async def renew_now(self) -> None:
        """Renew at a batch boundary after the previous transaction released its lock."""

        self.raise_if_lost()
        try:
            renewed = await self._service.renew(
                self.name,
                self.owner_instance_id,
                self.generation,
            )
        except Exception as exc:
            self._mark_lost("maintenance lease renewal failed")
            raise MaintenanceLeaseLost("maintenance lease renewal failed") from exc
        if renewed is None:
            self._mark_lost("maintenance lease is stale or expired")
            raise MaintenanceLeaseLost("maintenance lease is stale or expired")
        self._replace_lease(renewed)

    async def require_current_locked(self, db: aiosqlite.Connection) -> None:
        """Fence the caller's current SQLite transaction with this generation."""

        self.raise_if_lost()
        try:
            await self._service.require_current_locked(
                db,
                self.name,
                self.owner_instance_id,
                self.generation,
            )
        except MaintenanceLeaseConflict as exc:
            self._mark_lost("maintenance lease is stale or expired")
            raise MaintenanceLeaseLost(self._lost_reason) from exc

    async def run_locked(
        self,
        mutation: Callable[[aiosqlite.Connection], Awaitable[_ResultT]],
    ) -> _ResultT:
        """Run one fenced authoritative mutation in its own transaction."""

        self.raise_if_lost()
        async with aiosqlite.connect(self._service.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self.require_current_locked(db)
                result = await mutation(db)
                # A renewal cannot acquire SQLite's write lock while this
                # transaction is open. Re-check the authoritative deadline
                # before commit so a mutation that itself exceeded the TTL is
                # rolled back instead of being legitimized by an earlier check.
                await self.require_current_locked(db)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return result


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
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            # Timestamp only after acquiring SQLite's writer lock. A timestamp
            # captured while waiting behind another writer can create a lease
            # that is already near expiry when it is finally persisted.
            now, expires_at = self._time_window()
            await self._require_active_instance_locked(db, owner_instance_id)
            current = await self._fetch_locked(db, name)
            if current is None:
                acquired_generation = 1
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
                acquired_generation = int(current["generation"])
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
                acquired_generation = generation
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
            acquired = await self._fetch_exact_locked(
                db,
                name,
                owner_instance_id,
                acquired_generation,
            )
            if acquired is None:
                await db.rollback()
                raise MaintenanceLeaseConflict(
                    "maintenance lease acquisition did not preserve its owner and generation"
                )
            await db.commit()
        return self._from_row(acquired)

    async def renew(
        self, name: str, owner_instance_id: str, generation: int
    ) -> MaintenanceLease | None:
        self._validate_name(name)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now, expires_at = self._time_window()
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
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            now, _ = self._time_window()
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
    async def _fetch_exact_locked(
        db: aiosqlite.Connection,
        name: str,
        owner_instance_id: str,
        generation: int,
    ) -> aiosqlite.Row | None:
        return await (
            await db.execute(
                """
                SELECT name,owner_instance_id,generation,acquired_at,renewed_at,expires_at
                FROM maintenance_leases
                WHERE name=? AND owner_instance_id=? AND generation=?
                """,
                (name, owner_instance_id, generation),
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


class MaintenanceLeaseRunner:
    """Acquire, renew, fence, and cleanly release one maintenance operation."""

    def __init__(
        self,
        leases: MaintenanceLeaseService,
        *,
        owner_instance_id: str,
        renewal_interval_seconds: float | None = None,
    ) -> None:
        interval = (
            renewal_interval_seconds
            if renewal_interval_seconds is not None
            else leases.lease_seconds * 0.4
        )
        if interval <= 0 or interval >= leases.lease_seconds / 2:
            raise ValueError("maintenance renewal interval must be before half the lease TTL")
        if not owner_instance_id:
            raise ValueError("maintenance lease runner owner is required")
        self.leases = leases
        self.owner_instance_id = owner_instance_id
        self.renewal_interval_seconds = interval
        self.renewal_failure_count = 0
        self._closed = False
        self._runs: set[asyncio.Task[Any]] = set()
        self._active_names: set[str] = set()
        self._state_lock = asyncio.Lock()

    @property
    def metrics(self) -> dict[str, int]:
        """Return process-local liveness telemetry, never authorization state."""

        return {"maintenance_lease_renewal_failures": self.renewal_failure_count}

    async def run(
        self,
        name: str,
        operation: Callable[[MaintenanceLeaseGuard], Awaitable[_ResultT]],
    ) -> _ResultT | None:
        """Run an operation while its lease remains current.

        ``None`` means another instance currently owns the named lease. A lease
        lost after work starts is different: the operation is cancelled and a
        ``MaintenanceLeaseLost`` exception is raised.
        """

        current_task = asyncio.current_task()
        if current_task is None:  # pragma: no cover - run always has an asyncio task
            raise RuntimeError("maintenance lease runner requires an asyncio task")
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("maintenance lease runner is closed")
            if name in self._active_names:
                raise RuntimeError("maintenance lease runner already owns this operation")
            self._active_names.add(name)
            self._runs.add(current_task)
        lease: MaintenanceLease | None = None
        guard: MaintenanceLeaseGuard | None = None
        operation_task: asyncio.Task[_ResultT] | None = None
        renewal_task: asyncio.Task[None] | None = None
        acquisition_task = asyncio.create_task(
            self._acquire_and_run_operation(name, operation),
            name=f"maintenance-acquire:{name}",
        )
        try:
            acquired = await acquisition_task
            if acquired is None:
                return None
            lease, guard, operation_task = acquired
            renewal_task = asyncio.create_task(
                self._renew_until_cancelled(guard),
                name=f"maintenance-renewal:{name}:{lease.generation}",
            )
            done, _ = await asyncio.wait(
                {operation_task, renewal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_task in done:
                if renewal_task.cancelled():
                    raise asyncio.CancelledError
                renewal_error = renewal_task.exception()
                if renewal_error is not None:
                    guard._mark_lost("maintenance lease renewal failed")
                    operation_task.cancel()
                    await asyncio.gather(operation_task, return_exceptions=True)
                    if isinstance(renewal_error, MaintenanceLeaseLost):
                        raise renewal_error
                    raise MaintenanceLeaseLost(
                        "maintenance lease renewal failed"
                    ) from renewal_error
                raise RuntimeError("maintenance lease renewal stopped unexpectedly")

            renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await renewal_task
            return await operation_task
        except asyncio.CancelledError:
            if guard is not None:
                guard._mark_lost("maintenance operation was cancelled")
            if operation_task is not None:
                operation_task.cancel()
            if renewal_task is not None:
                renewal_task.cancel()
            acquisition_task.cancel()
            await asyncio.gather(
                acquisition_task,
                *(task for task in (operation_task, renewal_task) if task is not None),
                return_exceptions=True,
            )
            raise
        finally:
            acquisition_task.cancel()
            if operation_task is not None:
                operation_task.cancel()
            if renewal_task is not None:
                renewal_task.cancel()
            await asyncio.gather(
                acquisition_task,
                *(task for task in (operation_task, renewal_task) if task is not None),
                return_exceptions=True,
            )
            try:
                if guard is not None:
                    await self._release(guard)
            finally:
                # Release is best-effort durable cleanup and may itself fail
                # (for example, transient SQLite I/O). Never let that strand
                # process-local ownership metadata and block all later cycles.
                async with self._state_lock:
                    self._runs.discard(current_task)
                    self._active_names.discard(name)

    async def close(self) -> None:
        """Cancel active operations and wait until their lease cleanup finishes."""

        current = asyncio.current_task()
        async with self._state_lock:
            self._closed = True
            active = [task for task in self._runs if task is not current and not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    async def aclose(self) -> None:
        await self.close()

    async def _acquire_and_run_operation(
        self,
        name: str,
        operation: Callable[[MaintenanceLeaseGuard], Awaitable[_ResultT]],
    ) -> (
        tuple[
            MaintenanceLease,
            MaintenanceLeaseGuard,
            asyncio.Task[_ResultT],
        ]
        | None
    ):
        """Acquire before starting the callback, while remaining cancellable by close()."""

        lease = await self.leases.acquire(name, self.owner_instance_id)
        if lease is None:
            return None
        if self._closed:
            await self.leases.release(name, self.owner_instance_id, lease.generation)
            raise RuntimeError("maintenance lease runner is closed")
        guard = MaintenanceLeaseGuard(self.leases, lease)
        operation_task: asyncio.Task[_ResultT] = asyncio.create_task(
            self._invoke_operation(operation, guard),
            name=f"maintenance-callback:{name}:{lease.generation}",
        )
        return lease, guard, operation_task

    @staticmethod
    async def _invoke_operation(
        operation: Callable[[MaintenanceLeaseGuard], Awaitable[_ResultT]],
        guard: MaintenanceLeaseGuard,
    ) -> _ResultT:
        return await operation(guard)

    async def _renew_until_cancelled(self, guard: MaintenanceLeaseGuard) -> None:
        while True:
            await asyncio.sleep(self.renewal_interval_seconds)
            try:
                renewed = await self.leases.renew(
                    guard.name,
                    guard.owner_instance_id,
                    guard.generation,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.renewal_failure_count += 1
                guard._mark_lost("maintenance lease renewal failed")
                raise MaintenanceLeaseLost("maintenance lease renewal failed") from exc
            if renewed is None:
                self.renewal_failure_count += 1
                guard._mark_lost("maintenance lease is stale or expired")
                raise MaintenanceLeaseLost("maintenance lease is stale or expired")
            guard._replace_lease(renewed)

    async def _release(self, guard: MaintenanceLeaseGuard) -> None:
        release_task = asyncio.create_task(
            self.leases.release(
                guard.name,
                guard.owner_instance_id,
                guard.generation,
            ),
            name=f"maintenance-release:{guard.name}:{guard.generation}",
        )
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            # A second cancellation must not strand a live singleton lease.
            with suppress(asyncio.CancelledError):
                await release_task
            raise
