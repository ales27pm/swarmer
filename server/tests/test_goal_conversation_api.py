from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_goal_reply_http_is_private_durable_and_idempotent(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    created = client.post(
        "/goals", headers=paired_headers, json={"objective": "Build customer management"}
    )
    assert created.status_code == 201
    goal_id = created.json()["goal"]["id"]
    endpoint = f"/goals/{goal_id}/messages"
    assert client.get(endpoint).status_code == 401
    body = {
        "message": "Private CRM requirements: team sharing",
        "client_message_id": "phone-command-1",
        "reply_to_message_id": None,
    }
    first = client.post(endpoint, headers=paired_headers, json=body)
    replay = client.post(endpoint, headers=paired_headers, json=body)
    assert first.status_code == replay.status_code == 200
    messages = client.get(endpoint, headers=paired_headers)
    assert messages.status_code == 200 and messages.headers["cache-control"] == "no-store"
    assert len(messages.json()["messages"]) == 2
    assert messages.json()["messages"][-1]["content"] == body["message"]
    assert messages.json()["active_goal_id"] == goal_id
    assert messages.json()["pending_question_id"] is None
    assert body["message"] not in client.get("/sync/bootstrap", headers=paired_headers).text
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM goal_messages WHERE client_message_id=?",
            (body["client_message_id"],),
        ).fetchone() == (1,)
    changed = client.post(
        endpoint, headers=paired_headers, json={**body, "message": "Other content"}
    )
    assert changed.status_code == 409


def test_terminal_reply_http_returns_new_run_and_shared_conversation(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    created = client.post(
        "/goals",
        headers=paired_headers,
        json={"objective": "Build a project", "max_model_calls": 3},
    )
    goal_id = created.json()["goal"]["id"]
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE goal_runs SET status='budget_exhausted',model_call_count=3 WHERE id=?",
            (goal_id,),
        )
    response = client.post(
        f"/goals/{goal_id}/messages",
        headers=paired_headers,
        json={"message": "Continue with search", "client_message_id": "continue-1"},
    )
    assert response.status_code == 200, response.text
    new_id = response.json()["goal"]["id"]
    assert new_id != goal_id
    assert response.json()["goal"]["model_call_count"] == 0
    assert (
        client.get(f"/goals/{goal_id}", headers=paired_headers).json()["goal"]["model_call_count"]
        == 3
    )
    assert (
        client.get(f"/goals/{new_id}/messages", headers=paired_headers).json()
        == client.get(f"/goals/{goal_id}/messages", headers=paired_headers).json()
    )


def test_project_apply_http_maps_execution_record_to_application_contract(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    monkeypatch: Any,
) -> None:
    created = client.post("/goals", headers=paired_headers, json={"objective": "Build a project"})
    goal_id = created.json()["goal"]["id"]
    assert client.get(f"/goals/{goal_id}/project", headers=paired_headers).status_code == 404
    apply = AsyncMock(
        return_value={"id": "tc_project", "task_id": "tsk_project", "approval_id": "apr_project"}
    )
    monkeypatch.setattr(test_app.state.goal_manager.project_applications, "apply", apply)
    response = client.post(
        f"/goals/{goal_id}/project/apply",
        headers=paired_headers,
        json={"revision_id": "revision_one", "sha256": "a" * 64},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "task_id": "tsk_project",
        "tool_call_id": "tc_project",
        "approval_id": "apr_project",
    }
    assert apply.await_args.kwargs["requester"].id
