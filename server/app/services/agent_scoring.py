from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from app.services.maintenance_lease import MaintenanceLeaseGuard, MaintenanceLeaseService


@dataclass(frozen=True, slots=True)
class AgentScoreSnapshot:
    agent_id: str
    completed_jobs: int
    failed_jobs: int
    terminal_jobs: int
    lease_expiry_count: int
    observed_outcomes: int
    completion_rate: float
    failure_rate: float
    timeout_rate: float
    feedback_count: int
    feedback_average: float | None
    average_latency_seconds: float | None
    composite_score: float
    formula_version: str
    rebuilt_at: str


class AgentScoringService:
    """Build transparent worker-quality projections from server observations.

    The composite is normalized to ``[0, 1]`` and deliberately excludes
    self-reported worker metadata and model output:

    ``0.60 * completion + 0.20 * (1 - timeout) + 0.20 * feedback``

    Missing completion, timeout, or feedback evidence contributes a neutral
    ``0.5`` for that component. Feedback is normalized from ``[0, 5]``. A
    timeout observation is a hash-chained ``agent.job.lease_expired`` audit
    event whose server-authored payload contains ``agent_id``. Cancelled jobs
    are excluded from completion/failure rates because cancellation does not
    necessarily measure worker quality. Latency is diagnostic only and does
    not influence the composite until comparable job classes exist.
    """

    FORMULA_VERSION = "server-observed-v1"
    READ_BATCH_SIZE = 256

    def __init__(
        self,
        db_path: Path,
        *,
        maintenance_leases: MaintenanceLeaseService | None = None,
        owner_instance_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self.maintenance_leases = maintenance_leases
        self.owner_instance_id = owner_instance_id
        self.clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        """Create the additive, disposable score projection table."""

        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_score_snapshots (
                    agent_id TEXT PRIMARY KEY,
                    completed_jobs INTEGER NOT NULL CHECK(completed_jobs >= 0),
                    failed_jobs INTEGER NOT NULL CHECK(failed_jobs >= 0),
                    terminal_jobs INTEGER NOT NULL CHECK(terminal_jobs >= 0),
                    lease_expiry_count INTEGER NOT NULL CHECK(lease_expiry_count >= 0),
                    observed_outcomes INTEGER NOT NULL CHECK(observed_outcomes >= 0),
                    completion_rate REAL NOT NULL,
                    failure_rate REAL NOT NULL,
                    timeout_rate REAL NOT NULL,
                    feedback_count INTEGER NOT NULL CHECK(feedback_count >= 0),
                    feedback_average REAL,
                    average_latency_seconds REAL,
                    composite_score REAL NOT NULL,
                    formula_version TEXT NOT NULL,
                    rebuilt_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_score_snapshots_rank
                    ON agent_score_snapshots(composite_score DESC,agent_id ASC);
                """
            )
            await db.commit()

    async def rebuild(
        self,
        *,
        maintenance_generation: int | None = None,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> list[AgentScoreSnapshot]:
        """Atomically replace the rebuildable projection from authoritative rows."""

        effective_generation = (
            maintenance_guard.generation
            if maintenance_guard is not None
            else maintenance_generation
        )
        if (
            maintenance_guard is not None
            and maintenance_generation is not None
            and maintenance_guard.generation != maintenance_generation
        ):
            raise RuntimeError("agent scoring received mismatched maintenance fencing")
        if self.maintenance_leases is not None and effective_generation is None:
            if self.owner_instance_id is None:
                raise RuntimeError("agent scoring maintenance owner is not configured")
            lease = await self.maintenance_leases.acquire(
                "feedback-maintenance", self.owner_instance_id
            )
            if lease is None:
                return await self.list_ranked()
            effective_generation = lease.generation
        if maintenance_guard is not None:
            maintenance_guard.raise_if_lost()
        rebuilt_at = self._now()
        snapshots = await self._read_snapshots(
            rebuilt_at,
            maintenance_guard=maintenance_guard,
        )
        snapshot_values: list[tuple[object, ...]] = []
        for index, snapshot in enumerate(snapshots, start=1):
            snapshot_values.append(self._snapshot_values(snapshot))
            if index % self.READ_BATCH_SIZE == 0:
                await self._yield_read_batch(maintenance_guard)
        if maintenance_guard is not None:
            maintenance_guard.raise_if_lost()

        # Only the disposable projection replacement takes SQLite's write
        # lock. Authoritative history scanning and score calculation happen in
        # a read transaction above, where WAL writers (including the lease
        # runner's renewal) can continue to make progress.
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_current_locked(
                    db,
                    maintenance_guard=maintenance_guard,
                    maintenance_generation=effective_generation,
                )
                # This table is a disposable projection. Replacing it in one
                # short SQLite transaction cannot expose an empty intermediate
                # view to readers.
                await db.execute("DELETE FROM agent_score_snapshots")
                await db.executemany(
                    """
                    INSERT INTO agent_score_snapshots(
                        agent_id,completed_jobs,failed_jobs,terminal_jobs,
                        lease_expiry_count,observed_outcomes,completion_rate,failure_rate,
                    timeout_rate,feedback_count,feedback_average,average_latency_seconds,
                    composite_score,formula_version,rebuilt_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                    snapshot_values,
                )
                await self._require_current_locked(
                    db,
                    maintenance_guard=maintenance_guard,
                    maintenance_generation=effective_generation,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return snapshots

    async def _read_snapshots(
        self,
        rebuilt_at: str,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None,
    ) -> list[AgentScoreSnapshot]:
        """Read one coherent authoritative snapshot without a write lock."""

        job_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"completed": 0, "failed": 0})
        latency_totals: dict[str, float] = defaultdict(float)
        latency_counts: dict[str, int] = defaultdict(int)
        feedback_totals: dict[str, float] = defaultdict(float)
        feedback_counts: dict[str, int] = defaultdict(int)
        lease_expiries: dict[str, int] = defaultdict(int)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                agent_ids = await self._agent_ids_locked(db)
                await self._yield_read_batch(maintenance_guard)

                async with db.execute(
                    """
                    SELECT status,claimed_by,last_agent_id,claimed_at,completed_at
                    FROM agent_jobs WHERE status IN ('completed','failed')
                    """
                ) as cursor:
                    while rows := await cursor.fetchmany(self.READ_BATCH_SIZE):
                        for row in rows:
                            agent_id = self._job_agent_id(row)
                            if agent_id is None:
                                continue
                            agent_ids.add(agent_id)
                            status = str(row["status"])
                            job_counts[agent_id][status] += 1
                            latency = self._latency_seconds(row["claimed_at"], row["completed_at"])
                            if latency is not None:
                                latency_totals[agent_id] += latency
                                latency_counts[agent_id] += 1
                        await self._yield_read_batch(maintenance_guard)

                async with db.execute(
                    """
                    SELECT agent_id,score FROM feedback_events
                    WHERE agent_id IS NOT NULL AND agent_id<>'' AND score IS NOT NULL
                    """
                ) as cursor:
                    while rows := await cursor.fetchmany(self.READ_BATCH_SIZE):
                        for row in rows:
                            agent_id = str(row["agent_id"])
                            agent_ids.add(agent_id)
                            feedback_totals[agent_id] += self._bounded(
                                float(row["score"]), 0.0, 5.0
                            )
                            feedback_counts[agent_id] += 1
                        await self._yield_read_batch(maintenance_guard)

                async with db.execute(
                    """
                    SELECT payload_json FROM audit_events
                    WHERE event_type='agent.job.lease_expired'
                    """
                ) as cursor:
                    while rows := await cursor.fetchmany(self.READ_BATCH_SIZE):
                        for row in rows:
                            agent_id = self._audit_agent_id(row["payload_json"])
                            if agent_id is None:
                                continue
                            agent_ids.add(agent_id)
                            lease_expiries[agent_id] += 1
                        await self._yield_read_batch(maintenance_guard)
            finally:
                await db.rollback()

        snapshots: list[AgentScoreSnapshot] = []
        for index, agent_id in enumerate(sorted(agent_ids), start=1):
            snapshots.append(
                self._snapshot(
                    agent_id,
                    job_counts[agent_id],
                    lease_expiries[agent_id],
                    feedback_total=feedback_totals[agent_id],
                    feedback_count=feedback_counts[agent_id],
                    latency_total=latency_totals[agent_id],
                    latency_count=latency_counts[agent_id],
                    rebuilt_at=rebuilt_at,
                )
            )
            if index % self.READ_BATCH_SIZE == 0:
                await self._yield_read_batch(maintenance_guard)
        return snapshots

    @staticmethod
    async def _yield_read_batch(
        maintenance_guard: MaintenanceLeaseGuard | None,
    ) -> None:
        if maintenance_guard is not None:
            maintenance_guard.raise_if_lost()
        # Large histories must not monopolize the event loop and starve the
        # independent MaintenanceLeaseRunner renewal task.
        await asyncio.sleep(0)

    async def _require_current_locked(
        self,
        db: aiosqlite.Connection,
        *,
        maintenance_guard: MaintenanceLeaseGuard | None,
        maintenance_generation: int | None,
    ) -> None:
        if maintenance_guard is not None:
            await maintenance_guard.require_current_locked(db)
            return
        if self.maintenance_leases is None:
            return
        if self.owner_instance_id is None or maintenance_generation is None:
            raise RuntimeError("agent scoring requires a current maintenance lease")
        await self.maintenance_leases.require_current_locked(
            db,
            "feedback-maintenance",
            self.owner_instance_id,
            maintenance_generation,
        )

    async def get(self, agent_id: str) -> AgentScoreSnapshot | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM agent_score_snapshots WHERE agent_id=?",
                    (agent_id,),
                )
            ).fetchone()
        return self._from_row(row) if row is not None else None

    async def list_ranked(self) -> list[AgentScoreSnapshot]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM agent_score_snapshots
                    ORDER BY composite_score DESC,agent_id ASC
                    """
                )
            ).fetchall()
        return [self._from_row(row) for row in rows]

    async def _agent_ids_locked(self, db: aiosqlite.Connection) -> set[str]:
        agent_ids: set[str] = set()
        async with db.execute("SELECT id FROM agents") as cursor:
            while rows := await cursor.fetchmany(self.READ_BATCH_SIZE):
                agent_ids.update(str(row[0]) for row in rows)
                await asyncio.sleep(0)
        return agent_ids

    def _snapshot(
        self,
        agent_id: str,
        jobs: dict[str, int],
        lease_expiry_count: int,
        *,
        feedback_total: float,
        feedback_count: int,
        latency_total: float,
        latency_count: int,
        rebuilt_at: str,
    ) -> AgentScoreSnapshot:
        completed_jobs = jobs["completed"]
        failed_jobs = jobs["failed"]
        terminal_jobs = completed_jobs + failed_jobs
        completion_rate = completed_jobs / terminal_jobs if terminal_jobs else 0.0
        failure_rate = failed_jobs / terminal_jobs if terminal_jobs else 0.0
        observed_outcomes = terminal_jobs + lease_expiry_count
        timeout_rate = lease_expiry_count / observed_outcomes if observed_outcomes else 0.0
        feedback_average = feedback_total / feedback_count if feedback_count else None
        average_latency = latency_total / latency_count if latency_count else None

        completion_component = completion_rate if terminal_jobs else 0.5
        timeout_component = 1.0 - timeout_rate if observed_outcomes else 0.5
        feedback_component = feedback_average / 5.0 if feedback_average is not None else 0.5
        composite = (
            0.60 * completion_component + 0.20 * timeout_component + 0.20 * feedback_component
        )
        return AgentScoreSnapshot(
            agent_id=agent_id,
            completed_jobs=completed_jobs,
            failed_jobs=failed_jobs,
            terminal_jobs=terminal_jobs,
            lease_expiry_count=lease_expiry_count,
            observed_outcomes=observed_outcomes,
            completion_rate=round(completion_rate, 6),
            failure_rate=round(failure_rate, 6),
            timeout_rate=round(timeout_rate, 6),
            feedback_count=feedback_count,
            feedback_average=(round(feedback_average, 6) if feedback_average is not None else None),
            average_latency_seconds=(
                round(average_latency, 3) if average_latency is not None else None
            ),
            composite_score=round(self._bounded(composite, 0.0, 1.0), 6),
            formula_version=self.FORMULA_VERSION,
            rebuilt_at=rebuilt_at,
        )

    @staticmethod
    def _job_agent_id(row: aiosqlite.Row) -> str | None:
        raw = row["last_agent_id"] or row["claimed_by"]
        if not isinstance(raw, str) or not raw:
            return None
        return raw

    @staticmethod
    def _audit_agent_id(payload_json: object) -> str | None:
        if not isinstance(payload_json, str):
            return None
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        agent_id = payload.get("agent_id")
        return agent_id if isinstance(agent_id, str) and agent_id else None

    @staticmethod
    def _latency_seconds(claimed_at: object, completed_at: object) -> float | None:
        if not isinstance(claimed_at, str) or not isinstance(completed_at, str):
            return None
        try:
            claimed = datetime.fromisoformat(claimed_at)
            completed = datetime.fromisoformat(completed_at)
        except ValueError:
            return None
        if claimed.tzinfo is None or completed.tzinfo is None:
            return None
        latency = (completed.astimezone(UTC) - claimed.astimezone(UTC)).total_seconds()
        return latency if latency >= 0 else None

    @staticmethod
    def _bounded(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    @staticmethod
    def _snapshot_values(snapshot: AgentScoreSnapshot) -> tuple[object, ...]:
        return (
            snapshot.agent_id,
            snapshot.completed_jobs,
            snapshot.failed_jobs,
            snapshot.terminal_jobs,
            snapshot.lease_expiry_count,
            snapshot.observed_outcomes,
            snapshot.completion_rate,
            snapshot.failure_rate,
            snapshot.timeout_rate,
            snapshot.feedback_count,
            snapshot.feedback_average,
            snapshot.average_latency_seconds,
            snapshot.composite_score,
            snapshot.formula_version,
            snapshot.rebuilt_at,
        )

    @staticmethod
    def _from_row(row: aiosqlite.Row) -> AgentScoreSnapshot:
        return AgentScoreSnapshot(
            agent_id=str(row["agent_id"]),
            completed_jobs=int(row["completed_jobs"]),
            failed_jobs=int(row["failed_jobs"]),
            terminal_jobs=int(row["terminal_jobs"]),
            lease_expiry_count=int(row["lease_expiry_count"]),
            observed_outcomes=int(row["observed_outcomes"]),
            completion_rate=float(row["completion_rate"]),
            failure_rate=float(row["failure_rate"]),
            timeout_rate=float(row["timeout_rate"]),
            feedback_count=int(row["feedback_count"]),
            feedback_average=(
                float(row["feedback_average"]) if row["feedback_average"] is not None else None
            ),
            average_latency_seconds=(
                float(row["average_latency_seconds"])
                if row["average_latency_seconds"] is not None
                else None
            ),
            composite_score=float(row["composite_score"]),
            formula_version=str(row["formula_version"]),
            rebuilt_at=str(row["rebuilt_at"]),
        )

    def _now(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            raise RuntimeError("agent scoring clock must be timezone-aware")
        return value.astimezone(UTC).isoformat()
