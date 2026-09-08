from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.agent_lease_reaper import AgentLeaseReaper
from app.services.message_board import DurableEvent, SQLiteMessageBoard
from app.services.outbox import OutboxService
from app.services.state_service import StateService


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _agent(state: StateService, index: int) -> dict[str, object]:
    record = await state.register_agent(
        AgentCreate(
            name=f"reader-{index}",
            endpoint=f"http://127.0.0.1:{9200 + index}",
            skills=["workspace.list_dir"],
        ),
        "phone",
    )
    assert await state.heartbeat_agent(str(record["id"]), "online", str(record["credential"]))
    return record


@pytest.mark.asyncio
async def test_ten_claimers_produce_exactly_one_lease(tmp_path: Path) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    agents = [await _agent(state, index) for index in range(10)]
    task = await state.create_task(TaskRecord.new(TaskCreate(input="list"), source="phone"))
    dispatcher = AgentDispatcher(state.db_path, SQLiteMessageBoard(state.db_path))
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    claims = await asyncio.gather(
        *(dispatcher.claim(str(agent["id"])) for agent in agents),
        return_exceptions=True,
    )

    successful = [claim for claim in claims if isinstance(claim, dict)]
    assert len(successful) == 1
    assert all(
        claim is None or isinstance(claim, (dict, AgentDispatchConflict)) for claim in claims
    )
    claimed_job = await dispatcher.get_job(str(successful[0]["id"]))
    assert claimed_job is not None and claimed_job["lease_generation"] == 1


@pytest.mark.asyncio
async def test_five_outbox_drainers_converge_without_lost_events(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    board = SQLiteMessageBoard(database)
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        for index in range(50):
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="stress",
                aggregate_id=f"item-{index}",
                topic="system.stress",
                event_type="published",
                payload={"item_id": f"item-{index}"},
                dedupe_key=f"stress:{index}",
            )
        await db.commit()
    drainers = [
        OutboxService(database, board, instance_id=f"stress-drainer-{index}") for index in range(5)
    ]

    await asyncio.gather(*(drainer.drain(limit=50) for drainer in drainers))

    assert await drainers[0].pending_count() == 0
    events = await board.list_events(topic="system.stress", limit=100)
    assert len(events) == 50
    assert len({event["dedupe_key"] for event in events}) == 50


@pytest.mark.asyncio
async def test_lease_reaper_racing_heartbeat_has_one_fenced_outcome(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    state = StateService(database)
    await state.initialize()
    agent = await _agent(state, 1)
    task = await state.create_task(TaskRecord.new(TaskCreate(input="list"), source="phone"))
    start = datetime(2026, 9, 8, tzinfo=UTC)
    heartbeat_clock = MutableClock(start)
    reaper_clock = MutableClock(start)
    async with aiosqlite.connect(database) as db:
        await db.execute(
            "UPDATE agents SET last_seen_at=?,last_heartbeat_at=? WHERE id=?",
            (start.isoformat(), start.isoformat(), agent["id"]),
        )
        await db.commit()
    board = SQLiteMessageBoard(database)
    dispatcher = AgentDispatcher(database, board, lease_seconds=60, clock=heartbeat_clock)
    reaper = AgentLeaseReaper(database, board, clock=reaper_clock)
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
    claim = await dispatcher.claim(str(agent["id"]))
    assert claim is not None
    heartbeat_clock.now = start + timedelta(seconds=59)
    reaper_clock.now = start + timedelta(seconds=60)

    outcomes = await asyncio.gather(
        dispatcher.heartbeat(
            str(agent["id"]),
            str(claim["id"]),
            str(claim["claim_token"]),
            lease_id=str(claim["lease_id"]),
            lease_generation=int(claim["lease_generation"]),
        ),
        reaper.reap_expired(),
        return_exceptions=True,
    )
    current = await dispatcher.get_job(str(claim["id"]))

    assert current is not None
    assert current["status"] in {"claimed", "running", "queued"}
    if current["status"] in {"claimed", "running"}:
        assert isinstance(outcomes[0], dict)
        assert current["lease_expires_at"] > (start + timedelta(seconds=60)).isoformat()
    else:
        assert isinstance(outcomes[0], AgentDispatchConflict)
        assert current["lease_generation"] == 1


@pytest.mark.asyncio
async def test_duplicate_notification_delivery_is_application_deduplicated(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    board = SQLiteMessageBoard(database)
    event = DurableEvent(
        schema_version="1.0",
        event_id="evt_duplicate_stress",
        dedupe_key="capability-notification:duplicate-stress",
        topic="iphone.capabilities",
        event_type="capability_requested",
        aggregate_type="iphone_capability_request",
        aggregate_id="iphreq_duplicate_stress",
        task_id="tsk_duplicate_stress",
        agent_id="agt_duplicate_stress",
        payload={
            "request_id": "iphreq_duplicate_stress",
            "capability_name": "iphone.location.current",
            "arguments_redacted": True,
        },
        created_at=datetime(2026, 9, 8, tzinfo=UTC).isoformat(),
    )

    receipts = await asyncio.gather(*(board.publish(event) for _ in range(10)))

    assert sum(not receipt["duplicate"] for receipt in receipts) == 1
    events = await board.list_events(topic="iphone.capabilities")
    assert len(events) == 1
