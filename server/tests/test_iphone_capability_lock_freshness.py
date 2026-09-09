from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatcher
from app.services.iphone_capability_service import (
    IPhoneCapabilityConflict,
    IPhoneCapabilityService,
)
from app.services.message_board import MessageBoardService
from app.services.permission_policy import PermissionPolicy
from app.services.state_service import StateService


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass
class CapabilityRuntime:
    database: Path
    clock: MutableClock
    service: IPhoneCapabilityService
    agent: dict[str, Any]
    lease: dict[str, Any]


async def setup_runtime(
    database: Path,
    *,
    lease_seconds: int = 60,
    grant_ttl_seconds: int = 90,
) -> CapabilityRuntime:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="locate the phone"), source="phone")
    )
    agent = await state.register_agent(
        AgentCreate(
            name="capability-reader",
            endpoint="http://127.0.0.1:9001",
            skills=["workspace.list_dir"],
        ),
        "phone",
    )
    assert await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    timestamp = clock.value.isoformat()
    async with aiosqlite.connect(database) as db:
        await db.execute(
            "UPDATE agents SET last_seen_at=?,last_heartbeat_at=? WHERE id=?",
            (timestamp, timestamp, agent["id"]),
        )
        await db.execute(
            "INSERT INTO devices(id,name,token,created_at) VALUES(?,?,?,?)",
            ("phone", "Test phone", "sha256:" + "0" * 64, timestamp),
        )
        await db.commit()

    board = MessageBoardService(database)
    dispatcher = AgentDispatcher(
        database,
        board,
        lease_seconds=lease_seconds,
        clock=clock,
    )
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
    lease = await dispatcher.claim(agent["id"])
    assert lease is not None
    policy = PermissionPolicy.from_yaml(
        Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml"
    )
    service = IPhoneCapabilityService(
        database,
        board,
        policy,
        grant_ttl_seconds=grant_ttl_seconds,
        clock=clock,
    )
    return CapabilityRuntime(database, clock, service, agent, lease)


async def create_location_request(runtime: CapabilityRuntime) -> dict[str, Any]:
    return await runtime.service.create_request(
        agent_id=str(runtime.agent["id"]),
        job_id=str(runtime.lease["id"]),
        claim_token=str(runtime.lease["claim_token"]),
        lease_id=str(runtime.lease["lease_id"]),
        lease_generation=int(runtime.lease["lease_generation"]),
        capability_name="iphone.location.current",
        arguments={},
    )


async def run_after_writer_wait[T](
    database: Path,
    clock: MutableClock,
    operation: Awaitable[T],
    *,
    advance_seconds: int,
) -> asyncio.Task[T]:
    blocker = await aiosqlite.connect(database)
    await blocker.execute("BEGIN IMMEDIATE")
    running = asyncio.create_task(operation)
    try:
        await asyncio.sleep(0.05)
        assert not running.done()
        clock.advance(advance_seconds)
        await blocker.commit()
    except BaseException:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        raise
    finally:
        await blocker.close()
    return running


async def request_status(database: Path, request_id: str) -> str:
    async with aiosqlite.connect(database) as db:
        row = await (
            await db.execute(
                "SELECT status FROM iphone_capability_requests WHERE id=?",
                (request_id,),
            )
        ).fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.asyncio
async def test_create_request_rechecks_worker_lease_after_writer_wait(tmp_path: Path) -> None:
    runtime = await setup_runtime(tmp_path / "state.db")

    running = await run_after_writer_wait(
        runtime.database,
        runtime.clock,
        create_location_request(runtime),
        advance_seconds=61,
    )

    with pytest.raises(IPhoneCapabilityConflict, match="stale, expired, or inactive"):
        await asyncio.wait_for(running, timeout=1)
    async with aiosqlite.connect(runtime.database) as db:
        count = await (
            await db.execute("SELECT COUNT(*) FROM iphone_capability_requests")
        ).fetchone()
    assert count is not None and int(count[0]) == 0


@pytest.mark.asyncio
async def test_create_request_ttl_starts_after_writer_wait(tmp_path: Path) -> None:
    runtime = await setup_runtime(tmp_path / "state.db", lease_seconds=300)

    running = await run_after_writer_wait(
        runtime.database,
        runtime.clock,
        create_location_request(runtime),
        advance_seconds=10,
    )
    request = await asyncio.wait_for(running, timeout=1)

    assert request["created_at"] == "2026-01-01T00:00:10+00:00"
    assert request["expires_at"] == "2026-01-01T00:03:10+00:00"


@pytest.mark.asyncio
async def test_authorize_rechecks_worker_lease_after_writer_wait(tmp_path: Path) -> None:
    runtime = await setup_runtime(tmp_path / "state.db")
    request = await create_location_request(runtime)

    running = await run_after_writer_wait(
        runtime.database,
        runtime.clock,
        runtime.service.authorize(
            request["request_id"],
            "phone",
            decision="approve",
        ),
        advance_seconds=61,
    )

    with pytest.raises(IPhoneCapabilityConflict, match="worker lease is no longer active"):
        await asyncio.wait_for(running, timeout=1)
    assert await request_status(runtime.database, request["request_id"]) == "cancelled"
    async with aiosqlite.connect(runtime.database) as db:
        count = await (await db.execute("SELECT COUNT(*) FROM iphone_capability_grants")).fetchone()
    assert count is not None and int(count[0]) == 0


@pytest.mark.asyncio
async def test_grant_ttl_starts_after_writer_wait(tmp_path: Path) -> None:
    runtime = await setup_runtime(
        tmp_path / "state.db",
        lease_seconds=300,
        grant_ttl_seconds=40,
    )
    request = await create_location_request(runtime)

    running = await run_after_writer_wait(
        runtime.database,
        runtime.clock,
        runtime.service.authorize(
            request["request_id"],
            "phone",
            decision="approve",
        ),
        advance_seconds=10,
    )
    approved = await asyncio.wait_for(running, timeout=1)

    assert approved["grant"]["issued_at"] == "2026-01-01T00:00:10+00:00"
    assert approved["grant"]["expires_at"] == "2026-01-01T00:00:50+00:00"


@pytest.mark.asyncio
async def test_consume_rechecks_grant_expiry_after_writer_wait(tmp_path: Path) -> None:
    runtime = await setup_runtime(
        tmp_path / "state.db",
        lease_seconds=300,
        grant_ttl_seconds=30,
    )
    request = await create_location_request(runtime)
    approved = await runtime.service.authorize(
        request["request_id"],
        "phone",
        decision="approve",
    )
    grant = approved["grant"]

    running = await run_after_writer_wait(
        runtime.database,
        runtime.clock,
        runtime.service.consume(
            request["request_id"],
            "phone",
            grant_id=grant["grant_id"],
            action_digest=approved["action_digest"],
        ),
        advance_seconds=31,
    )

    with pytest.raises(IPhoneCapabilityConflict, match="grant expired"):
        await asyncio.wait_for(running, timeout=1)
    assert await request_status(runtime.database, request["request_id"]) == "expired"
    async with aiosqlite.connect(runtime.database) as db:
        consumed = await (
            await db.execute(
                "SELECT consumed_at FROM iphone_capability_grants WHERE request_id=?",
                (request["request_id"],),
            )
        ).fetchone()
    assert consumed is not None and consumed[0] is None


@pytest.mark.asyncio
async def test_submit_result_rechecks_worker_lease_after_writer_wait(tmp_path: Path) -> None:
    runtime = await setup_runtime(tmp_path / "state.db")
    request = await create_location_request(runtime)
    approved = await runtime.service.authorize(
        request["request_id"],
        "phone",
        decision="approve",
    )
    grant = approved["grant"]
    await runtime.service.consume(
        request["request_id"],
        "phone",
        grant_id=grant["grant_id"],
        action_digest=approved["action_digest"],
    )

    running = await run_after_writer_wait(
        runtime.database,
        runtime.clock,
        runtime.service.submit_result(
            request["request_id"],
            "phone",
            grant_id=grant["grant_id"],
            action_digest=approved["action_digest"],
            result={
                "name": "iphone.location.current",
                "status": "completed",
                "value": {"latitude": 45.5, "longitude": -73.6, "accuracy": 10},
            },
        ),
        advance_seconds=61,
    )

    with pytest.raises(IPhoneCapabilityConflict, match="worker lease is no longer active"):
        await asyncio.wait_for(running, timeout=1)
    assert await request_status(runtime.database, request["request_id"]) == "cancelled"
    async with aiosqlite.connect(runtime.database) as db:
        count = await (
            await db.execute("SELECT COUNT(*) FROM iphone_capability_results")
        ).fetchone()
    assert count is not None and int(count[0]) == 0


@pytest.mark.asyncio
async def test_poll_rechecks_worker_lease_after_writer_wait(tmp_path: Path) -> None:
    runtime = await setup_runtime(tmp_path / "state.db")
    request = await create_location_request(runtime)

    running = await run_after_writer_wait(
        runtime.database,
        runtime.clock,
        runtime.service.poll_for_worker(
            request["request_id"],
            agent_id=str(runtime.agent["id"]),
            job_id=str(runtime.lease["id"]),
            claim_token=str(runtime.lease["claim_token"]),
            lease_id=str(runtime.lease["lease_id"]),
            lease_generation=int(runtime.lease["lease_generation"]),
        ),
        advance_seconds=61,
    )

    with pytest.raises(IPhoneCapabilityConflict, match="stale, expired, or inactive"):
        await asyncio.wait_for(running, timeout=1)
