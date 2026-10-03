from fastapi.testclient import TestClient


def test_projection_status_requires_pairing_and_reports_missing_index_without_text(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    assert client.get("/memory/status").status_code == 401
    content = "Private qualification source not to expose in status."
    created = client.post("/memory", json={"content": content}, headers=paired_headers)
    assert created.status_code == 201
    response = client.get("/memory/status", headers=paired_headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    status = response.json()["memory_projections"]
    assert status["pending"] == 1
    assert status["configuration_missing"] == 1
    assert status["completed"] == 0
    assert content not in response.text
    assert created.json()["id"] not in response.text
    assert "vector" not in status


def test_projection_status_does_not_recreate_deleted_source(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    item = client.post(
        "/memory", json={"content": "Delete only this source."}, headers=paired_headers
    ).json()
    assert client.delete(f"/memory/{item['id']}", headers=paired_headers).status_code == 204
    for _ in range(2):
        response = client.get("/memory/status", headers=paired_headers)
        assert response.status_code == 200
        assert response.json()["memory_projections"]["configuration_missing"] == 0
        assert client.get("/memory", headers=paired_headers).json() == []
