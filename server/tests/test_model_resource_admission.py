from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.services.model_resource_admission import (
    active_local_model_work_locked,
    requires_local_model_resource,
)

NOW = datetime(2026, 10, 1, 5, tzinfo=UTC)


@pytest.fixture
async def resource_db(tmp_path: Path) -> Path:
    path = tmp_path / "resources.db"
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            CREATE TABLE goal_model_calls (
                id TEXT PRIMARY KEY, provider_source TEXT NOT NULL,
                status TEXT NOT NULL, lease_expires_at TEXT
            );
            CREATE TABLE agent_jobs (
                id TEXT PRIMARY KEY, required_skill TEXT NOT NULL,
                status TEXT NOT NULL, lease_expires_at TEXT
            );
            """
        )
    return path


async def add_work(
    path: Path, *, kind: str, category: str, status: str, expires: datetime | None
) -> None:
    table, column = (
        ("goal_model_calls", "provider_source")
        if kind == "model"
        else ("agent_jobs", "required_skill")
    )
    async with aiosqlite.connect(path) as db:
        await db.execute(
            f"INSERT INTO {table}(id,{column},status,lease_expires_at) VALUES(?,?,?,?)",
            ("existing", category, status, expires.isoformat() if expires else None),
        )
        await db.commit()


async def busy(path: Path, now: datetime = NOW) -> bool:
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        return await active_local_model_work_locked(db, now=now)


@pytest.mark.parametrize("skill", ["writing.draft", "code.build_project", "code.generate_python"])
def test_generation_skills_share_the_resource(skill: str) -> None:
    assert requires_local_model_resource(skill)


@pytest.mark.parametrize(
    "skill",
    ["research.collect", "research.query", "workspace.read_text", "code.review_python"],
)
def test_non_generation_jobs_remain_parallel(skill: str) -> None:
    assert not requires_local_model_resource(skill)


@pytest.mark.parametrize(
    "kind,category,status",
    [
        ("model", "ubuntu_local", "started"),
        ("job", "writing.draft", "claimed"),
        ("job", "writing.draft", "running"),
        ("job", "code.build_project", "running"),
        ("job", "code.generate_python", "running"),
    ],
)
async def test_live_local_generation_blocks_other_reservations(
    resource_db: Path, kind: str, category: str, status: str
) -> None:
    await add_work(
        resource_db,
        kind=kind,
        category=category,
        status=status,
        expires=NOW + timedelta(seconds=60),
    )
    assert await busy(resource_db)


@pytest.mark.parametrize(
    "kind,category,status",
    [
        ("model", "iphone_local", "started"),
        ("model", "manual", "started"),
        ("model", "test", "started"),
        ("model", "ubuntu_local", "completed"),
        ("model", "ubuntu_local", "failed"),
        ("job", "writing.draft", "queued"),
        ("job", "writing.draft", "completed"),
        ("job", "writing.draft", "cancelled"),
        ("job", "research.collect", "running"),
        ("job", "workspace.read_text", "running"),
    ],
)
async def test_other_work_does_not_hold_local_generation_resource(
    resource_db: Path, kind: str, category: str, status: str
) -> None:
    await add_work(
        resource_db,
        kind=kind,
        category=category,
        status=status,
        expires=NOW + timedelta(seconds=60),
    )
    assert not await busy(resource_db)


@pytest.mark.parametrize("kind,category", [("model", "ubuntu_local"), ("job", "writing.draft")])
@pytest.mark.parametrize("expires", [None, NOW - timedelta(seconds=1), NOW])
async def test_expired_or_missing_leases_do_not_deadlock_crash_recovery(
    resource_db: Path, kind: str, category: str, expires: datetime | None
) -> None:
    await add_work(
        resource_db,
        kind=kind,
        category=category,
        status="started" if kind == "model" else "running",
        expires=expires,
    )
    assert not await busy(resource_db)
    # Admission never rewrites the old lease or its failure/accounting history.
    table = "goal_model_calls" if kind == "model" else "agent_jobs"
    async with aiosqlite.connect(resource_db) as db:
        row = await (await db.execute(f"SELECT status,lease_expires_at FROM {table}")).fetchone()
    assert row == (
        "started" if kind == "model" else "running",
        expires.isoformat() if expires else None,
    )


async def test_releasing_existing_lease_admits_next_owner(resource_db: Path) -> None:
    await add_work(
        resource_db,
        kind="job",
        category="writing.draft",
        status="running",
        expires=NOW + timedelta(seconds=60),
    )
    assert await busy(resource_db)
    async with aiosqlite.connect(resource_db) as db:
        await db.execute("UPDATE agent_jobs SET status='completed' WHERE id='existing'")
        await db.commit()
    assert not await busy(resource_db)


async def test_shared_sqlite_transaction_serializes_api_and_worker_admission(
    resource_db: Path,
) -> None:
    async def reserve(kind: str) -> bool:
        async with aiosqlite.connect(resource_db) as db:
            await db.execute("BEGIN IMMEDIATE")
            if await active_local_model_work_locked(db, now=NOW):
                await db.rollback()
                return False
            await asyncio.sleep(0)
            if kind == "model":
                await db.execute(
                    "INSERT INTO goal_model_calls VALUES('api','ubuntu_local','started',?)",
                    ((NOW + timedelta(seconds=60)).isoformat(),),
                )
            else:
                await db.execute(
                    "INSERT INTO agent_jobs VALUES('worker','writing.draft','claimed',?)",
                    ((NOW + timedelta(seconds=60)).isoformat(),),
                )
            await db.commit()
            return True

    assert sorted(await asyncio.gather(reserve("model"), reserve("job"))) == [False, True]
    async with aiosqlite.connect(resource_db) as db:
        models = await (await db.execute("SELECT count(*) FROM goal_model_calls")).fetchone()
        jobs = await (await db.execute("SELECT count(*) FROM agent_jobs")).fetchone()
    assert models is not None and jobs is not None and models[0] + jobs[0] == 1


async def test_admission_rejects_unlocked_or_naive_time_checks(resource_db: Path) -> None:
    async with aiosqlite.connect(resource_db) as db:
        with pytest.raises(RuntimeError, match="transaction"):
            await active_local_model_work_locked(db, now=NOW)
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="timezone-aware"):
            await active_local_model_work_locked(db, now=NOW.replace(tzinfo=None))
