from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatcher
from app.services.iphone_capability_service import IPhoneCapabilityService
from app.services.message_board import MessageBoardService
from app.services.outbox import OutboxService
from app.services.permission_policy import PermissionPolicy
from app.services.state_service import StateService


class UnavailableMessageBoard:
    async def _raise(self, *_: object, **__: object) -> dict[str, Any]:
        raise OSError("message board unavailable")

    publish = _raise
    record = _raise
    claim = _raise
    ack = _raise
    fail = _raise
    heartbeat = _raise

    async def list_events(self, **_: object) -> list[dict[str, Any]]:
        return []


async def create_task_and_agent(
    database: Path,
) -> tuple[StateService, TaskRecord, dict[str, Any]]:
    state = StateService(database)
    await state.initialize()
    task = await state.create_task(TaskRecord.new(TaskCreate(input="list root"), source="phone"))
    agent = await state.register_agent(
        AgentCreate(
            name="reader",
            endpoint="http://127.0.0.1:9001",
            skills=["workspace.list_dir"],
        ),
        "phone",
    )
    online = await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    assert online is not None
    return state, task, agent


async def pending_outbox_rows(database: Path, aggregate_id: str) -> list[tuple[str, str]]:
    async with aiosqlite.connect(database) as db:
        return [
            (str(row[0]), str(row[1]))
            for row in await (
                await db.execute(
                    """
                    SELECT topic,event_type FROM outbox_events
                    WHERE aggregate_id=? AND published_at IS NULL
                    ORDER BY id
                    """,
                    (aggregate_id,),
                )
            ).fetchall()
        ]


@pytest.mark.asyncio
async def test_queue_commit_survives_board_failure_with_pending_outbox(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _, task, _ = await create_task_and_agent(database)
    dispatcher = AgentDispatcher(database, UnavailableMessageBoard())

    job = await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})

    assert job["status"] == "queued"
    assert await pending_outbox_rows(database, job["id"]) == [("tasks.inbox", "published")]


@pytest.mark.asyncio
async def test_claim_returns_lease_when_board_fails_and_keeps_outbox(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _, task, agent = await create_task_and_agent(database)
    healthy = AgentDispatcher(database, MessageBoardService(database))
    queued = await healthy.queue_job(task.id, "workspace.list_dir", {"path": "."})
    dispatcher = AgentDispatcher(database, UnavailableMessageBoard())

    claimed = await dispatcher.claim(agent["id"])

    assert claimed is not None
    assert claimed["id"] == queued["id"]
    assert claimed["status"] == "claimed"
    assert claimed["claim_token"]
    assert await pending_outbox_rows(database, claimed["id"]) == [("tasks.inbox", "claimed")]


@pytest.mark.asyncio
async def test_result_commit_survives_board_failure_with_pending_outbox(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _, task, agent = await create_task_and_agent(database)
    healthy = AgentDispatcher(database, MessageBoardService(database))
    await healthy.queue_job(task.id, "workspace.list_dir", {"path": "."})
    claimed = await healthy.claim(agent["id"])
    assert claimed is not None
    dispatcher = AgentDispatcher(database, UnavailableMessageBoard())

    completed, changed = await dispatcher.submit_result(
        agent["id"],
        claimed["id"],
        claimed["claim_token"],
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
        status="completed",
        result={"entries": ["README.md"]},
        error=None,
    )

    assert changed is True
    assert completed["status"] == "completed"
    assert await pending_outbox_rows(database, completed["id"]) == [("tasks.status", "acked")]


@pytest.mark.asyncio
async def test_restart_drain_publishes_previously_pending_event(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _, task, _ = await create_task_and_agent(database)
    unavailable = AgentDispatcher(database, UnavailableMessageBoard())
    job = await unavailable.queue_job(task.id, "workspace.list_dir", {"path": "."})

    board = MessageBoardService(database)
    restarted = OutboxService(database, board)
    drained = await restarted.drain()

    assert drained == {"selected": 1, "published": 1, "failed": 0, "pending": 0}
    events = await board.list_events()
    assert [(event["message_id"], event["event_type"]) for event in events] == [
        (job["id"], "published")
    ]


@pytest.mark.asyncio
async def test_duplicate_drain_does_not_duplicate_board_event(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _, task, _ = await create_task_and_agent(database)
    unavailable = AgentDispatcher(database, UnavailableMessageBoard())
    job = await unavailable.queue_job(task.id, "workspace.list_dir", {"path": "."})
    board = MessageBoardService(database)
    outbox = OutboxService(database, board)

    await outbox.drain()
    await outbox.drain()

    events = [event for event in await board.list_events() if event["message_id"] == job["id"]]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_publish_before_mark_crash_converges_without_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.db"
    _, task, _ = await create_task_and_agent(database)
    unavailable = AgentDispatcher(database, UnavailableMessageBoard())
    job = await unavailable.queue_job(task.id, "workspace.list_dir", {"path": "."})
    board = MessageBoardService(database)
    outbox = OutboxService(database, board)
    original_mark = outbox.mark_published

    async def crash_before_mark(_: int) -> None:
        raise RuntimeError("simulated crash before published_at commit")

    monkeypatch.setattr(outbox, "mark_published", crash_before_mark)
    first = await outbox.drain()
    assert first["failed"] == 1
    assert first["pending"] == 1
    monkeypatch.setattr(outbox, "mark_published", original_mark)

    second = await outbox.drain()

    assert second["published"] == 1
    events = [event for event in await board.list_events() if event["message_id"] == job["id"]]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_capability_transitions_survive_board_failure(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _, task, agent = await create_task_and_agent(database)
    board = MessageBoardService(database)
    dispatcher = AgentDispatcher(database, board)
    await dispatcher.queue_job(task.id, "workspace.list_dir", {"path": "."})
    lease = await dispatcher.claim(agent["id"])
    assert lease is not None
    async with aiosqlite.connect(database) as db:
        await db.execute(
            "INSERT INTO devices(id,name,token,created_at) VALUES(?,?,?,?)",
            ("phone", "Test phone", "sha256:" + "0" * 64, task.created_at.isoformat()),
        )
        await db.commit()
    policy = PermissionPolicy.from_yaml(Path(__file__).parents[2] / "configs/permissions.yaml")
    capability = IPhoneCapabilityService(database, UnavailableMessageBoard(), policy)

    request = await capability.create_request(
        agent_id=agent["id"],
        job_id=lease["id"],
        claim_token=lease["claim_token"],
        lease_id=lease["lease_id"],
        lease_generation=lease["lease_generation"],
        capability_name="iphone.location.current",
        arguments={},
    )
    approved = await capability.authorize(request["request_id"], "phone", decision="approve")
    grant = approved["grant"]
    await capability.consume(
        request["request_id"],
        "phone",
        grant_id=grant["grant_id"],
        action_digest=approved["action_digest"],
    )
    receipt = await capability.submit_result(
        request["request_id"],
        "phone",
        grant_id=grant["grant_id"],
        action_digest=approved["action_digest"],
        result={
            "name": "iphone.location.current",
            "status": "completed",
            "value": {"latitude": 45.5, "longitude": -73.6, "accuracy": 10},
        },
    )

    assert receipt["status"] == "accepted"
    async with aiosqlite.connect(database) as db:
        status_row = await (
            await db.execute(
                "SELECT status FROM iphone_capability_requests WHERE id=?",
                (request["request_id"],),
            )
        ).fetchone()
        pending = await (
            await db.execute(
                """
                SELECT event_type FROM outbox_events
                WHERE aggregate_id=? AND published_at IS NULL ORDER BY id
                """,
                (request["request_id"],),
            )
        ).fetchall()
    assert status_row is not None and status_row[0] == "completed"
    assert [row[0] for row in pending] == [
        "capability_requested",
        "capability_authorized",
        "capability_consumed",
        "capability_result",
    ]
