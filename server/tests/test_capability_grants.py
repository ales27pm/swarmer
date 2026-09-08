from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.approval_binding import canonical_action_digest
from tests.conftest import OPERATOR_TOKEN
from tests.test_worker_protocol import register


def test_capability_digest_matches_mobile_canonical_vectors() -> None:
    request_id = f"iphreq_{'a' * 32}"
    assert (
        canonical_action_digest(
            tool_call_id=request_id,
            tool_name="iphone.mail.compose",
            arguments={
                "recipients": ["a@example.com"],
                "subject": "Hello",
                "body": "Body",
            },
        )
        == "sha256:928d3688233c2de096a0add2152d9747625307590618670406f332b0002a5461"
    )
    assert (
        canonical_action_digest(
            tool_call_id=f"iphreq_{'f' * 32}",
            tool_name="iphone.sms.compose",
            arguments={"recipients": [], "message": f"Allô 👋 — {'x' * 100}"},
        )
        == "sha256:772618f364f73d14ce6002e501776b7db5bb4e2c06867e08abcce48106c179da"
    )


def prepare_leased_job(
    client: TestClient, paired_headers: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str], str]:
    task = client.post("/tasks", headers=paired_headers, json={"input": "locate the phone"}).json()
    dispatched = client.post(
        f"/tasks/{task['id']}/dispatch",
        headers=paired_headers,
        json={"required_skill": "workspace.list_dir", "payload": {"path": "."}},
    )
    assert dispatched.status_code == 201
    agent = register(client, paired_headers, "reader", ["workspace.list_dir"])
    agent_headers = {"Authorization": f"Bearer {agent['credential']}"}
    claim = client.post(f"/agents/{agent['id']}/claim", headers=agent_headers, json={})
    assert claim.status_code == 200
    return {**claim.json(), "agent_id": agent["id"]}, agent_headers, str(task["id"])


def capability_body(lease: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    value = {
        "claim_token": lease["claim_token"],
        "lease_id": lease["lease_id"],
        "lease_generation": lease["lease_generation"],
        "capability_name": "iphone.location.current",
        "arguments": {},
    }
    value.update(overrides)
    return value


def request_capability(
    client: TestClient,
    lease: dict[str, Any],
    agent_headers: dict[str, str],
    **overrides: Any,
) -> dict[str, Any]:
    response = client.post(
        f"/agents/{lease['agent_id']}/jobs/{lease['id']}/capability-requests",
        headers=agent_headers,
        json=capability_body(lease, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_capability_grant_is_one_use_argument_bound_and_hash_only(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    request = request_capability(client, lease, agent_headers)
    request_id = request["request_id"]
    assert request["status"] == "waiting_approval"
    assert request["target_device_id"] == "test-phone"

    detail = client.get(
        f"/iphone/capabilities/requests/{request_id}", headers=paired_headers
    ).json()
    assert detail["arguments"] == {}
    assert detail["grant"] is None
    approval = client.post(
        f"/iphone/capabilities/requests/{request_id}/authorize",
        headers=paired_headers,
        json={"decision": "approve"},
    )
    assert approval.status_code == 200, approval.text
    envelope = approval.json()
    grant = envelope["grant"]
    assert envelope["status"] == "approved"
    assert grant["request_id"] == request_id
    assert grant["target_device_id"] == "test-phone"
    assert grant["use"] == "once"

    with sqlite3.connect(test_app.state.settings.db_path) as db:
        stored = db.execute(
            "SELECT token_hash FROM iphone_capability_grants WHERE request_id=?",
            (request_id,),
        ).fetchone()[0]
    assert stored == f"sha256:{hashlib.sha256(grant['grant_id'].encode()).hexdigest()}"
    assert grant["grant_id"] not in stored

    retried_approval = client.post(
        f"/iphone/capabilities/requests/{request_id}/authorize",
        headers=paired_headers,
        json={"decision": "approve"},
    )
    assert retried_approval.status_code == 200, retried_approval.text
    retried_envelope = retried_approval.json()
    rotated_grant = retried_envelope["grant"]
    assert rotated_grant["grant_id"] != grant["grant_id"]
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        rotated_hash = db.execute(
            "SELECT token_hash FROM iphone_capability_grants WHERE request_id=?",
            (request_id,),
        ).fetchone()[0]
    assert rotated_hash == (
        f"sha256:{hashlib.sha256(rotated_grant['grant_id'].encode()).hexdigest()}"
    )
    stale_grant = client.post(
        f"/iphone/capabilities/requests/{request_id}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": envelope["action_digest"]},
    )
    assert stale_grant.status_code == 409
    grant = rotated_grant

    tampered = client.post(
        f"/iphone/capabilities/requests/{request_id}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": "sha256:" + "0" * 64},
    )
    assert tampered.status_code == 409
    consumed = client.post(
        f"/iphone/capabilities/requests/{request_id}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": envelope["action_digest"]},
    )
    assert consumed.status_code == 200
    assert consumed.json()["status"] == "consumed"
    replay = client.post(
        f"/iphone/capabilities/requests/{request_id}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": envelope["action_digest"]},
    )
    assert replay.status_code == 409


def test_expired_grant_and_wrong_request_are_rejected(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    request = request_capability(client, lease, agent_headers)
    approved = client.post(
        f"/iphone/capabilities/requests/{request['request_id']}/authorize",
        headers=paired_headers,
        json={"decision": "approve"},
    ).json()
    grant = approved["grant"]
    wrong = client.post(
        f"/iphone/capabilities/requests/iphreq_{'0' * 32}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": approved["action_digest"]},
    )
    assert wrong.status_code == 409
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE iphone_capability_grants SET expires_at='2000-01-01T00:00:00+00:00' WHERE request_id=?",
            (request["request_id"],),
        )
    expired = client.post(
        f"/iphone/capabilities/requests/{request['request_id']}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": approved["action_digest"]},
    )
    assert expired.status_code == 409


def test_capability_audit_and_board_redact_arguments(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    secret_marker = "private-recipient@example.com"
    request = request_capability(
        client,
        lease,
        agent_headers,
        capability_name="iphone.mail.compose",
        arguments={"recipients": [secret_marker], "subject": "private", "body": "hidden"},
    )
    assert request["request_id"].startswith("iphreq_")
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        audit = "\n".join(
            row[0]
            for row in db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type LIKE 'iphone.capability.%'"
            )
        )
        board = "\n".join(
            row[0]
            for row in db.execute(
                "SELECT payload_json FROM message_board_events WHERE topic='iphone.capabilities'"
            )
        )
    assert secret_marker not in audit
    assert secret_marker not in board
    assert json.loads(audit.splitlines()[-1])["arguments_redacted"] is True


def test_capability_policy_rejects_extra_nested_arguments(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    response = client.post(
        f"/agents/{lease['agent_id']}/jobs/{lease['id']}/capability-requests",
        headers=agent_headers,
        json=capability_body(lease, arguments={"unexpected": True}),
    )
    assert response.status_code == 422


def test_identical_capability_request_retries_converge_but_different_arguments_do_not(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    service = test_app.state.iphone_capability_service

    async def create_location_requests() -> list[dict[str, Any]]:
        return await asyncio.gather(
            *(
                service.create_request(
                    agent_id=lease["agent_id"],
                    job_id=lease["id"],
                    claim_token=lease["claim_token"],
                    lease_id=lease["lease_id"],
                    lease_generation=lease["lease_generation"],
                    capability_name="iphone.location.current",
                    arguments={},
                )
                for _ in range(6)
            )
        )

    identical = asyncio.run(create_location_requests())
    assert len({record["request_id"] for record in identical}) == 1

    ada = request_capability(
        client,
        lease,
        agent_headers,
        capability_name="iphone.contacts.lookup",
        arguments={"query": "Ada"},
    )
    grace = request_capability(
        client,
        lease,
        agent_headers,
        capability_name="iphone.contacts.lookup",
        arguments={"query": "Grace"},
    )
    assert ada["request_id"] != grace["request_id"]


def test_migration_reconciles_duplicate_nonterminal_request_fingerprints(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    original = request_capability(client, lease, agent_headers)
    request_id = original["request_id"]
    duplicate_id = f"iphreq_{'f' * 32}"
    duplicate_digest = canonical_action_digest(
        tool_call_id=duplicate_id,
        tool_name="iphone.location.current",
        arguments={},
    )
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute("DROP INDEX idx_iphone_capability_one_nonterminal_fingerprint")
        db.execute(
            """
            INSERT INTO iphone_capability_requests(
                id,task_id,requesting_agent_id,requesting_job_id,lease_generation,
                device_id,capability_name,arguments_json,request_fingerprint,
                action_digest,status,approval_id,request_audit_id,created_at,expires_at
            )
            SELECT ?,task_id,requesting_agent_id,requesting_job_id,lease_generation,
                   device_id,capability_name,arguments_json,request_fingerprint,
                   ?,status,?,request_audit_id,?,expires_at
            FROM iphone_capability_requests WHERE id=?
            """,
            (
                duplicate_id,
                duplicate_digest,
                f"icapr_{'f' * 32}",
                "2099-01-01T00:00:00+00:00",
                request_id,
            ),
        )
        db.execute("PRAGMA user_version=9")

    asyncio.run(test_app.state.state_service.initialize())

    with sqlite3.connect(test_app.state.settings.db_path) as db:
        rows = db.execute(
            """
            SELECT id,status,request_fingerprint
            FROM iphone_capability_requests ORDER BY created_at,id
            """
        ).fetchall()
        index = db.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type='index' AND name='idx_iphone_capability_one_nonterminal_fingerprint'
            """
        ).fetchone()
    assert rows[0][0:2] == (request_id, "waiting_approval")
    assert rows[1][0:2] == (duplicate_id, "cancelled")
    assert rows[0][2] == rows[1][2]
    assert index is not None and "WHERE status NOT IN" in str(index[0])


@pytest.mark.parametrize(
    "timestamp",
    ["2026-09-08 12:00:00Z", "2026-09-08T12:00:00"],
)
def test_calendar_capability_rejects_non_rfc3339_timestamps(
    client: TestClient,
    paired_headers: dict[str, str],
    timestamp: str,
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    response = client.post(
        f"/agents/{lease['agent_id']}/jobs/{lease['id']}/capability-requests",
        headers=agent_headers,
        json=capability_body(
            lease,
            capability_name="iphone.calendar.events",
            arguments={"start": timestamp, "end": "2026-09-08T13:00:00Z"},
        ),
    )
    assert response.status_code == 422


def test_capability_policy_rejects_recipient_items_mobile_cannot_accept(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    response = client.post(
        f"/agents/{lease['agent_id']}/jobs/{lease['id']}/capability-requests",
        headers=agent_headers,
        json=capability_body(
            lease,
            capability_name="iphone.mail.compose",
            arguments={"recipients": ["x" * 1_001]},
        ),
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "result",
    [
        {
            "name": "iphone.contacts.lookup",
            "status": "completed",
            "value": [{"id": 7, "name": "Ada", "phoneNumbers": [], "emails": []}],
        },
        {
            "name": "iphone.calendar.events",
            "status": "completed",
            "value": [{"id": "1", "title": "", "start": "not-a-date", "end": "also-bad"}],
        },
        {
            "name": "iphone.calendar.events",
            "status": "completed",
            "value": [
                {
                    "id": "1",
                    "title": "",
                    "start": "2026-09-08 12:00:00Z",
                    "end": "2026-09-08T13:00:00Z",
                }
            ],
        },
        {
            "name": "iphone.calendar.events",
            "status": "completed",
            "value": [
                {
                    "id": "1",
                    "title": "",
                    "start": "2026-09-08T12:00:00",
                    "end": "2026-09-08T13:00:00Z",
                }
            ],
        },
        {
            "name": "iphone.photos.pick",
            "status": "completed",
            "value": {"uri": "file:///photo.jpg", "width": -1, "height": 10},
        },
        {
            "name": "iphone.location.current",
            "status": "denied",
            "reason": "permission_denied",
        },
    ],
)
def test_capability_result_rejects_malformed_native_values(
    client: TestClient, paired_headers: dict[str, str], result: dict[str, Any]
) -> None:
    response = client.post(
        "/iphone/capabilities/requests/iphreq_00000000000000000000000000000000/result",
        headers=paired_headers,
        json={
            "grant_id": "g" * 32,
            "action_digest": "sha256:" + "0" * 64,
            "result": result,
        },
    )

    assert response.status_code == 422


def pair_second_phone(client: TestClient) -> dict[str, str]:
    code = client.post(
        "/pairing/code", headers={"X-Mongars-Operator-Token": OPERATOR_TOKEN}
    ).json()["code"]
    candidate = client.post(
        "/pairing/complete",
        json={"code": code, "device_id": "other-phone", "name": "Other"},
    ).json()
    headers = {"Authorization": f"Bearer {candidate['candidate_token']}"}
    client.post(
        "/pairing/finalize",
        headers=headers,
        json={"pairing_id": candidate["pairing_id"], "device_id": "other-phone"},
    )
    assert client.get("/sync/bootstrap", headers=headers).status_code == 200
    return headers


def test_grant_cannot_be_used_by_another_paired_device(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    request = request_capability(client, lease, agent_headers)
    approved = client.post(
        f"/iphone/capabilities/requests/{request['request_id']}/authorize",
        headers=paired_headers,
        json={"decision": "approve"},
    ).json()
    other = pair_second_phone(client)
    assert (
        client.get(
            f"/iphone/capabilities/requests/{request['request_id']}", headers=other
        ).status_code
        == 404
    )
    use = client.post(
        f"/iphone/capabilities/requests/{request['request_id']}/execute",
        headers=other,
        json={
            "grant_id": approved["grant"]["grant_id"],
            "action_digest": approved["action_digest"],
        },
    )
    assert use.status_code == 409
