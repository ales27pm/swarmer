from fastapi.testclient import TestClient

from app.main import app


def test_create_task() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            json={"input": "inspecte le swarm", "mode": "review", "source": "test"},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["id"].startswith("tsk_")
    assert payload["status"] == "created"
