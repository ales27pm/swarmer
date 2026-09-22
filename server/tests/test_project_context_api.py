from fastapi.testclient import TestClient

from tests.test_goal_api import _create_goal


def test_context_routes_require_pairing(client: TestClient) -> None:
    for method, path in (
        ("GET", "/memory/status"),
        ("POST", "/goals/g/context"),
        ("POST", "/goals/g/context/compact"),
        ("GET", "/goals/g/context/sources/m"),
    ):
        assert client.request(method, path).status_code == 401


def test_durable_context_api_versions_sources_and_disabled_compaction(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    settings = client.app.state.settings
    assert client.post("/goals/missing/context", headers=paired_headers).json() == {
        "enabled": False
    }
    settings.project_context_enabled = True
    assert client.post("/goals/missing/context", headers=paired_headers).status_code == 409
    detail = _create_goal(
        client, paired_headers, objective="Organiser les documents sans envoyer de courriels"
    )
    goal_id = detail["goal"]["id"]
    path = f"/goals/{goal_id}/context"
    response = client.post(path, headers=paired_headers)
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["version"] == 1 and state["requirements"]
    assert client.post(path, headers=paired_headers).json() == state
    source_id = state["requirements"][0]["source_id"]
    source = client.get(f"{path}/sources/{source_id}", headers=paired_headers)
    assert source.status_code == 200 and "courriels" in source.json()["content"]
    assert client.get(f"{path}/sources/unknown", headers=paired_headers).status_code == 404
    assert client.post(f"{path}/compact", headers=paired_headers).status_code == 409
    status = client.get("/memory/status", headers=paired_headers).json()
    assert status["context_enabled"] and status["readiness"] == "lexical_fallback"
    assert status["embedding_configured"] is False
