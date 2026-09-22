from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.models import AgentCapabilityRequest, IPhoneCapabilityNativeResult
from tests.test_capability_grants import prepare_leased_job, request_capability

EVENT = {
    "calendar_id": "local",
    "title": "Rendez-vous",
    "start": "2026-10-01T15:00:00Z",
    "end": "2026-10-01T16:00:00Z",
}


def test_extended_agenda_disabled_during_old_client_rollout(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    lease, headers, _ = prepare_leased_job(client, paired_headers)
    result = client.post(
        f"/agents/{lease['agent_id']}/jobs/{lease['id']}/capability-requests",
        headers=headers,
        json={
            "claim_token": lease["claim_token"],
            "lease_id": lease["lease_id"],
            "lease_generation": lease["lease_generation"],
            "capability_name": "iphone.calendar.event.create",
            "arguments": EVENT,
        },
    )
    assert result.status_code == 409
    assert "compatible iPhone" in result.json()["detail"]
    assert client.get("/iphone/capabilities/requests", headers=paired_headers).json() == []


def request(name: str, arguments: dict) -> AgentCapabilityRequest:
    return AgentCapabilityRequest.model_validate(
        {
            "claim_token": "c" * 32,
            "lease_id": "l" * 32,
            "lease_generation": 1,
            "capability_name": name,
            "arguments": arguments,
        }
    )


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("iphone.calendar.calendars", {}),
        ("iphone.calendar.reminders", {"calendar_id": "local"}),
        ("iphone.calendar.event.create", EVENT),
        (
            "iphone.calendar.event.update",
            {"id": "event", **{k: v for k, v in EVENT.items() if k != "calendar_id"}},
        ),
        ("iphone.calendar.reminder.create", {"calendar_id": "local", "title": "Call", "due": None}),
        (
            "iphone.calendar.reminder.update",
            {"id": "reminder", "title": "Call", "due": "2026-10-01T15:00:00Z", "completed": True},
        ),
    ],
)
def test_agenda_requests_require_approval_and_one_use_grants(
    client: TestClient, paired_headers: dict[str, str], name: str, arguments: dict
) -> None:
    client.app.state.iphone_capability_service.extended_agenda_enabled = True
    lease, headers, _ = prepare_leased_job(client, paired_headers)
    pending = request_capability(client, lease, headers, capability_name=name, arguments=arguments)
    assert pending["status"] == "waiting_approval"
    base = f"/iphone/capabilities/requests/{pending['request_id']}"
    detail = client.get(base, headers=paired_headers).json()
    if name == "iphone.calendar.reminder.create":
        assert detail["arguments"]["due"] is None
    approved = client.post(
        f"{base}/authorize", headers=paired_headers, json={"decision": "approve"}
    )
    assert approved.status_code == 200, approved.text
    body = {
        "grant_id": approved.json()["grant"]["grant_id"],
        "action_digest": approved.json()["action_digest"],
    }
    assert client.post(f"{base}/execute", headers=paired_headers, json=body).status_code == 200
    assert client.post(f"{base}/execute", headers=paired_headers, json=body).status_code == 409


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("iphone.calendar.event.create", {**EVENT, "attendees": ["recipient"]}),
        ("iphone.calendar.event.create", {**EVENT, "end": EVENT["start"]}),
        ("iphone.calendar.event.create", {**EVENT, "calendar_id": " local"}),
        (
            "iphone.calendar.reminder.create",
            {"calendar_id": "local", "title": "Call", "due": "2026-10-01T15:00:00.123Z"},
        ),
        (
            "iphone.calendar.reminder.update",
            {"id": "r", "title": "Call", "due": None, "completed": False},
        ),
        (
            "iphone.calendar.reminder.update",
            {"id": "r", "title": "Call", "due": "2026-10-01T15:00:00Z", "completed": "false"},
        ),
    ],
)
def test_agenda_rejects_unsupported_or_ambiguous_arguments(name: str, arguments: dict) -> None:
    with pytest.raises(ValidationError):
        request(name, arguments)


@pytest.mark.parametrize(
    "name,value",
    [
        (
            "iphone.calendar.calendars",
            [
                {
                    "id": "local",
                    "title": "Personal",
                    "entityType": "event",
                    "allowsModifications": True,
                }
            ],
        ),
        (
            "iphone.calendar.reminders",
            [{"id": "r", "title": "Call", "due": None, "completed": False}],
        ),
        (
            "iphone.calendar.event.create",
            {"id": "event", "title": "Call", "start": EVENT["start"], "end": EVENT["end"]},
        ),
        (
            "iphone.calendar.reminder.update",
            {"id": "r", "title": "Call", "due": EVENT["start"], "completed": True},
        ),
    ],
)
def test_agenda_readback_receipts_require_exact_native_fields(name: str, value: object) -> None:
    assert (
        IPhoneCapabilityNativeResult.model_validate(
            {"name": name, "status": "completed", "value": value}
        ).value
        == value
    )
    with pytest.raises(ValidationError):
        IPhoneCapabilityNativeResult.model_validate(
            {"name": name, "status": "completed", "value": {"saved": True}}
        )


def test_remote_dispatch_preserves_explicit_no_due_date() -> None:
    from app.services.remote_job_policy import validate_remote_job

    payload = validate_remote_job(
        "workspace.list_dir",
        {
            "path": ".",
            "capability_request": {
                "capability_name": "iphone.calendar.reminder.create",
                "arguments": {"calendar_id": "local", "title": "Call", "due": None},
            },
        },
    )
    assert payload["capability_request"]["arguments"]["due"] is None
