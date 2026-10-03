"""Public, SQL-backed transport negotiation: no models or worker processes."""

from __future__ import annotations

import json
import socket
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.services.agent_scheduler import SchedulerService
from app.services.memory_symbolic_contracts import SymbolicCatalog


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("capability tests must not connect to a provider")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def register(client, headers, *, name="reader", capacity=None):
    response = client.post(
        "/agents/register",
        headers=headers,
        json={
            "name": name,
            "endpoint": "http://127.0.0.1",
            "skills": ["workspace.list_dir"],
            "capacity": capacity or {},
        },
    )
    assert response.status_code == 201, response.text
    agent = response.json()
    auth = {"Authorization": "Bearer " + agent["credential"]}
    assert (
        client.post(
            f"/agents/{agent['id']}/heartbeat", headers=auth, json={"status": "online"}
        ).status_code
        == 200
    )
    return agent, auth


def queued(client, headers, app, *, symbolic=True, status="available"):
    dispatcher = app.state.agent_dispatcher
    dispatcher.symbolic_catalogs = (
        (SymbolicCatalog(namespace="software", scheme_id="engineering"),) if symbolic else ()
    )
    task = client.post("/tasks", headers=headers, json={"input": "Inspect cache safely"})
    assert task.status_code == 201, task.text
    response = client.post(
        f"/tasks/{task.json()['id']}/dispatch",
        headers=headers,
        json={"required_skill": "workspace.list_dir", "payload": {"path": "."}},
    )
    assert response.status_code == 201, response.text
    job = response.json()
    if symbolic:
        assert job["payload"]["symbolic_context"]["evidence"] == []
        if status != "available":
            job["payload"]["symbolic_context"]["status"] = status
            with sqlite3.connect(app.state.state_service.db_path) as db:
                db.execute(
                    "UPDATE agent_jobs SET payload_json=? WHERE id=?",
                    (json.dumps(job["payload"]), job["id"]),
                )
    return job


def claim(client, agent, auth, protocols=None):
    body = {"wait_seconds": 0}
    if protocols is not None:
        body["context_protocols"] = protocols
    return client.post(f"/agents/{agent['id']}/claim", headers=auth, json=body)


def effects(app):
    with sqlite3.connect(app.state.state_service.db_path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("agent_jobs", "tasks", "scheduler_decisions", "audit_events")
        }


@pytest.mark.parametrize("status", ["available", "omitted_budget"])
def test_legacy_claim_cannot_lease_symbolic_envelope(client, paired_headers, test_app, status):
    agent, auth = register(client, paired_headers)
    queued(client, paired_headers, test_app, status=status)
    before = effects(test_app)
    response = claim(client, agent, auth)
    assert response.status_code == 200, response.text
    assert response.json() is None
    assert effects(test_app) == before


@pytest.mark.parametrize("protocols", [None, [], ["symbolic-v1"]])
def test_legacy_job_does_not_require_new_capability(client, paired_headers, test_app, protocols):
    agent, auth = register(client, paired_headers)
    job = queued(client, paired_headers, test_app, symbolic=False)
    response = claim(client, agent, auth, protocols)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == job["id"]


@pytest.mark.parametrize("status", ["available", "omitted_budget"])
def test_registered_capability_and_this_claim_are_both_required(
    client, paired_headers, test_app, status
):
    legacy, legacy_auth = register(client, paired_headers, name="legacy")
    capable, capable_auth = register(
        client, paired_headers, name="capable", capacity={"symbolic_context_version": 1}
    )
    assert capable["agent_card"]["capabilities"]["symbolic_context_version"] == 1
    job = queued(client, paired_headers, test_app, status=status)
    before = effects(test_app)
    # A declaration by the running process cannot approve its own SQL capability.
    assert claim(client, legacy, legacy_auth, ["symbolic-v1"]).json() is None
    assert claim(client, capable, capable_auth).json() is None
    assert effects(test_app) == before
    response = claim(client, capable, capable_auth, ["symbolic-v1"])
    assert response.status_code == 200, response.text
    assert response.json()["id"] == job["id"]
    assert response.json()["payload"]["symbolic_context"]["status"] == status


@pytest.mark.parametrize("value", [True, False, "1", 1.0, 0, 2, None])
def test_public_registration_requires_exact_integer_capability(client, paired_headers, value):
    response = client.post(
        "/agents/register",
        headers=paired_headers,
        json={
            "name": "invalid",
            "endpoint": "http://127.0.0.1",
            "skills": ["workspace.list_dir"],
            "capacity": {"symbolic_context_version": value},
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "protocols", [None, "symbolic-v1", ["symbolic-v2"], ["symbolic-v1", "symbolic-v1"], [True]]
)
def test_public_claim_rejects_malformed_protocols_without_effects(
    client, paired_headers, test_app, protocols
):
    agent, auth = register(client, paired_headers)
    queued(client, paired_headers, test_app)
    before = effects(test_app)
    response = client.post(
        f"/agents/{agent['id']}/claim", headers=auth, json={"context_protocols": protocols}
    )
    assert response.status_code == 422, response.text
    assert effects(test_app) == before


@pytest.mark.parametrize(
    "change", ["legacy_empty", "explicit_empty", "ttl", "future", "restart", "revoked"]
)
def test_previously_capable_identity_cannot_indefinitely_outrank_current_client(
    client, paired_headers, test_app, change
):
    old, old_auth = register(
        client, paired_headers, name="previous binary", capacity={"symbolic_context_version": 1}
    )
    current, current_auth = register(
        client, paired_headers, name="current binary", capacity={"symbolic_context_version": 1}
    )
    dispatcher = test_app.state.agent_dispatcher
    now = [datetime.now(UTC) + timedelta(seconds=2)]
    dispatcher.clock = lambda: now[0]
    dispatcher.scheduler.clock = dispatcher.clock
    dispatcher.scheduler.offline_timeout_seconds = 300
    with sqlite3.connect(test_app.state.state_service.db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO agent_scores(agent_id,score,sample_count,updated_at) VALUES(?,?,1,'fixture')",
            (old["id"], 10),
        )
    # Only a genuine authenticated claim establishes volatile readiness.
    assert claim(client, old, old_auth, ["symbolic-v1"]).json() is None
    job = queued(client, paired_headers, test_app)
    before = effects(test_app)
    assert claim(client, current, current_auth, ["symbolic-v1"]).json() is None
    assert effects(test_app) == before
    if change in {"legacy_empty", "explicit_empty"}:
        protocols = [] if change == "explicit_empty" else None
        assert claim(client, old, old_auth, protocols).json() is None
    elif change == "ttl":
        now[0] += timedelta(seconds=30)
    elif change == "future":
        now[0] -= timedelta(seconds=1)
    elif change == "restart":
        dispatcher.scheduler = SchedulerService(
            dispatcher.db_path,
            offline_timeout_seconds=300,
            permission_policy=dispatcher.permission_policy,
            clock=dispatcher.clock,
        )
    else:
        with sqlite3.connect(test_app.state.state_service.db_path) as db:
            db.execute("UPDATE agents SET capacity_json='{}' WHERE id=?", (old["id"],))
    assert effects(test_app) == before
    response = claim(client, current, current_auth, ["symbolic-v1"])
    assert response.status_code == 200, response.text
    assert response.json()["id"] == job["id"]
    with sqlite3.connect(test_app.state.state_service.db_path) as db:
        candidates, scoring = db.execute(
            "SELECT candidates_json,scoring_json FROM scheduler_decisions WHERE job_id=?",
            (job["id"],),
        ).fetchone()
    assert [a["agent_id"] for a in json.loads(candidates)] == [current["id"]]
    assert json.loads(scoring)["required_context_protocol"] == "symbolic-v1"


def test_unseen_approved_peer_never_blocks_first_compatible_claim(client, paired_headers, test_app):
    old, _ = register(
        client, paired_headers, name="silent old binary", capacity={"symbolic_context_version": 1}
    )
    current, auth = register(
        client, paired_headers, name="ready", capacity={"symbolic_context_version": 1}
    )
    with sqlite3.connect(test_app.state.state_service.db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO agent_scores(agent_id,score,sample_count,updated_at) VALUES(?,?,1,'fixture')",
            (old["id"], 10),
        )
    job = queued(client, paired_headers, test_app)
    response = claim(client, current, auth, ["symbolic-v1"])
    assert response.status_code == 200, response.text
    assert response.json()["id"] == job["id"]


def test_unsupported_envelopes_do_not_fill_legacy_candidate_page(client, paired_headers, test_app):
    agent, auth = register(client, paired_headers)
    for _ in range(test_app.state.agent_dispatcher.CLAIM_CANDIDATE_LIMIT + 1):
        queued(client, paired_headers, test_app)
    legacy = queued(client, paired_headers, test_app, symbolic=False)
    response = claim(client, agent, auth)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == legacy["id"]
    with sqlite3.connect(test_app.state.state_service.db_path) as db:
        states = db.execute(
            "SELECT DISTINCT status,attempt_count,lease_id FROM agent_jobs WHERE id!=?",
            (legacy["id"],),
        ).fetchall()
    assert states == [("queued", 0, None)]


@pytest.mark.asyncio
async def test_readiness_time_is_selected_after_waiting_for_sql_writer(
    client, paired_headers, test_app
):
    import asyncio

    import aiosqlite

    old, old_auth = register(
        client, paired_headers, name="earlier", capacity={"symbolic_context_version": 1}
    )
    current, _ = register(
        client, paired_headers, name="waiting", capacity={"symbolic_context_version": 1}
    )
    dispatcher = test_app.state.agent_dispatcher
    now = [datetime.now(UTC) + timedelta(seconds=2)]
    dispatcher.clock = lambda: now[0]
    dispatcher.scheduler.clock = dispatcher.clock
    dispatcher.scheduler.offline_timeout_seconds = 300
    with sqlite3.connect(dispatcher.db_path) as db:
        db.execute("INSERT INTO agent_scores VALUES(?,1,10,'fixture')", (old["id"],))
    assert claim(client, old, old_auth, ["symbolic-v1"]).json() is None
    job = queued(client, paired_headers, test_app)
    blocker = await aiosqlite.connect(dispatcher.db_path)
    await blocker.execute("BEGIN IMMEDIATE")
    running = asyncio.create_task(
        dispatcher.claim(current["id"], context_protocols=("symbolic-v1",))
    )
    try:
        await asyncio.sleep(0.05)
        assert not running.done()
        now[0] += timedelta(seconds=31)
        await blocker.commit()
    finally:
        await blocker.close()
    accepted = await asyncio.wait_for(running, timeout=3)
    assert accepted and accepted["id"] == job["id"]
    assert accepted["claimed_at"] == now[0].isoformat()


@pytest.mark.asyncio
async def test_readiness_does_not_cross_dispatcher_instances(client, paired_headers, test_app):
    from app.services.agent_dispatcher import AgentDispatcher

    old, old_auth = register(
        client, paired_headers, name="ready elsewhere", capacity={"symbolic_context_version": 1}
    )
    current, _ = register(
        client, paired_headers, name="ready here", capacity={"symbolic_context_version": 1}
    )
    first = test_app.state.agent_dispatcher
    with sqlite3.connect(first.db_path) as db:
        db.execute("INSERT INTO agent_scores VALUES(?,1,10,'fixture')", (old["id"],))
    assert claim(client, old, old_auth, ["symbolic-v1"]).json() is None
    job = queued(client, paired_headers, test_app)
    second = AgentDispatcher(
        first.db_path,
        first.board,
        permission_policy=first.permission_policy,
        symbolic_catalogs=first.symbolic_catalogs,
    )
    before = effects(test_app)
    assert await second.claim(old["id"]) is None
    assert effects(test_app) == before
    accepted = await second.claim(current["id"], context_protocols=("symbolic-v1",))
    assert accepted and accepted["id"] == job["id"]
