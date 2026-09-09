from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.message_board import DurableEvent, MessageBoardUnavailableError
from tests.test_worker_protocol import register


class UnavailableEventFabric:
    """External transport double; authoritative SQLite must remain usable."""

    async def publish(self, event: DurableEvent) -> dict[str, Any]:
        del event
        raise MessageBoardUnavailableError("external event fabric unavailable")

    async def health(self) -> dict[str, Any]:
        return {"backend": "external-test", "status": "degraded"}

    async def close(self) -> None:
        return None


def _agent_headers(agent: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {agent['credential']}"}


def _expire_worker_lease(database: Path, job_id: str) -> None:
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE agent_jobs SET lease_expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )


def test_one_authoritative_control_plane_fences_dead_worker_and_fails_over(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    """Model the supported topology: one SQLite authority, two API workers.

    Worker host A and worker host B are independent authenticated identities.
    They never share SQLite, event-fabric credentials, or a lease secret. The
    test deliberately fails the optional event fabric while authoritative job
    transitions continue, then verifies lease-generation failover and recovery.
    """

    dispatcher = test_app.state.agent_dispatcher
    reaper = test_app.state.agent_lease_reaper
    authoritative_board = test_app.state.message_board
    unavailable_board = UnavailableEventFabric()
    dispatcher.outbox.board = unavailable_board
    reaper.outbox.board = unavailable_board

    worker_a = register(client, paired_headers, "worker-host-a", ["workspace.list_dir"])
    assert (
        asyncio.run(dispatcher.authenticate(str(worker_a["id"]), str(worker_a["credential"])))
        is not None
    )
    assert asyncio.run(dispatcher.authenticate(str(worker_a["id"]), "wrong-credential")) is None

    task = client.post(
        "/tasks",
        headers=paired_headers,
        json={"input": "list the authoritative workspace root"},
    )
    assert task.status_code == 201
    task_id = str(task.json()["id"])
    queued = client.post(
        f"/tasks/{task_id}/dispatch",
        headers=paired_headers,
        json={"required_skill": "workspace.list_dir", "payload": {"path": "."}},
    )
    assert queued.status_code == 201

    claim_a = client.post(
        f"/agents/{worker_a['id']}/claim",
        headers=_agent_headers(worker_a),
        json={},
    )
    assert claim_a.status_code == 200
    lease_a = claim_a.json()
    assert lease_a["lease_generation"] == 1
    assert asyncio.run(dispatcher.outbox.pending_count()) > 0

    worker_b = register(client, paired_headers, "worker-host-b", ["workspace.list_dir"])
    assert worker_a["credential"] != worker_b["credential"]

    # Worker A dies without a heartbeat. Expiry is forced rather than sleeping
    # so this qualification remains deterministic and bounded in CI.
    database = Path(test_app.state.settings.db_path)
    _expire_worker_lease(database, str(lease_a["id"]))
    recovered = asyncio.run(reaper.reap_expired())
    assert recovered["expired"] == 1
    assert recovered["requeued"] == 1

    claim_b = client.post(
        f"/agents/{worker_b['id']}/claim",
        headers=_agent_headers(worker_b),
        json={},
    )
    assert claim_b.status_code == 200
    lease_b = claim_b.json()
    assert lease_b["id"] == lease_a["id"]
    assert lease_b["lease_generation"] == lease_a["lease_generation"] + 1
    assert lease_b["claim_token"] != lease_a["claim_token"]

    stale_result = client.post(
        f"/agents/{worker_a['id']}/jobs/{lease_a['id']}/result",
        headers=_agent_headers(worker_a),
        json={
            "claim_token": lease_a["claim_token"],
            "lease_id": lease_a["lease_id"],
            "lease_generation": lease_a["lease_generation"],
            "status": "completed",
            "result": {"entries": ["stale"]},
        },
    )
    assert stale_result.status_code == 409

    # Recover the optional event fabric. Durable publications converge from the
    # outbox, while job state has remained authoritative in SQLite throughout.
    dispatcher.outbox.board = authoritative_board
    reaper.outbox.board = authoritative_board
    drained = asyncio.run(dispatcher.outbox.drain(limit=500))
    assert drained["failed"] == 0
    assert drained["pending"] == 0

    completed = client.post(
        f"/agents/{worker_b['id']}/jobs/{lease_b['id']}/result",
        headers=_agent_headers(worker_b),
        json={
            "claim_token": lease_b["claim_token"],
            "lease_id": lease_b["lease_id"],
            "lease_generation": lease_b["lease_generation"],
            "status": "completed",
            "result": {"entries": ["README.md"]},
        },
    )
    assert completed.status_code == 200
    assert completed.json()["idempotent_replay"] is False
    assert (
        client.get(f"/tasks/{task_id}", headers=paired_headers).json()["task"]["status"]
        == "completed"
    )

    with sqlite3.connect(database) as db:
        job = db.execute(
            "SELECT status,lease_generation,claimed_by FROM agent_jobs WHERE id=?",
            (lease_b["id"],),
        ).fetchone()
        duplicate_domain_rows = db.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT dedupe_key FROM outbox_events
                GROUP BY dedupe_key HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
    assert job == ("completed", 2, worker_b["id"])
    assert duplicate_domain_rows == (0,)
