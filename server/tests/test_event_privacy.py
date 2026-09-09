from __future__ import annotations

import json
from collections.abc import Callable
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

_SECRET_KEY_VARIANTS = (
    "AUTH",
    "Authori.Zation",
    "bearer-token",
    "x_apiKey",
    "privateKey",
    "PASSWORD",
    "se-cret",
    "grant",
    "claim.token",
    "lease-token",
    "\uff54\uff4f\uff4b\uff45\uff4e",  # full-width token
    "t\u03bfken",  # Greek omicron
    "\u0430uthoriz\u0430tion",  # Cyrillic a
    "\u0455ecret",  # Cyrillic dze
    "pa\u0455\u0455word",  # two Cyrillic dze characters
    "api\u200bkey",  # zero-width separator
    "tok\u00e9n",  # combining-equivalent accent
)

_BOARD_PRIVATE_KEY_VARIANTS = (
    "Email.Body",
    "smsBody",
    "contact-details",
    "GPS-Latitude",
    "geo.coordinates",
    "recipient",
    "ph\u03bfne",  # Greek omicron
)


def _at_root(key: str) -> dict[str, object]:
    return {key: "must-not-escape"}


def _in_object(key: str) -> dict[str, object]:
    return {"safe": {key: "must-not-escape"}}


def _in_array(key: str) -> dict[str, object]:
    return {"safe": [{"deeper": [{key: "must-not-escape"}]}]}


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


@pytest.mark.parametrize("secret_key", _SECRET_KEY_VARIANTS)
@pytest.mark.parametrize(
    "container",
    (
        _at_root,
        _in_object,
        _in_array,
    ),
)
def test_shared_event_contract_rejects_nested_secret_key_aliases(
    secret_key: str,
    container: Callable[[str], dict[str, object]],
) -> None:
    payload = container(secret_key)

    with pytest.raises(EventPrivacyError):
        assert_safe_shared_payload(payload)


@pytest.mark.parametrize("private_key", _BOARD_PRIVATE_KEY_VARIANTS)
def test_shared_event_contract_rejects_board_private_data_aliases(
    private_key: str,
) -> None:
    with pytest.raises(EventPrivacyError):
        assert_safe_shared_payload({"safe": [{"metadata": {private_key: "must-not-escape"}}]})


@pytest.mark.parametrize(
    "secret_text",
    (
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "Basic dXNlcjpwYXNzd29yZA==",
        "api-key=hunter2hunter2",
        "a\u0440iKey: hunter2hunter2",  # Cyrillic p
        "token: abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturevalue",
        "-----BEGIN PRIVATE KEY----- private-material",
        "https://user:password@example.invalid/private",
    ),
)
def test_shared_event_contract_rejects_credential_like_scalar_text(
    secret_text: str,
) -> None:
    with pytest.raises(EventPrivacyError):
        assert_safe_shared_payload({"summary": secret_text})


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


@pytest.mark.parametrize("secret_field", _SECRET_KEY_VARIANTS)
def test_websocket_projection_removes_nested_obfuscated_secret_aliases(
    secret_field: str,
) -> None:
    event = safe_websocket_event(
        {
            "type": "agent.updated",
            "payload": {
                "agent_id": "agt_safe",
                "metadata": [{"safe_label": "worker-a", secret_field: "must-not-escape"}],
            },
        }
    )

    assert event == {
        "type": "agent.updated",
        "payload": {
            "agent_id": "agt_safe",
            "metadata": [{"safe_label": "worker-a"}],
        },
    }


@pytest.mark.parametrize("private_field", _BOARD_PRIVATE_KEY_VARIANTS)
def test_websocket_projection_removes_nested_private_data_aliases(
    private_field: str,
) -> None:
    event = safe_websocket_event(
        {
            "type": "agent.updated",
            "payload": {
                "agent_id": "agt_safe",
                "metadata": [{"safe_label": "worker-a", private_field: "private"}],
            },
        }
    )

    assert event == {
        "type": "agent.updated",
        "payload": {
            "agent_id": "agt_safe",
            "metadata": [{"safe_label": "worker-a"}],
        },
    }


def test_websocket_projection_redacts_nested_credential_text_but_keeps_metadata() -> None:
    event = safe_websocket_event(
        {
            "type": "agent.updated",
            "payload": {
                "agent_id": "agt_safe",
                "status": "online",
                "metadata": {
                    "runtime": "python",
                    "labels": ["read-only", "stable"],
                    "diagnostic": "api-key=hunter2hunter2",
                },
            },
        }
    )

    assert event["payload"]["agent_id"] == "agt_safe"
    assert event["payload"]["status"] == "online"
    assert event["payload"]["metadata"]["runtime"] == "python"
    assert event["payload"]["metadata"]["labels"] == ["read-only", "stable"]
    assert event["payload"]["metadata"]["diagnostic"] == "<redacted sensitive text>"


def test_websocket_projection_rejects_non_boolean_redaction_marker() -> None:
    with pytest.raises(EventPrivacyError):
        safe_websocket_event(
            {
                "type": "agent.updated",
                "payload": {
                    "agent_id": "agt_safe",
                    "arguments_redacted": {"safe_label": "not-a-marker"},
                },
            }
        )


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


def test_task_websocket_event_is_notification_metadata_only() -> None:
    private_input = "Meet the client at the private launch location"
    event = safe_websocket_event(
        {
            "type": "task.updated",
            "payload": {
                "id": "tsk_private",
                "title": private_input,
                "input": private_input,
                "source": "phone-private",
                "conversation_id": "cnv_private",
                "status": "running",
                "mode": "normal",
                "priority": 7,
                "created_at": "2030-01-01T00:00:00+00:00",
                "updated_at": "2030-01-01T00:01:00+00:00",
                "completed_at": None,
                "error_json": {"message": private_input},
            },
        }
    )

    assert event == {
        "type": "task.updated",
        "payload": {
            "id": "tsk_private",
            "conversation_id": "cnv_private",
            "status": "running",
            "created_at": "2030-01-01T00:00:00+00:00",
            "updated_at": "2030-01-01T00:01:00+00:00",
            "completed_at": None,
            "refetch_required": True,
        },
    }
    assert private_input not in json.dumps(event)


def test_message_websocket_event_is_notification_metadata_only() -> None:
    private_content = "My medical appointment is tomorrow at noon"
    event = safe_websocket_event(
        {
            "type": "message.created",
            "payload": {
                "id": "msg_private",
                "conversation_id": "cnv_private",
                "task_id": "tsk_private",
                "role": "user",
                "agent_id": None,
                "content": private_content,
                "metadata": {"verified_status": "conversation_only", "note": private_content},
                "created_at": "2030-01-01T00:00:00+00:00",
            },
        }
    )

    assert event == {
        "type": "message.created",
        "payload": {
            "id": "msg_private",
            "conversation_id": "cnv_private",
            "task_id": "tsk_private",
            "role": "user",
            "agent_id": None,
            "created_at": "2030-01-01T00:00:00+00:00",
            "refetch_required": True,
        },
    }
    assert private_content not in json.dumps(event)


def test_orchestrator_websocket_event_is_notification_metadata_only() -> None:
    private_summary = (
        "Medical contact Alice is at +1-555-0100; text the SMS body and use coordinates 45.5,-73.5."
    )
    event = safe_websocket_event(
        {
            "type": "orchestrator.proposed",
            "payload": {
                "task_id": "tsk_private",
                "planner_source": "ubuntu_local",
                "status": "proposed",
                "tool_name": "none",
                "summary": private_summary,
                "arguments": {
                    "sms_body": private_summary,
                    "latitude": 45.5,
                    "longitude": -73.5,
                },
            },
        }
    )

    assert event == {
        "type": "orchestrator.proposed",
        "payload": {
            "task_id": "tsk_private",
            "planner_source": "ubuntu_local",
            "status": "proposed",
            "refetch_required": True,
        },
    }
    assert private_summary not in json.dumps(event)


@pytest.mark.parametrize("event_type", ("approval.requested", "approval.decided"))
def test_approval_websocket_event_is_notification_metadata_only(event_type: str) -> None:
    private_note = "Approve reading /protected/customer-secrets.txt for Alice"
    event = safe_websocket_event(
        {
            "type": event_type,
            "payload": {
                "id": "apr_private",
                "approval_id": "apr_private",
                "task_id": "tsk_private",
                "tool_call_id": "call_private",
                "status": "approved" if event_type.endswith("decided") else "pending",
                "created_at": "2030-01-01T00:00:00+00:00",
                "expires_at": "2030-01-01T00:01:00+00:00",
                "decided_at": "2030-01-01T00:00:30+00:00",
                "action": "process.run",
                "summary": private_note,
                "user_note": private_note,
                "decision": {
                    "user_note": private_note,
                    "action_snapshot": {"arguments": {"path": "/protected/customer-secrets.txt"}},
                },
            },
        }
    )

    assert event == {
        "type": event_type,
        "payload": {
            "id": "apr_private",
            "approval_id": "apr_private",
            "task_id": "tsk_private",
            "tool_call_id": "call_private",
            "status": "approved" if event_type.endswith("decided") else "pending",
            "created_at": "2030-01-01T00:00:00+00:00",
            "expires_at": "2030-01-01T00:01:00+00:00",
            "decided_at": "2030-01-01T00:00:30+00:00",
            "refetch_required": True,
        },
    }
    encoded = json.dumps(event)
    assert private_note not in encoded
    assert "/protected/customer-secrets.txt" not in encoded


@pytest.mark.parametrize(
    "field",
    (
        "content",
        "messageContent",
        "task_input",
        "user-prompt",
        "private.title",
        "eventDescription",
        "model_summary",
        "operatorNotes",
    ),
)
def test_board_contract_rejects_natural_language_payload_fields(field: str) -> None:
    with pytest.raises(EventPrivacyError):
        assert_safe_shared_payload({field: "private natural-language data"})


def test_board_contract_still_accepts_identifier_metadata() -> None:
    assert_safe_shared_payload(
        {
            "message_id": "msg_safe",
            "task_id": "tsk_safe",
            "status": "queued",
            "updated_at": "2030-01-01T00:00:00+00:00",
        }
    )


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
