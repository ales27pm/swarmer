from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_capability_grants import prepare_leased_job, request_capability


def test_lost_safe_mutation_response_replays_one_authoritative_write(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    """Dropping the first response must not duplicate a safe mutation."""

    idempotency_key = "chaos_response_lost_12345678901234567890"
    headers = {**paired_headers, "Idempotency-Key": idempotency_key}
    body = {
        "operation": "chat.message.create",
        "resource_id": "cnv_chaos_response_lost",
        "payload": {
            "content": "persist exactly once despite a lost response",
            "conversation_id": "cnv_chaos_response_lost",
            "start_task": False,
        },
    }

    lost_response = client.post("/sync/mutations", headers=headers, json=body)
    assert lost_response.status_code == 201
    # The caller intentionally treats the response as lost and retries with the
    # same application idempotency key.
    retry = client.post("/sync/mutations", headers=headers, json=body)

    assert retry.status_code == 201
    assert retry.json()["idempotent_replay"] is True
    assert retry.json()["result"] == lost_response.json()["result"]
    with sqlite3.connect(Path(test_app.state.settings.db_path)) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
            ("cnv_chaos_response_lost",),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM idempotency_receipts WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone() == (1,)


def test_lost_capability_result_response_is_duplicate_not_native_replay(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    """An uncertain result POST never creates another grant or native effect."""

    lease, agent_headers, _ = prepare_leased_job(client, paired_headers)
    capability = request_capability(client, lease, agent_headers)
    request_id = str(capability["request_id"])
    approved = client.post(
        f"/iphone/capabilities/requests/{request_id}/authorize",
        headers=paired_headers,
        json={"decision": "approve"},
    )
    assert approved.status_code == 200
    envelope = approved.json()
    grant = envelope["grant"]
    consumed = client.post(
        f"/iphone/capabilities/requests/{request_id}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": envelope["action_digest"]},
    )
    assert consumed.status_code == 200

    result: dict[str, Any] = {
        "grant_id": grant["grant_id"],
        "action_digest": envelope["action_digest"],
        "result": {
            "name": "iphone.location.current",
            "status": "completed",
            "value": {"latitude": 45.5, "longitude": -73.6, "accuracy": 10},
        },
    }
    lost_response = client.post(
        f"/iphone/capabilities/requests/{request_id}/result",
        headers=paired_headers,
        json=result,
    )
    assert lost_response.status_code == 200

    duplicate = client.post(
        f"/iphone/capabilities/requests/{request_id}/result",
        headers=paired_headers,
        json=result,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"

    replay_native_execution = client.post(
        f"/iphone/capabilities/requests/{request_id}/execute",
        headers=paired_headers,
        json={"grant_id": grant["grant_id"], "action_digest": envelope["action_digest"]},
    )
    assert replay_native_execution.status_code == 409
    with sqlite3.connect(Path(test_app.state.settings.db_path)) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM iphone_capability_results WHERE request_id=?",
            (request_id,),
        ).fetchone() == (1,)
        grant_state = db.execute(
            "SELECT consumed_at FROM iphone_capability_grants WHERE request_id=?",
            (request_id,),
        ).fetchone()
    assert grant_state is not None and grant_state[0] is not None
