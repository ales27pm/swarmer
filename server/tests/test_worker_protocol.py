import sqlite3

from fastapi.testclient import TestClient


def register(
    client: TestClient, headers: dict[str, str], name: str, skills: list[str]
) -> dict[str, object]:
    response = client.post(
        "/agents/register",
        headers=headers,
        json={"name": name, "endpoint": "http://127.0.0.1:9001", "skills": skills},
    )
    assert response.status_code == 201
    agent = response.json()
    heartbeat = client.post(
        f"/agents/{agent['id']}/heartbeat",
        headers={"Authorization": f"Bearer {agent['credential']}"},
        json={"status": "online"},
    )
    assert heartbeat.status_code == 200
    return agent


def test_worker_claim_requires_agent_credential_and_matches_skills(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    task_id = client.post("/tasks", headers=paired_headers, json={"input": "list root"}).json()[
        "id"
    ]
    dispatched = client.post(
        f"/tasks/{task_id}/dispatch",
        headers=paired_headers,
        json={"required_skill": "workspace.list_dir", "payload": {"path": "."}},
    )
    assert dispatched.status_code == 201
    wrong = register(client, paired_headers, "text-reader", ["workspace.read_text"])
    matching = register(client, paired_headers, "reader", ["workspace.list_dir"])

    assert client.post(f"/agents/{matching['id']}/claim", json={}).status_code == 401
    wrong_claim = client.post(
        f"/agents/{wrong['id']}/claim",
        headers={"Authorization": f"Bearer {wrong['credential']}"},
        json={},
    )
    assert wrong_claim.status_code == 200 and wrong_claim.json() is None
    claim = client.post(
        f"/agents/{matching['id']}/claim",
        headers={"Authorization": f"Bearer {matching['credential']}"},
        json={},
    )
    assert claim.status_code == 200
    assert claim.json()["required_skill"] == "workspace.list_dir"
    assert claim.json()["claim_token"]
    listed = client.get(
        f"/agents/{matching['id']}/jobs",
        headers={"Authorization": f"Bearer {matching['credential']}"},
    ).json()
    assert len(listed) == 1
    assert "claim_token" not in listed[0]
    assert "lease_token" not in listed[0]
    assert "lease_token_hash" not in listed[0]


def test_distributed_runtime_status_is_authenticated_and_payload_free(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    assert client.get("/status").status_code == 401

    response = client.get("/status", headers=paired_headers)

    assert response.status_code == 200
    assert set(response.json()) == {
        "status",
        "version",
        "instance_id",
        "message_board_backend",
        "message_board_health",
        "last_successful_publication",
        "redis_reconnect_count",
        "redis_last_error_category",
        "outbox_pending",
        "outbox_publishing",
        "outbox_failed",
        "outbox_duplicate_publications",
        "outbox_claim_expirations",
        "outbox_publish_latency_ms_count",
        "outbox_publish_latency_ms_total",
        "outbox_publish_latency_ms_max",
        "queued_jobs",
        "leased_jobs",
        "dead_letter_jobs",
        "quarantined_jobs",
        "expired_leases",
        "retries",
        "dead_letter_events",
        "active_agents",
        "offline_agents",
        "maintenance_lease_owner",
        "maintenance_lease_renewal_failures",
        "pending_capability_requests",
        "vector_backend",
        "vector_generation_age_seconds",
    }
    assert response.json()["status"] == "ok"
    assert response.json()["message_board_backend"] == "sqlite"
    encoded_status = str(response.json()).casefold()
    assert "redis_url" not in encoded_status
    assert "6379" not in encoded_status
    assert "password" not in encoded_status


def test_distributed_runtime_status_counts_stale_online_agent_as_offline(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    fresh = register(client, paired_headers, "fresh-reader", ["workspace.list_dir"])
    stale = register(client, paired_headers, "stale-reader", ["workspace.list_dir"])
    db_path = test_app.state.settings.db_path
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE agents SET last_seen_at=?,last_heartbeat_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", stale["id"]),
        )

    response = client.get("/status", headers=paired_headers)

    assert response.status_code == 200
    assert response.json()["active_agents"] == 1
    assert response.json()["offline_agents"] == 1
    assert fresh["id"] != stale["id"]


def test_worker_result_is_idempotent_and_cannot_change_terminal_result(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    task_id = client.post("/tasks", headers=paired_headers, json={"input": "list root"}).json()[
        "id"
    ]
    client.post(
        f"/tasks/{task_id}/dispatch",
        headers=paired_headers,
        json={"required_skill": "workspace.list_dir", "payload": {"path": "."}},
    )
    agent = register(client, paired_headers, "reader", ["workspace.list_dir"])
    auth = {"Authorization": f"Bearer {agent['credential']}"}
    job = client.post(f"/agents/{agent['id']}/claim", headers=auth, json={}).json()
    body = {
        "claim_token": job["claim_token"],
        "lease_id": job["lease_id"],
        "lease_generation": job["lease_generation"],
        "status": "completed",
        "result": {"value": 1, "metadata": {"b": 2, "a": "typed"}},
    }
    first = client.post(f"/agents/{agent['id']}/jobs/{job['id']}/result", headers=auth, json=body)
    replay = client.post(
        f"/agents/{agent['id']}/jobs/{job['id']}/result",
        headers=auth,
        json={
            **body,
            "result": {"metadata": {"a": "typed", "b": 2}, "value": 1},
        },
    )
    typed_conflict = client.post(
        f"/agents/{agent['id']}/jobs/{job['id']}/result",
        headers=auth,
        json={
            **body,
            "result": {"value": True, "metadata": {"b": 2, "a": "typed"}},
        },
    )
    conflict = client.post(
        f"/agents/{agent['id']}/jobs/{job['id']}/result",
        headers=auth,
        json={**body, "status": "failed", "result": None, "error": "changed"},
    )
    assert first.status_code == 200 and first.json()["idempotent_replay"] is False
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
    assert typed_conflict.status_code == 409
    assert conflict.status_code == 409
    assert (
        client.get(f"/tasks/{task_id}", headers=paired_headers).json()["task"]["status"]
        == "completed"
    )


def test_worker_result_requires_full_lease_proof(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    task_id = client.post("/tasks", headers=paired_headers, json={"input": "list root"}).json()[
        "id"
    ]
    client.post(
        f"/tasks/{task_id}/dispatch",
        headers=paired_headers,
        json={"required_skill": "workspace.list_dir", "payload": {"path": "."}},
    )
    agent = register(client, paired_headers, "lease-reader", ["workspace.list_dir"])
    auth = {"Authorization": f"Bearer {agent['credential']}"}
    job = client.post(f"/agents/{agent['id']}/claim", headers=auth, json={}).json()

    response = client.post(
        f"/agents/{agent['id']}/jobs/{job['id']}/result",
        headers=auth,
        json={"claim_token": job["claim_token"], "status": "completed", "result": {}},
    )

    assert response.status_code == 422


def test_terminal_task_rejects_late_worker_result(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    task_id = client.post("/tasks", headers=paired_headers, json={"input": "list root"}).json()[
        "id"
    ]
    client.post(
        f"/tasks/{task_id}/dispatch",
        headers=paired_headers,
        json={"required_skill": "workspace.list_dir", "payload": {"path": "."}},
    )
    agent = register(client, paired_headers, "reader", ["workspace.list_dir"])
    auth = {"Authorization": f"Bearer {agent['credential']}"}
    job = client.post(f"/agents/{agent['id']}/claim", headers=auth, json={}).json()
    db_path = test_app.state.settings.db_path
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
    response = client.post(
        f"/agents/{agent['id']}/jobs/{job['id']}/result",
        headers=auth,
        json={
            "claim_token": job["claim_token"],
            "lease_id": job["lease_id"],
            "lease_generation": job["lease_generation"],
            "status": "completed",
            "result": {},
        },
    )
    assert response.status_code == 409
