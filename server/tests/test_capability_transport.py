from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from tests.test_capability_grants import (
    capability_body,
    prepare_leased_job,
    request_capability,
)


def test_worker_waits_for_phone_result_and_result_is_idempotent(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    lease, agent_headers, task_id = prepare_leased_job(client, paired_headers)
    capability = request_capability(client, lease, agent_headers)
    request_id = capability["request_id"]
    poll_path = (
        f"/agents/{lease['agent_id']}/jobs/{lease['id']}/capability-requests/{request_id}/poll"
    )
    proof = {
        "claim_token": lease["claim_token"],
        "lease_id": lease["lease_id"],
        "lease_generation": lease["lease_generation"],
    }
    waiting = client.post(poll_path, headers=agent_headers, json=proof)
    assert waiting.status_code == 200
    assert waiting.json()["status"] == "waiting_approval"

    envelope = client.post(
        f"/iphone/capabilities/requests/{request_id}/authorize",
        headers=paired_headers,
        json={"decision": "approve"},
    ).json()
    grant = envelope["grant"]
    consume = client.post(
        f"/iphone/capabilities/requests/{request_id}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": envelope["action_digest"]},
    )
    assert consume.status_code == 200
    result_body: dict[str, Any] = {
        "grant_id": grant["grant_id"],
        "action_digest": envelope["action_digest"],
        "result": {
            "name": "iphone.location.current",
            "status": "completed",
            "value": {"latitude": 45.5, "longitude": -73.6, "accuracy": 10},
        },
    }
    first = client.post(
        f"/iphone/capabilities/requests/{request_id}/result",
        headers=paired_headers,
        json=result_body,
    )
    duplicate = client.post(
        f"/iphone/capabilities/requests/{request_id}/result",
        headers=paired_headers,
        json=result_body,
    )
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert duplicate.status_code == 200 and duplicate.json()["status"] == "duplicate"

    completed = client.post(poll_path, headers=agent_headers, json=proof)
    assert completed.status_code == 200
    assert completed.json()["result"] == result_body["result"]
    job_result = client.post(
        f"/agents/{lease['agent_id']}/jobs/{lease['id']}/result",
        headers=agent_headers,
        json={**proof, "status": "completed", "result": {"phone": "received"}},
    )
    assert job_result.status_code == 200
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE agent_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (lease["id"],),
        )
    terminal_duplicate = client.post(
        f"/iphone/capabilities/requests/{request_id}/result",
        headers=paired_headers,
        json=result_body,
    )
    assert terminal_duplicate.status_code == 200
    assert terminal_duplicate.json()["status"] == "duplicate"
    assert client.get(f"/tasks/{task_id}", headers=paired_headers).json()["task"]["status"] == (
        "completed"
    )


def test_stale_lease_cannot_request_or_poll_phone_capability(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    stale = capability_body(lease, lease_generation=lease["lease_generation"] + 1)
    create = client.post(
        f"/agents/{lease['agent_id']}/jobs/{lease['id']}/capability-requests",
        headers=agent_headers,
        json=stale,
    )
    assert create.status_code == 409


def test_pending_phone_request_blocks_worker_completion(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    request_capability(client, lease, agent_headers)
    result = client.post(
        f"/agents/{lease['agent_id']}/jobs/{lease['id']}/result",
        headers=agent_headers,
        json={
            "claim_token": lease["claim_token"],
            "lease_id": lease["lease_id"],
            "lease_generation": lease["lease_generation"],
            "status": "completed",
            "result": {},
        },
    )
    assert result.status_code == 409
    assert "capability request is pending" in result.json()["detail"]


def test_consumed_request_expires_if_phone_never_delivers_result(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    capability = request_capability(client, lease, agent_headers)
    request_id = capability["request_id"]
    envelope = client.post(
        f"/iphone/capabilities/requests/{request_id}/authorize",
        headers=paired_headers,
        json={"decision": "approve"},
    ).json()
    grant = envelope["grant"]
    consumed = client.post(
        f"/iphone/capabilities/requests/{request_id}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": envelope["action_digest"]},
    )
    assert consumed.status_code == 200
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE iphone_capability_requests SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (request_id,),
        )

    late = client.post(
        f"/iphone/capabilities/requests/{request_id}/result",
        headers=paired_headers,
        json={
            "grant_id": grant["grant_id"],
            "action_digest": envelope["action_digest"],
            "result": {
                "name": "iphone.location.current",
                "status": "completed",
                "value": {"latitude": 45.5, "longitude": -73.6, "accuracy": 10},
            },
        },
    )

    assert late.status_code == 409
    detail = client.get(
        f"/iphone/capabilities/requests/{request_id}", headers=paired_headers
    ).json()
    assert detail["status"] == "expired"


def test_expired_requesting_lease_rejects_first_phone_result(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    capability = request_capability(client, lease, agent_headers)
    request_id = capability["request_id"]
    envelope = client.post(
        f"/iphone/capabilities/requests/{request_id}/authorize",
        headers=paired_headers,
        json={"decision": "approve"},
    ).json()
    grant = envelope["grant"]
    consumed = client.post(
        f"/iphone/capabilities/requests/{request_id}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": envelope["action_digest"]},
    )
    assert consumed.status_code == 200
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE agent_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (lease["id"],),
        )

    result = client.post(
        f"/iphone/capabilities/requests/{request_id}/result",
        headers=paired_headers,
        json={
            "grant_id": grant["grant_id"],
            "action_digest": envelope["action_digest"],
            "result": {
                "name": "iphone.location.current",
                "status": "completed",
                "value": {"latitude": 45.5, "longitude": -73.6, "accuracy": 10},
            },
        },
    )

    assert result.status_code == 409
    detail = client.get(
        f"/iphone/capabilities/requests/{request_id}", headers=paired_headers
    ).json()
    assert detail["status"] == "cancelled"


def test_parent_cancellation_fences_capability_grant(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    lease, agent_headers, task_id = prepare_leased_job(client, paired_headers)
    capability = request_capability(client, lease, agent_headers)
    request_id = capability["request_id"]
    envelope = client.post(
        f"/iphone/capabilities/requests/{request_id}/authorize",
        headers=paired_headers,
        json={"decision": "approve"},
    ).json()
    grant = envelope["grant"]

    cancelled = client.post(f"/tasks/{task_id}/cancel", headers=paired_headers)

    assert cancelled.status_code == 200
    execute = client.post(
        f"/iphone/capabilities/requests/{request_id}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": envelope["action_digest"]},
    )
    assert execute.status_code == 409
    detail = client.get(
        f"/iphone/capabilities/requests/{request_id}", headers=paired_headers
    ).json()
    assert detail["status"] == "cancelled"


def test_capability_websocket_notification_is_targeted_and_redacted(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    ticket = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]
    with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "connected"
        capability = request_capability(
            client,
            lease,
            agent_headers,
            capability_name="iphone.mail.compose",
            arguments={
                "recipients": ["private@example.com"],
                "subject": "private",
                "body": "hidden",
            },
        )
        event = websocket.receive_json()

    assert event == {
        "type": "iphone.capability.requested",
        "payload": {
            "request_id": capability["request_id"],
            "capability_name": "iphone.mail.compose",
            "expires_at": capability["expires_at"],
            "preview": {"arguments_redacted": True},
        },
    }
    assert "private@example.com" not in str(event)
