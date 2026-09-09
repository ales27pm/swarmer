from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
import yaml

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatcher
from app.services.agent_lease_reaper import AgentLeaseReaper
from app.services.message_board import MessageBoardService
from app.services.permission_policy import PermissionPolicy, PermissionPolicyError
from app.services.state_service import StateService
from app.services.worker_skill_policy import WorkerSkillPolicyFenceError

REPO_ROOT = Path(__file__).resolve().parents[2]


def denied_worker_policy(skill: str) -> PermissionPolicy:
    base = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    worker_rules = dict(base.worker_skill_rules)
    worker_rules[skill] = replace(
        worker_rules[skill],
        decision="deny",
        auto_redistribute=False,
    )
    return PermissionPolicy(
        protected_paths=base.protected_paths,
        process=base.process,
        tool_rules=base.tool_rules,
        capability_rules=base.capability_rules,
        worker_skill_rules=worker_rules,
    )


async def runtime(
    tmp_path: Path,
    *,
    clock: list[datetime] | None = None,
) -> tuple[StateService, AgentDispatcher, dict[str, object], str]:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="list root"), source="device"))
    agent = await state.register_agent(
        AgentCreate(
            name="policy-fenced-reader",
            endpoint="http://127.0.0.1:1",
            skills=["workspace.list_dir"],
        ),
        "device",
    )
    assert await state.heartbeat_agent(
        str(agent["id"]),
        "online",
        str(agent["credential"]),
    )
    if clock is not None:
        seen_at = clock[0].isoformat()
        async with aiosqlite.connect(state.db_path) as db:
            await db.execute(
                "UPDATE agents SET last_seen_at=?,last_heartbeat_at=? WHERE id=?",
                (seen_at, seen_at, agent["id"]),
            )
            await db.commit()
    allowed = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    dispatcher = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=allowed,
        agent_offline_timeout_seconds=300,
        clock=(lambda: clock[0]) if clock is not None else None,
    )
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
    return state, dispatcher, agent, task.id


@pytest.mark.asyncio
async def test_blocked_stale_dispatcher_claim_observes_newer_denial_epoch(
    tmp_path: Path,
) -> None:
    state, _, agent, task_id = await runtime(tmp_path)
    stale_dispatcher = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml"),
    )
    revoker = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=denied_worker_policy("workspace.list_dir"),
    )
    blocker = await aiosqlite.connect(state.db_path)
    blocker.row_factory = aiosqlite.Row
    await blocker.execute("BEGIN IMMEDIATE")
    claiming = asyncio.create_task(stale_dispatcher.claim(str(agent["id"])))
    try:
        await asyncio.sleep(0.05)
        assert not claiming.done()
        _, changed = await revoker.worker_skill_policy.replace_locked(
            blocker,
            denied_worker_policy("workspace.list_dir"),
            now=datetime.now(UTC).isoformat(),
            expected_epoch=1,
        )
        assert changed is True
        await blocker.commit()
    finally:
        await blocker.close()

    assert await asyncio.wait_for(claiming, timeout=1) is None
    with sqlite3.connect(state.db_path) as db:
        policy = db.execute(
            "SELECT epoch FROM worker_skill_policy_state WHERE singleton_id=1"
        ).fetchone()
        job = db.execute(
            "SELECT status FROM agent_jobs WHERE task_id=?",
            (task_id,),
        ).fetchone()
    assert policy == (2,)
    assert job == ("quarantined",)


@pytest.mark.asyncio
async def test_reaper_blocked_by_writer_observes_newer_denial_epoch(
    tmp_path: Path,
) -> None:
    current_time = [datetime(2026, 9, 8, 12, 0, tzinfo=UTC)]
    state, dispatcher, agent, _ = await runtime(tmp_path, clock=current_time)
    claimed = await dispatcher.claim(str(agent["id"]))
    assert claimed is not None
    current_time[0] += timedelta(seconds=61)
    stale_reaper = AgentLeaseReaper(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml"),
        clock=lambda: current_time[0],
    )
    revoker = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=denied_worker_policy("workspace.list_dir"),
    )
    blocker = await aiosqlite.connect(state.db_path)
    blocker.row_factory = aiosqlite.Row
    await blocker.execute("BEGIN IMMEDIATE")
    reaping = asyncio.create_task(stale_reaper.reap_expired())
    try:
        await asyncio.sleep(0.05)
        assert not reaping.done()
        await revoker.worker_skill_policy.replace_locked(
            blocker,
            denied_worker_policy("workspace.list_dir"),
            now=current_time[0].isoformat(),
            expected_epoch=1,
        )
        await blocker.commit()
    finally:
        await blocker.close()

    counts = await asyncio.wait_for(reaping, timeout=1)
    assert counts["requeued"] == 0
    assert counts["quarantined"] == 1
    persisted = await dispatcher.get_job(str(claimed["id"]))
    assert persisted is not None and persisted["status"] == "quarantined"


@pytest.mark.asyncio
async def test_stale_parsed_reload_cannot_overwrite_newer_denial_epoch(
    tmp_path: Path,
) -> None:
    state, dispatcher, _, _ = await runtime(tmp_path)
    raw_policy = yaml.safe_load(
        (REPO_ROOT / "configs" / "permissions.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(raw_policy, dict)
    stale_path = tmp_path / "stale-allow.yaml"
    stale_path.write_text(yaml.safe_dump(raw_policy), encoding="utf-8")

    stale_dispatcher = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=PermissionPolicy.from_yaml(stale_path),
    )
    revoker = AgentDispatcher(
        state.db_path,
        MessageBoardService(state.db_path),
        permission_policy=denied_worker_policy("workspace.list_dir"),
    )
    blocker = await aiosqlite.connect(state.db_path)
    blocker.row_factory = aiosqlite.Row
    await blocker.execute("BEGIN IMMEDIATE")
    stale_reload = asyncio.create_task(stale_dispatcher.reload_worker_skill_policy(stale_path))
    try:
        await asyncio.sleep(0.05)
        assert not stale_reload.done()
        await revoker.worker_skill_policy.replace_locked(
            blocker,
            denied_worker_policy("workspace.list_dir"),
            now=datetime.now(UTC).isoformat(),
            expected_epoch=1,
        )
        await blocker.commit()
    finally:
        await blocker.close()

    with pytest.raises(WorkerSkillPolicyFenceError, match="epoch changed"):
        await asyncio.wait_for(stale_reload, timeout=1)
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        snapshot = await dispatcher.worker_skill_policy.load_locked(
            db,
            now=datetime.now(UTC).isoformat(),
        )
        await db.rollback()
    assert snapshot is not None
    assert snapshot.epoch == 2
    assert snapshot.is_allowed("workspace.list_dir") is False


@pytest.mark.asyncio
async def test_blocked_agent_registration_observes_newer_denial_epoch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    allowed_policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    state = StateService(database, permission_policy=allowed_policy)
    await state.initialize()
    authority = AgentDispatcher(
        database,
        MessageBoardService(database),
        permission_policy=allowed_policy,
    )
    assert await authority.install_worker_skill_policy(allowed_policy)
    stale_state = StateService(
        database,
        permission_policy=PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml"),
    )
    revoker = AgentDispatcher(
        database,
        MessageBoardService(database),
        permission_policy=denied_worker_policy("workspace.list_dir"),
    )
    request = AgentCreate(
        name="blocked-registration",
        endpoint="http://127.0.0.1:1",
        skills=["workspace.list_dir"],
    )
    blocker = await aiosqlite.connect(database)
    blocker.row_factory = aiosqlite.Row
    await blocker.execute("BEGIN IMMEDIATE")
    registering = asyncio.create_task(stale_state.register_agent(request, "device"))
    try:
        await asyncio.sleep(0.05)
        assert not registering.done()
        await revoker.worker_skill_policy.replace_locked(
            blocker,
            denied_worker_policy("workspace.list_dir"),
            now=datetime.now(UTC).isoformat(),
            expected_epoch=1,
        )
        await blocker.commit()
    finally:
        await blocker.close()

    with pytest.raises(PermissionPolicyError, match="denied"):
        await asyncio.wait_for(registering, timeout=1)
    async with aiosqlite.connect(database) as db:
        registered = await (
            await db.execute("SELECT COUNT(*) FROM agents WHERE name='blocked-registration'")
        ).fetchone()
    assert registered == (0,)
