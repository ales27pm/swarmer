"""Real SQLite/flock handoff; no model, GPU, production data or Docker."""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path

import aiosqlite
import pytest
from test_model_resource_integration import (
    LocalPlanner,
    goal,
    manager,
    queued,
    reserve,
    stored_goal,
    worker,
)

from app.services.goal_manager import _LocalModelResourceBusy
from app.services.model_resource_admission import ExternalGPUAdmission


def owner(path: Path) -> int:
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_host_slot_exclusion_marker_and_recovery(tmp_path: Path) -> None:
    path = tmp_path / "gpu.lock"
    external = owner(path)
    admission = ExternalGPUAdmission(path)
    try:
        assert not admission.try_acquire()
        os.write(external, b"chroma-studio-v1:existing-renderer\n")
        os.fsync(external)
    finally:
        os.close(external)
    # Kernel ownership died, but the renderer can still exist: do not admit.
    assert not admission.try_acquire()
    assert path.read_bytes() == b"chroma-studio-v1:existing-renderer\n"
    cleanup = owner(path)
    os.ftruncate(cleanup, 0)
    os.fsync(cleanup)
    os.close(cleanup)
    assert admission.try_acquire()
    other = ExternalGPUAdmission(path)
    assert not other.try_acquire()
    admission.close()
    assert other.try_acquire()
    other.close()


@pytest.mark.parametrize("kind", ["directory", "file_symlink", "parent_symlink", "missing_parent"])
def test_unknown_or_unsafe_lock_fails_closed(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "gpu.lock"
    if kind == "directory":
        path.mkdir()
    elif kind == "file_symlink":
        target = tmp_path / "target"
        target.touch()
        path.symlink_to(target)
    elif kind == "parent_symlink":
        target = tmp_path / "target"
        target.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(target, target_is_directory=True)
        path = alias / "gpu.lock"
    else:
        path = tmp_path / "missing" / "gpu.lock"
    admission = ExternalGPUAdmission(path)
    assert not admission.try_acquire()
    admission.close()


async def configured(tmp_path: Path):
    value = await manager(tmp_path, LocalPlanner())
    path = tmp_path / "gpu.lock"
    value.agent_dispatcher.local_model_gpu_lock_path = path
    return value, path


@pytest.mark.parametrize("state", ["owned", "orphan_marker", "unavailable"])
async def test_external_work_does_not_charge_api_budget(tmp_path: Path, state: str) -> None:
    value, path = await configured(tmp_path)
    created = await goal(value)
    fd = owner(path) if state == "owned" else None
    if state == "orphan_marker":
        path.write_bytes(b"orphaned Studio renderer")
    elif state == "unavailable":
        path.mkdir()
    try:
        with pytest.raises(_LocalModelResourceBusy):
            await reserve(value, created["id"])
        current = await stored_goal(value, created["id"])
        assert current["model_call_count"] == 0
        async with aiosqlite.connect(value.db_path) as db:
            assert (await (await db.execute("SELECT count(*) FROM goal_model_calls")).fetchone())[
                0
            ] == 0
    finally:
        if fd is not None:
            os.close(fd)


async def test_external_work_keeps_gpu_job_queued_but_allows_files(tmp_path: Path) -> None:
    value, path = await configured(tmp_path)
    heavy = await queued(
        value,
        "writing.draft",
        {
            "schema_version": "1.0",
            "objective": "Write a note",
            "conversation": [],
        },
    )
    light = await queued(value, "workspace.list_dir", {"path": "."})
    writer = await worker(value, ["writing.draft"])
    agent = await worker(value, ["workspace.list_dir"])
    fd = owner(path)
    try:
        assert await value.agent_dispatcher.claim(writer) is None
        claimed = await value.agent_dispatcher.claim(agent)
        assert claimed and claimed["id"] == light["id"]
        pending = await value.agent_dispatcher.get_job(heavy["id"])
        assert pending and pending["status"] == "queued"
        assert pending["attempt_count"] == 0 and pending["lease_id"] is None
    finally:
        os.close(fd)


@pytest.mark.parametrize("kind", ["model", "worker"])
async def test_descriptor_survives_reservation_commit(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    value, path = await configured(tmp_path)
    created = await goal(value)
    agent = await worker(value, ["writing.draft"])
    await queued(
        value,
        "writing.draft",
        {
            "schema_version": "1.0",
            "objective": "Write a note",
            "conversation": [],
        },
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = aiosqlite.Connection.commit

    async def held_commit(db):
        entered.set()
        await release.wait()
        await original(db)

    monkeypatch.setattr(aiosqlite.Connection, "commit", held_commit)
    action = (
        reserve(value, created["id"]) if kind == "model" else value.agent_dispatcher.claim(agent)
    )
    running = asyncio.create_task(action)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        admission = ExternalGPUAdmission(path)
        # The reservation is not committed yet. Studio must still be excluded.
        assert not admission.try_acquire()
    finally:
        release.set()
        await running
    admission = ExternalGPUAdmission(path)
    assert admission.try_acquire()
    admission.close()
    # Ownership was handed to the durable lease, so a second API call waits.
    next_goal = await goal(value)
    with pytest.raises(_LocalModelResourceBusy):
        await reserve(value, next_goal["id"])


async def test_cancelled_reservation_rolls_back_before_releasing_flock(
    tmp_path: Path, monkeypatch
) -> None:
    value, path = await configured(tmp_path)
    created = await goal(value)
    entered = asyncio.Event()

    async def cancelled_commit(_db):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(aiosqlite.Connection, "commit", cancelled_commit)
    running = asyncio.create_task(reserve(value, created["id"]))
    await asyncio.wait_for(entered.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    admission = ExternalGPUAdmission(path)
    assert admission.try_acquire()
    admission.close()
    current = await stored_goal(value, created["id"])
    assert current["model_call_count"] == 0
    async with aiosqlite.connect(value.db_path) as db:
        assert (await (await db.execute("SELECT count(*) FROM goal_model_calls")).fetchone())[
            0
        ] == 0
