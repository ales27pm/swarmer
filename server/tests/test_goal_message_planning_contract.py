from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.parametrize("planning_mode", [None, "automatic", "iphone_local"])
def test_goal_reply_preserves_explicit_planner_choice_at_http_boundary(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    planning_mode: str | None,
) -> None:
    created = client.post(
        "/goals", headers=paired_headers, json={"objective": "Extend the project"}
    )
    assert created.status_code == 201
    detail = created.json()
    reply = AsyncMock(return_value=detail)
    monkeypatch.setattr(test_app.state.goal_manager, "reply_goal", reply)
    body: dict[str, Any] = {"message": "Add exports", "client_message_id": "phone-next-plan"}
    if planning_mode is not None:
        body["planning_mode"] = planning_mode
    response = client.post(
        f"/goals/{detail['goal']['id']}/messages", headers=paired_headers, json=body
    )
    assert response.status_code == 200
    reply.assert_awaited_once()
    request = reply.await_args.args[1]
    assert request.planning_mode == (planning_mode or "automatic")
    assert request.message == body["message"]
    assert request.client_message_id == body["client_message_id"]
    assert reply.await_args.kwargs["actor_id"] == "test-phone"


@pytest.mark.parametrize("planning_mode", [None, True, "", "manual", "ubuntu_local"])
def test_goal_reply_rejects_invalid_planner_choice_before_any_reply_mutation(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    planning_mode: object,
) -> None:
    reply = AsyncMock()
    monkeypatch.setattr(test_app.state.goal_manager, "reply_goal", reply)
    response = client.post(
        "/goals/goal_contract/messages",
        headers=paired_headers,
        json={
            "message": "Add exports",
            "client_message_id": "phone-next-plan",
            "planning_mode": planning_mode,
        },
    )
    assert response.status_code == 422
    reply.assert_not_awaited()


def test_conversation_project_id_follows_active_goal_without_creating_a_project(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    original = client.post(
        "/goals", headers=paired_headers, json={"objective": "Original goal"}
    ).json()["goal"]["id"]
    active = client.post(
        "/goals", headers=paired_headers, json={"objective": "Current goal"}
    ).json()["goal"]["id"]
    endpoint = f"/goals/{original}/messages"
    before = client.get(endpoint, headers=paired_headers)
    assert before.status_code == 200 and before.json()["project_id"] is None
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM coding_projects").fetchone() == (0,)
    assert client.portal is not None
    project_id = client.portal.call(
        test_app.state.goal_manager.project_applications.ensure_project, active
    )
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE goal_conversations SET active_goal_id=? WHERE id=(SELECT conversation_id FROM goal_conversation_links WHERE goal_run_id=?)",
            (active, original),
        )
    current = client.get(endpoint, headers=paired_headers)
    assert current.status_code == 200
    assert current.json()["active_goal_id"] == active
    assert current.json()["project_id"] == project_id
    assert current.headers["cache-control"] == "no-store"
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM coding_projects").fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM goal_project_links WHERE goal_run_id=?", (original,)
        ).fetchone() == (0,)
