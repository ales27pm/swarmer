from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.mark.parametrize(
    ("declared", "last_seen", "timeout", "expected"),
    [
        *[
            (status, NOW.isoformat(), 90, status)
            for status in ("online", "busy", "draining", "offline", "unverified")
        ],
        *[
            (status, (NOW - timedelta(days=2)).isoformat(), 90, "offline")
            for status in ("online", "busy", "draining")
        ],
        ("unverified", None, 90, "unverified"),
        ("offline", None, 90, "offline"),
        ("online", None, 90, "offline"),
        ("online", "invalid", 90, "offline"),
        ("online", "2030-01-01T12:00:00", 90, "offline"),
        ("online", (NOW + timedelta(seconds=1)).isoformat(), 90, "offline"),
        ("online", (NOW - timedelta(seconds=90)).isoformat(), 90, "offline"),
        ("busy", "2030-01-01T06:59:59-05:00", 90, "busy"),
        ("online", (NOW - timedelta(seconds=60)).isoformat(), 30, "offline"),
        ("online", (NOW - timedelta(seconds=120)).isoformat(), 180, "online"),
    ],
)
def test_registry_projects_liveness_without_changing_persisted_agent(
    client: TestClient,
    test_app: FastAPI,
    paired_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    declared: str,
    last_seen: str | None,
    timeout: int,
    expected: str,
) -> None:
    registered = client.post(
        "/agents/register",
        headers=paired_headers,
        json={
            "name": "Registry worker",
            "endpoint": "https://worker.invalid",
            "skills": ["code.build_project"],
        },
    )
    assert registered.status_code == 201
    agent = registered.json()
    assert agent["status"] == "unverified"
    agent_id = agent["id"]
    test_app.state.settings.agent_offline_timeout_seconds = timeout
    # Liveness uses the canonical last_seen_at, not a newer display timestamp.
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE agents SET status=?, last_seen_at=?, last_heartbeat_at=? WHERE id=?",
            (declared, last_seen, NOW.isoformat(), agent_id),
        )
        before = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        jobs_before = db.execute("SELECT COUNT(*) FROM agent_jobs").fetchone()
    monkeypatch.setattr("app.main.datetime", FixedDatetime)

    listed = client.get("/agents", headers=paired_headers)
    detail = client.get(f"/agents/{agent_id}", headers=paired_headers)

    assert listed.status_code == detail.status_code == 200
    assert listed.json() == [detail.json()]
    assert detail.json()["status"] == expected
    assert detail.json()["last_seen_at"] == last_seen
    assert detail.json()["last_heartbeat_at"] == NOW.isoformat()
    assert detail.json()["agent_card"] == agent["agent_card"]
    assert "credential" not in detail.json() and "auth_token_hash" not in detail.json()
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone() == before
        assert db.execute("SELECT COUNT(*) FROM agent_jobs").fetchone() == jobs_before
    # Internal consumers retain the declared state; only the HTTP reads project it.
    raw = asyncio.run(test_app.state.state_service.get_agent(agent_id))
    assert raw["status"] == declared
    assert asyncio.run(test_app.state.state_service.list_agents())[0]["status"] == declared


def test_registry_retains_authentication_and_unknown_agent_responses(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    assert client.get("/agents").status_code == 401
    assert client.get("/agents/missing").status_code == 401
    assert client.get("/agents/missing", headers=paired_headers).status_code == 404
