from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.event_privacy import EventPrivacyError
from app.services.message_board import (
    DurableEvent,
    MessageBoardDedupeConflict,
    MessageBoardService,
    SQLiteMessageBoard,
)
from app.services.state_service import StateService


def event(*, dedupe_key: str = "task-1:queued") -> DurableEvent:
    return DurableEvent(
        schema_version="1.0",
        event_id="evt_task_1_queued",
        dedupe_key=dedupe_key,
        topic="tasks.inbox",
        event_type="published",
        aggregate_type="agent_job",
        aggregate_id="job_1",
        task_id="task_1",
        agent_id=None,
        payload={"job_id": "job_1", "status": "queued"},
        created_at=datetime(2026, 9, 8, 16, 0, tzinfo=UTC).isoformat(),
    )


@pytest.mark.asyncio
async def test_sqlite_message_board_persists_lifecycle_events(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await StateService(path).initialize()
    board = MessageBoardService(path)

    published = await board.publish("tasks.inbox", {"kind": "read"}, task_id="tsk_1")
    await board.claim(published["message_id"], topic="tasks.inbox", agent_id="agt_1")
    await board.heartbeat(published["message_id"], topic="agents.heartbeat", agent_id="agt_1")
    await board.ack(published["message_id"], topic="tasks.status", agent_id="agt_1")

    events = await board.list_events(after_id=0)
    assert [event["event_type"] for event in events] == [
        "published",
        "claimed",
        "heartbeat",
        "acked",
    ]
    assert events[0]["payload"] == {"kind": "read"}
    assert "payload_json" not in events[0]


def test_durable_event_requires_application_dedupe_key() -> None:
    with pytest.raises(ValueError, match="dedupe"):
        event(dedupe_key="")


def test_durable_event_rejects_sensitive_payload_at_transport_boundary() -> None:
    with pytest.raises(EventPrivacyError):
        DurableEvent(
            schema_version="1.0",
            event_id="evt_unsafe",
            dedupe_key="unsafe:token",
            topic="system.events",
            event_type="published",
            aggregate_type="system",
            aggregate_id="unsafe",
            payload={"lease_token": "must-never-reach-a-board"},
            created_at=datetime(2026, 9, 8, 16, 0, tzinfo=UTC).isoformat(),
        )


@pytest.mark.asyncio
async def test_board_revalidates_event_after_nested_payload_mutation(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await StateService(path).initialize()
    nested = {"safe": True}
    durable = DurableEvent(
        schema_version="1.0",
        event_id="evt_nested",
        dedupe_key="nested:mutation",
        topic="system.events",
        event_type="published",
        aggregate_type="system",
        aggregate_id="nested",
        payload={"nested": nested},
        created_at=datetime(2026, 9, 8, 16, 0, tzinfo=UTC).isoformat(),
    )
    nested["lease_token"] = "mutated-after-validation"
    assert "lease_token" not in durable.payload["nested"]

    durable.payload["nested"]["lease_token"] = "mutated-event-payload"
    with pytest.raises(EventPrivacyError):
        await SQLiteMessageBoard(path).publish(durable)


@pytest.mark.asyncio
async def test_sqlite_message_board_v2_publish_is_idempotent_and_healthy(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await StateService(path).initialize()
    board = SQLiteMessageBoard(path)
    durable = event()

    first = await board.publish(durable)
    duplicate = await board.publish(durable)
    health = await board.health()
    events = await board.list_events(after_id=0)
    await board.close()

    assert first["event_id"] == durable.event_id
    assert first["duplicate"] is False
    assert duplicate["event_id"] == durable.event_id
    assert duplicate["duplicate"] is True
    assert len(events) == 1
    assert events[0]["dedupe_key"] == durable.dedupe_key
    assert events[0]["payload"] == durable.payload
    assert health["backend"] == "sqlite"
    assert health["status"] == "connected"


@pytest.mark.asyncio
async def test_sqlite_message_board_rejects_dedupe_key_rebound_to_new_event(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    await StateService(path).initialize()
    board = SQLiteMessageBoard(path)
    original = event(dedupe_key="stable-domain-key")
    conflicting = DurableEvent(
        schema_version=original.schema_version,
        event_id="evt_conflicting",
        dedupe_key=original.dedupe_key,
        topic=original.topic,
        event_type=original.event_type,
        aggregate_type=original.aggregate_type,
        aggregate_id=original.aggregate_id,
        task_id=original.task_id,
        agent_id=original.agent_id,
        payload={"job_id": "job_1", "status": "failed"},
        created_at=original.created_at,
    )

    await board.publish(original)
    with pytest.raises(MessageBoardDedupeConflict):
        await board.publish(conflicting)

    [stored] = await board.list_events()
    assert stored["payload"] == original.payload


@pytest.mark.asyncio
async def test_sqlite_message_board_binds_dedupe_to_stable_event_id(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await StateService(path).initialize()
    board = SQLiteMessageBoard(path)
    original = event(dedupe_key="stable-event-identity")
    different_identity = DurableEvent(
        schema_version=original.schema_version,
        event_id="evt_same_content_different_identity",
        dedupe_key=original.dedupe_key,
        topic=original.topic,
        event_type=original.event_type,
        aggregate_type=original.aggregate_type,
        aggregate_id=original.aggregate_id,
        task_id=original.task_id,
        agent_id=original.agent_id,
        payload=original.payload,
        created_at=original.created_at,
    )

    await board.publish(original)
    with pytest.raises(MessageBoardDedupeConflict):
        await board.publish(different_identity)
