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
    return response.json()


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
    wrong = register(client, paired_headers, "writer", ["workspace.write_text"])
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
        "status": "completed",
        "result": {"entries": ["README.md"]},
    }
    first = client.post(f"/agents/{agent['id']}/jobs/{job['id']}/result", headers=auth, json=body)
    replay = client.post(f"/agents/{agent['id']}/jobs/{job['id']}/result", headers=auth, json=body)
    conflict = client.post(
        f"/agents/{agent['id']}/jobs/{job['id']}/result",
        headers=auth,
        json={**body, "status": "failed", "result": None, "error": "changed"},
    )
    assert first.status_code == 200 and first.json()["idempotent_replay"] is False
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
    assert conflict.status_code == 409
    assert (
        client.get(f"/tasks/{task_id}", headers=paired_headers).json()["task"]["status"]
        == "completed"
    )


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
        json={"claim_token": job["claim_token"], "status": "completed", "result": {}},
    )
    assert response.status_code == 409
