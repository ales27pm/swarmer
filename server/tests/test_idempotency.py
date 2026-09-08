from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def headers(paired_headers: dict[str, str], key: str) -> dict[str, str]:
    return {**paired_headers, "Idempotency-Key": key}


def test_feedback_duplicate_post_returns_one_authoritative_record(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    key = "mut_feedback_12345678901234567890"
    body = {"operation": "feedback.create", "payload": {"score": 5, "notes": "useful"}}

    first = client.post("/sync/mutations", headers=headers(paired_headers, key), json=body)
    replay = client.post("/sync/mutations", headers=headers(paired_headers, key), json=body)

    assert first.status_code == replay.status_code == 201
    assert first.json()["idempotent_replay"] is False
    assert replay.json()["idempotent_replay"] is True
    assert first.json()["result"] == replay.json()["result"]
    database = Path(test_app.state.settings.db_path)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_events").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM idempotency_receipts").fetchone() == (1,)


def test_idempotency_key_is_bound_to_exact_operation_and_payload(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    key = "mut_binding_123456789012345678901"
    accepted = client.post(
        "/sync/mutations",
        headers=headers(paired_headers, key),
        json={"operation": "feedback.create", "payload": {"score": 4}},
    )
    changed = client.post(
        "/sync/mutations",
        headers=headers(paired_headers, key),
        json={"operation": "feedback.create", "payload": {"score": 1}},
    )
    changed_operation = client.post(
        "/sync/mutations",
        headers=headers(paired_headers, key),
        json={
            "operation": "chat.message.create",
            "payload": {"content": "hello", "start_task": False},
        },
    )

    assert accepted.status_code == 201
    assert changed.status_code == changed_operation.status_code == 409


def test_safe_mutation_contract_rejects_missing_key_extra_fields_and_sensitive_actions(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    missing = client.post(
        "/sync/mutations",
        headers=paired_headers,
        json={"operation": "feedback.create", "payload": {"score": 5}},
    )
    extra = client.post(
        "/sync/mutations",
        headers=headers(paired_headers, "mut_extra_12345678901234567890123"),
        json={
            "operation": "feedback.create",
            "payload": {"score": 5},
            "grant_token": "forbidden",
        },
    )
    sensitive = client.post(
        "/sync/mutations",
        headers=headers(paired_headers, "mut_sensitive_12345678901234567890"),
        json={"operation": "iphone.location.current", "payload": {}},
    )
    task_creation = client.post(
        "/sync/mutations",
        headers=headers(paired_headers, "mut_task_chat_12345678901234567890"),
        json={
            "operation": "chat.message.create",
            "payload": {"content": "run it", "start_task": True},
        },
    )

    assert missing.status_code == 422
    assert extra.status_code == sensitive.status_code == task_creation.status_code == 422


def test_memory_metadata_mutation_only_updates_pinned(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    memory = client.post(
        "/memory",
        headers=paired_headers,
        json={"content": "authoritative content", "pinned": False},
    ).json()
    key = "mut_memory_12345678901234567890123"
    response = client.post(
        "/sync/mutations",
        headers=headers(paired_headers, key),
        json={
            "operation": "memory.metadata.update",
            "resource_id": memory["id"],
            "payload": {"pinned": True},
        },
    )
    content_change = client.post(
        "/sync/mutations",
        headers=headers(paired_headers, "mut_memory_content_1234567890123456"),
        json={
            "operation": "memory.metadata.update",
            "resource_id": memory["id"],
            "payload": {"content": "changed"},
        },
    )

    assert response.status_code == 201
    assert response.json()["result"]["pinned"] is True
    assert response.json()["result"]["content"] == "authoritative content"
    assert content_change.status_code == 422


def test_offline_chat_replay_persists_one_message_and_never_starts_task(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    key = "mut_chat_1234567890123456789012345"
    body = {
        "operation": "chat.message.create",
        "resource_id": "cnv_offline_test",
        "payload": {
            "content": "saved while offline",
            "conversation_id": "cnv_offline_test",
            "start_task": False,
        },
    }
    first = client.post("/sync/mutations", headers=headers(paired_headers, key), json=body)
    replay = client.post("/sync/mutations", headers=headers(paired_headers, key), json=body)

    assert first.status_code == replay.status_code == 201
    assert first.json()["result"]["task"] is None
    assert replay.json()["result"] == first.json()["result"]
    database = Path(test_app.state.settings.db_path)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
