from fastapi.testclient import TestClient

from app.main import app


def paired_headers(client: TestClient) -> dict[str, str]:
    code = client.post("/pairing/code").json()["code"]
    token = client.post("/pairing/complete", json={"code": code, "device_id": "test-phone", "name": "pytest"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_create_task() -> None:
    with TestClient(app) as client:
        headers = paired_headers(client)
        response = client.post(
            "/tasks",
            headers=headers,
            json={"input": "inspecte le swarm", "mode": "review", "source": "test"},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["id"].startswith("tsk_")
    assert payload["status"] == "created"
