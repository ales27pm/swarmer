from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from app.services.model_resource_admission import (
    ExternalGPUAdmission,
    model_admission_connection,
)


async def test_repeated_cancellation_keeps_flock_until_blocked_commit_finishes(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    db_path, lock_path = root / "reservations.db", root / "gpu.lock"
    with sqlite3.connect(db_path) as setup:
        setup.execute("CREATE TABLE reservations (id INTEGER)")
    reader = sqlite3.connect(db_path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM reservations").fetchall()
    commit_started = asyncio.Event()
    loop = asyncio.get_running_loop()
    admissions: list[ExternalGPUAdmission] = []
    descriptor: int | None = None

    async def reserve() -> None:
        nonlocal descriptor
        async with model_admission_connection(db_path, lock_path) as (db, admission):
            admissions.append(admission)
            await db.execute("BEGIN IMMEDIATE")
            assert admission.try_acquire()
            descriptor = admission.descriptor
            await db.execute("INSERT INTO reservations VALUES (1)")

            def trace(statement: str) -> None:
                if statement == "COMMIT":
                    loop.call_soon_threadsafe(commit_started.set)

            await db.set_trace_callback(trace)
            await db.commit()

    task = asyncio.create_task(reserve())
    contender = ExternalGPUAdmission(lock_path)
    try:
        # The trace callback proves COMMIT reached SQLite. The reader keeps its
        # exclusive lock pending, so no timing assumption about disk speed is used.
        await asyncio.wait_for(commit_started.wait(), timeout=3)
        for _ in range(3):
            task.cancel()
            # Deliver each cancellation separately, including during cleanup.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert not contender.try_acquire(), "flock released while COMMIT was pending"
        assert not task.done()
        assert reader.execute("SELECT count(*) FROM reservations").fetchone() == (0,)
    finally:
        contender.close()
        reader.rollback()
        reader.close()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)

    # Cancellation is propagated only after the already-queued COMMIT drains.
    with sqlite3.connect(db_path) as observer:
        assert observer.execute("SELECT count(*) FROM reservations").fetchone() == (1,)
    assert admissions[0].descriptor is None
    assert descriptor is not None
    with pytest.raises(OSError):
        os.fstat(descriptor)
    try:
        assert contender.try_acquire()
    finally:
        contender.close()


async def test_body_error_rolls_back_before_releasing_flock(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    db_path, lock_path = root / "reservations.db", root / "gpu.lock"
    with sqlite3.connect(db_path) as setup:
        setup.execute("CREATE TABLE reservations (id INTEGER)")

    with pytest.raises(ValueError, match="fixture failure"):
        async with model_admission_connection(db_path, lock_path) as (db, admission):
            await db.execute("BEGIN IMMEDIATE")
            assert admission.try_acquire()
            await db.execute("INSERT INTO reservations VALUES (1)")
            raise ValueError("fixture failure")

    contender = ExternalGPUAdmission(lock_path)
    try:
        assert contender.try_acquire()
        with sqlite3.connect(db_path) as observer:
            assert observer.execute("SELECT count(*) FROM reservations").fetchone() == (0,)
    finally:
        contender.close()
