from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from app.services.event_privacy import (
    EventPrivacyError,
    assert_safe_shared_payload,
    safe_websocket_event,
)
from app.services.message_board import MessageBoardService
from app.services.outbox import OutboxService
from app.services.state_service import StateService


@pytest.mark.parametrize(
    "payload",
    [
        {"claim_token": "opaque-worker-proof"},
        {"nested": {"grant_id": "one-use-secret"}},
        {"authorization": "Bearer abcdefghijklmnopqrstuvwxyz"},
        {"message": "redis://user:password@example.invalid/0"},
        {"native_result": {"latitude": 1.0, "longitude": 2.0}},
        {"lat": 45.5, "lng": -73.5},
        {"phone": "+1-555-0100"},
        {"body": "message body"},
        {"output": "protected file contents"},
        {"api_key": "must-not-persist"},
        {"api-key": "must-not-persist"},
        {"apiKey": "must-not-persist"},
        {"private_key": "must-not-persist"},
        {"private-key": "must-not-persist"},
        {"privateKey": "must-not-persist"},
        {"session_cookie": "must-not-persist"},
        {"session-id": "must-not-persist"},
        {"sessionId": "must-not-persist"},
    ],
)
def test_shared_event_contract_rejects_credentials_and_native_results(
    payload: dict[str, object],
) -> None:
    with pytest.raises(EventPrivacyError):
        assert_safe_shared_payload(payload)


def test_shared_event_allows_only_true_redaction_markers() -> None:
    assert_safe_shared_payload({"arguments_redacted": True, "result_redacted": True})
    for value in (False, "true", {"raw": "value"}):
        with pytest.raises(EventPrivacyError):
            assert_safe_shared_payload({"arguments_redacted": value})


@pytest.mark.parametrize(
    "secret_field",
    [
        "api_key",
        "api-key",
        "apiKey",
        "private_key",
        "private-key",
        "privateKey",
        "session_id",
        "session-id",
        "sessionId",
        "cookie",
    ],
)
def test_websocket_projection_removes_common_secret_aliases(secret_field: str) -> None:
    event = safe_websocket_event(
        {
            "type": "agent.updated",
            "payload": {"agent_id": "agt_safe", secret_field: "must-not-broadcast"},
        }
    )

    assert event == {"type": "agent.updated", "payload": {"agent_id": "agt_safe"}}


def test_tool_websocket_event_redacts_executor_result_and_credential_text() -> None:
    event = safe_websocket_event(
        {
            "type": "tool.completed",
            "payload": {
                "id": "call_1",
                "tool_name": "workspace.read_text",
                "result": {"text": "private file contents"},
                "summary": "Bearer abcdefghijklmnopqrstuvwxyz",
            },
        }
    )

    assert event["payload"]["result"] == {"result_redacted": True}
    encoded = json.dumps(event)
    assert "private file contents" not in encoded
    assert "abcdefghijklmnopqrstuvwxyz" not in encoded


def test_iphone_websocket_event_keeps_only_delivery_preview() -> None:
    event = safe_websocket_event(
        {
            "type": "iphone.capability.requested",
            "payload": {
                "request_id": "iphreq_1",
                "capability_name": "iphone.location.current",
                "expires_at": "2030-01-01T00:00:00+00:00",
                "preview": {"arguments_redacted": True},
                "result": {"latitude": 45.5, "longitude": -73.5},
                "device_id": "phone-private",
            },
        }
    )

    assert set(event["payload"]) == {
        "request_id",
        "capability_name",
        "expires_at",
        "preview",
    }


@pytest.mark.asyncio
async def test_outbox_rejects_unsafe_event_in_same_domain_transaction(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """INSERT INTO tasks(
                id,title,input,mode,source,status,priority,created_at,updated_at
            ) VALUES('tsk_privacy','privacy','privacy','normal','test','created',0,?,?)""",
            ("2030-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00"),
        )
        with pytest.raises(EventPrivacyError):
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="task",
                aggregate_id="tsk_privacy",
                topic="tasks.status",
                event_type="published",
                payload={"lease_token": "must-not-persist"},
                dedupe_key="privacy:unsafe",
            )
        await db.rollback()

    async with aiosqlite.connect(database) as db:
        task_count = await (await db.execute("SELECT COUNT(*) FROM tasks")).fetchone()
        outbox_count = await (await db.execute("SELECT COUNT(*) FROM outbox_events")).fetchone()
    assert task_count == (0,)
    assert outbox_count == (0,)


@pytest.mark.asyncio
async def test_published_board_envelope_contains_no_forbidden_fields(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    board = MessageBoardService(database)
    async with aiosqlite.connect(database) as db:
        await db.execute("BEGIN IMMEDIATE")
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="agent_job",
            aggregate_id="job_safe",
            topic="tasks.status",
            event_type="acked",
            payload={"job_id": "job_safe", "status": "completed", "lease_generation": 2},
            dedupe_key="job-safe:completed:2",
        )
        await db.commit()
    outbox = OutboxService(database, board, instance_id="privacy-publisher")

    result = await outbox.drain()
    events = await board.list_events()

    assert result["published"] == 1
    encoded = json.dumps(events).casefold()
    for forbidden in (
        "claim_token",
        "lease_token",
        "grant_id",
        "authorization",
        "native_result",
    ):
        assert forbidden not in encoded
