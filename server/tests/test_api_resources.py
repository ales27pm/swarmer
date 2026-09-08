from itertools import pairwise

from fastapi.testclient import TestClient


def test_richer_resources_are_authenticated(client: TestClient) -> None:
    checks = [
        ("get", "/conversations"),
        ("get", "/memory"),
        ("get", "/agents"),
        ("get", "/audit"),
        ("post", "/chat"),
        ("post", "/feedback"),
    ]
    for method, path in checks:
        response = client.post(path, json={}) if method == "post" else client.get(path)
        assert response.status_code == 401, path


def test_chat_memory_agent_audit_and_bootstrap(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    chat = client.post(
        "/chat",
        headers=paired_headers,
        json={"content": "Inspect the local workspace", "start_task": True},
    )
    assert chat.status_code == 201
    conversation_id = chat.json()["conversation_id"]
    task_id = chat.json()["task"]["id"]
    messages = client.get(
        f"/conversations/{conversation_id}/messages", headers=paired_headers
    ).json()
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "Inspect the local workspace")
    ]

    memory = client.post(
        "/memory",
        headers=paired_headers,
        json={
            "content": "The outdoor swarm uses a local FastAPI control plane.",
            "summary": "Local control plane",
            "scope": "swarmer",
            "pinned": False,
        },
    )
    assert memory.status_code == 201
    memory_id = memory.json()["id"]
    search = client.post(
        "/memory/search",
        headers=paired_headers,
        json={"query": "FastAPI local", "scope": "swarmer"},
    )
    assert search.status_code == 200
    assert search.json()[0]["id"] == memory_id
    assert search.json()[0]["search_kind"] == "lexical"
    pinned = client.patch(f"/memory/{memory_id}", headers=paired_headers, json={"pinned": True})
    assert pinned.json()["pinned"] is True

    agent = client.post(
        "/agents/register",
        headers=paired_headers,
        json={
            "name": "Local reviewer",
            "endpoint": "http://127.0.0.1:9001",
            "skills": ["code_review.git_status", "code_review.static_analysis"],
        },
    )
    assert agent.status_code == 201
    agent_id = agent.json()["id"]
    assert agent.json()["status"] == "unverified"
    assert agent.json()["last_heartbeat_at"] is None
    credential = agent.json()["credential"]
    assert credential
    rejected_heartbeat = client.post(
        f"/agents/{agent_id}/heartbeat", headers=paired_headers, json={"status": "busy"}
    )
    assert rejected_heartbeat.status_code == 401
    heartbeat = client.post(
        f"/agents/{agent_id}/heartbeat",
        headers={"Authorization": f"Bearer {credential}"},
        json={"status": "busy"},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["status"] == "busy"

    feedback = client.post(
        "/feedback",
        headers=paired_headers,
        json={"task_id": task_id, "score": 4, "notes": "Useful"},
    )
    assert feedback.status_code == 201
    audit = client.get("/audit?limit=100", headers=paired_headers).json()
    assert any(event["event_type"] == "feedback.created" for event in audit)
    chained = list(reversed([event for event in audit if event["hash"]]))
    for previous, current in pairwise(chained):
        assert current["prev_hash"] == previous["hash"]

    bootstrap = client.get("/sync/bootstrap", headers=paired_headers).json()
    assert bootstrap["counts"]["tasks"] == 1
    assert bootstrap["counts"]["messages"] == 1
    assert bootstrap["counts"]["agents"] == 1
    assert bootstrap["counts"]["memory_items"] == 1
    assert bootstrap["pinned_memory"][0]["id"] == memory_id
    assert all("decision" in approval for approval in bootstrap["approvals"])
    assert all("decision_json" not in approval for approval in bootstrap["approvals"])

    assert client.delete(f"/memory/{memory_id}", headers=paired_headers).status_code == 204
    assert client.delete(f"/memory/{memory_id}", headers=paired_headers).status_code == 404
