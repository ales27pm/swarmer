import json
import sqlite3
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


def test_create_and_cancel_task(client: TestClient, paired_headers: dict[str, str]) -> None:
    response = client.post(
        "/tasks",
        headers=paired_headers,
        json={"input": "inspecte le swarm", "mode": "review"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["id"].startswith("tsk_")
    assert payload["status"] == "created"
    assert payload["title"] == "inspecte le swarm"
    assert payload["source"] == "test-phone"

    detail = client.get(f"/tasks/{payload['id']}", headers=paired_headers)
    assert detail.status_code == 200
    assert detail.json()["task"]["id"] == payload["id"]

    cancelled = client.post(f"/tasks/{payload['id']}/cancel", headers=paired_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    audit = client.get("/audit?limit=20", headers=paired_headers).json()
    cancellation_events = [event for event in audit if event["event_type"] == "task.cancelled"]
    assert len(cancellation_events) == 1
    assert cancellation_events[0]["actor_type"] == "device"
    assert cancellation_events[0]["actor_id"] == "test-phone"
    assert not any(event["event_type"] == "task.cancelled.by_device" for event in audit)

    replay = client.post(f"/tasks/{payload['id']}/cancel", headers=paired_headers)
    assert replay.status_code == 409


def test_task_routes_require_authentication(client: TestClient) -> None:
    assert client.get("/tasks").status_code == 401
    assert client.post("/tasks", json={"input": "no"}).status_code == 401


def test_task_source_cannot_be_forged(client: TestClient, paired_headers: dict[str, str]) -> None:
    response = client.post(
        "/tasks",
        headers=paired_headers,
        json={"input": "forged", "source": "trusted-operator"},
    )
    assert response.status_code == 422


def test_conversation_message_does_not_create_or_plan_a_task(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    test_app.state.orchestrator_service.chat = AsyncMock(
        return_value="Oui. Quel genre d’application web veux-tu construire?"
    )

    response = client.post(
        "/chat", headers=paired_headers, json={"content": "Je veux créer une application web"}
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["task"] is None
    assert payload["message"]["role"] == "agent"
    assert payload["message"]["metadata"] == {"verified_status": "conversation_only"}
    assert client.get("/tasks", headers=paired_headers).json() == []
    messages = client.get(
        f"/conversations/{payload['conversation_id']}/messages", headers=paired_headers
    ).json()
    assert [message["role"] for message in messages] == ["user", "agent"]
    test_app.state.orchestrator_service.chat.assert_awaited_once()


def test_websocket_task_and_message_notifications_never_broadcast_natural_language(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    private_task_input = "Inspect the private acquisition workspace"
    private_user_message = "Discuss the confidential medical appointment"
    private_assistant_message = "I can discuss that confidential appointment."
    test_app.state.orchestrator_service.chat = AsyncMock(return_value=private_assistant_message)
    ticket = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]

    with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "connected"

        task_response = client.post(
            "/tasks",
            headers=paired_headers,
            json={"input": private_task_input},
        )
        assert task_response.status_code == 201
        task_event = websocket.receive_json()

        chat_response = client.post(
            "/chat",
            headers=paired_headers,
            json={"content": private_user_message},
        )
        assert chat_response.status_code == 201
        message_events = [websocket.receive_json(), websocket.receive_json()]

    assert task_response.json()["input"] == private_task_input
    assert chat_response.json()["message"]["content"] == private_assistant_message
    assert task_event["type"] == "task.updated"
    assert task_event["payload"]["refetch_required"] is True
    assert [event["type"] for event in message_events] == [
        "message.created",
        "message.created",
    ]
    assert all(event["payload"]["refetch_required"] is True for event in message_events)

    encoded_notifications = json.dumps([task_event, *message_events])
    for private_text in (
        private_task_input,
        private_user_message,
        private_assistant_message,
    ):
        assert private_text not in encoded_notifications


def test_tool_proposal_requires_exact_structured_fields(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "validate proposal schema"}
    ).json()
    missing_arguments = client.post(
        f"/tasks/{task['id']}/tool-calls",
        headers=paired_headers,
        json={"tool_name": "workspace.list_dir", "summary": "List files"},
    )
    hidden_claim = client.post(
        f"/tasks/{task['id']}/tool-calls",
        headers=paired_headers,
        json={
            "tool_name": "workspace.list_dir",
            "arguments": {"path": "."},
            "summary": "List files",
            "completed": True,
        },
    )
    hidden = "credential-like-unknown-field"
    unknown_argument = client.post(
        f"/tasks/{task['id']}/tool-calls",
        headers=paired_headers,
        json={
            "tool_name": "workspace.list_dir",
            "arguments": {"path": ".", "private": hidden},
            "summary": f"List files with {hidden}",
        },
    )

    assert missing_arguments.status_code == 422
    assert hidden_claim.status_code == 422
    assert unknown_argument.status_code == 400
    assert hidden not in unknown_argument.text
    assert client.get(f"/tasks/{task['id']}/tool-calls", headers=paired_headers).json() == []


def test_model_none_proposal_remains_planned_and_truthfully_labeled(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    proposal_text = "No supported tool can safely satisfy this request."
    test_app.state.orchestrator_service.plan = AsyncMock(
        return_value={
            "tool_name": "none",
            "arguments": {},
            "summary": proposal_text,
        }
    )
    chat = client.post(
        "/chat",
        headers=paired_headers,
        json={"content": "answer without a tool", "start_task": True},
    ).json()
    task = chat["task"]

    response = client.post(f"/tasks/{task['id']}/plan", headers=paired_headers)

    assert response.status_code == 200
    assert response.json()["task"]["status"] == "planned"
    assert response.json()["proposal"]["summary"] == proposal_text
    detail = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()
    assert detail["task"]["status"] == "planned"
    assert detail["tool_calls"] == []
    assert detail["messages"][-1]["content"] == proposal_text
    assert detail["messages"][-1]["metadata"]["verified_status"] == "proposal_only"


def test_model_none_summary_remains_rest_scoped_and_never_enters_websocket(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    private_summary = (
        "Medical contact Alice is at +1-555-0100; use the private SMS body near "
        "coordinates 45.5,-73.5."
    )
    test_app.state.orchestrator_service.plan = AsyncMock(
        return_value={"tool_name": "none", "arguments": {}, "summary": private_summary}
    )
    task = client.post(
        "/tasks",
        headers=paired_headers,
        json={"input": "Ask a sensitive question without executing a tool"},
    ).json()
    ticket = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]

    with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "connected"
        response = client.post(f"/tasks/{task['id']}/plan", headers=paired_headers)
        event = websocket.receive_json()

    assert response.status_code == 200
    assert response.json()["proposal"]["summary"] == private_summary
    assert event == {
        "type": "orchestrator.proposed",
        "payload": {
            "task_id": task["id"],
            "planner_source": "ubuntu_local",
            "refetch_required": True,
        },
    }
    assert private_summary not in json.dumps(event)


def test_shipped_french_root_intent_executes_against_the_configured_workspace(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    intent = "Liste les fichiers du projet et résume sa structure."
    request = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
    model_response = httpx.Response(
        200,
        request=request,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "tool_name": "workspace.list_dir",
                                "arguments": {"path": "projet"},
                                "summary": "Liste les fichiers du projet.",
                            }
                        )
                    }
                }
            ]
        },
    )
    chat = client.post(
        "/chat", headers=paired_headers, json={"content": intent, "start_task": True}
    ).json()

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=model_response)):
        response = client.post(f"/tasks/{chat['task']['id']}/plan", headers=paired_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["arguments"] == {"path": "."}
    assert response.json()["result"] == {"entries": []}
    detail = client.get(f"/tasks/{chat['task']['id']}", headers=paired_headers).json()
    assert detail["task"]["status"] == "completed"
    assert detail["tool_calls"] == [response.json()]
    assert detail["messages"][-1]["metadata"]["verified_status"] == "completed"


def test_invalid_model_process_proposal_is_rejected_before_public_proposal_event(
    client: TestClient, paired_headers: dict[str, str], test_app
) -> None:
    hidden = "credential-like-invalid-git-token"
    test_app.state.orchestrator_service.plan = AsyncMock(
        return_value={
            "tool_name": "process.run",
            "arguments": {"argv": [hidden], "cwd": "."},
            "summary": "Invalid process proposal",
        }
    )
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "reject invalid model process"}
    ).json()

    response = client.post(f"/tasks/{task['id']}/plan", headers=paired_headers)
    audit = client.get("/audit?limit=100", headers=paired_headers).json()

    assert response.status_code == 400
    assert response.json()["detail"] == "tool proposal failed executor validation"
    assert hidden not in json.dumps({"response": response.json(), "audit": audit})
    assert sum(event["event_type"] == "orchestrator.rejected" for event in audit) == 1
    assert sum(event["event_type"] == "orchestrator.proposed" for event in audit) == 0
    assert client.get(f"/tasks/{task['id']}/tool-calls", headers=paired_headers).json() == []


def test_direct_invalid_process_proposal_does_not_echo_executable(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    hidden = "credential-like-direct-executable"
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "reject direct invalid process"}
    ).json()

    response = client.post(
        f"/tasks/{task['id']}/tool-calls",
        headers=paired_headers,
        json={
            "tool_name": "process.run",
            "arguments": {"argv": [hidden], "cwd": "."},
            "summary": "Invalid direct process proposal",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "tool proposal failed executor validation"
    assert hidden not in response.text
    assert client.get(f"/tasks/{task['id']}/tool-calls", headers=paired_headers).json() == []


def test_two_post_commit_reread_failures_still_return_durable_completion(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = client.post(
        "/chat",
        headers=paired_headers,
        json={"content": "list workspace after commit", "start_task": True},
    ).json()["task"]
    engine = test_app.state.execution_engine
    original_get = engine.get
    get_calls = 0

    async def fail_first_two_terminal_rereads(tool_call_id: str) -> dict[str, Any] | None:
        nonlocal get_calls
        get_calls += 1
        if get_calls <= 2:
            raise sqlite3.OperationalError("transient post-commit read failure")
        return await original_get(tool_call_id)

    monkeypatch.setattr(engine, "get", fail_first_two_terminal_rereads)
    ticket = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]
    with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "connected"
        response = client.post(
            f"/tasks/{task['id']}/tool-calls",
            headers=paired_headers,
            json={
                "tool_name": "workspace.list_dir",
                "arguments": {"path": "."},
                "summary": "List the workspace",
            },
        )
        events = [websocket.receive_json() for _ in range(3)]

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert get_calls >= 3
    assert [event["type"] for event in events] == [
        "tool.proposed",
        "tool.completed",
        "task.updated",
    ]
    audit = client.get("/audit?limit=100", headers=paired_headers).json()
    assert sum(event["event_type"] == "tool.completed" for event in audit) == 1
    assert sum(event["event_type"] == "tool.execution_rejected" for event in audit) == 0
    detail = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()
    assert detail["task"]["status"] == "completed"
    assert detail["messages"][-1]["metadata"]["verified_status"] == "completed"


def test_two_preclaim_read_failures_preserve_known_queued_rejection(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = client.post(
        "/tasks", headers=paired_headers, json={"input": "do not misclassify queued work"}
    ).json()
    engine = test_app.state.execution_engine
    original_get_internal = engine._get_internal
    internal_read_calls = 0

    async def fail_first_two_internal_reads(tool_call_id: str) -> dict[str, Any] | None:
        nonlocal internal_read_calls
        internal_read_calls += 1
        if internal_read_calls <= 2:
            raise sqlite3.OperationalError("transient pre-claim read failure")
        return await original_get_internal(tool_call_id)

    monkeypatch.setattr(engine, "_get_internal", fail_first_two_internal_reads)
    ticket = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]
    with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "connected"
        response = client.post(
            f"/tasks/{task['id']}/tool-calls",
            headers=paired_headers,
            json={
                "tool_name": "workspace.list_dir",
                "arguments": {"path": "."},
                "summary": "List the workspace",
            },
        )
        events = [websocket.receive_json() for _ in range(3)]

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert internal_read_calls >= 3
    assert [event["type"] for event in events] == [
        "tool.proposed",
        "tool.execution_rejected",
        "task.updated",
    ]
    audit = client.get("/audit?limit=100", headers=paired_headers).json()
    assert sum(event["event_type"] == "tool.execution_rejected" for event in audit) == 1
    assert sum(event["event_type"] == "tool.outcome_uncertain" for event in audit) == 0
    detail = client.get(f"/tasks/{task['id']}", headers=paired_headers).json()
    assert detail["task"]["status"] == "queued"
    assert detail["tool_calls"][0]["status"] == "queued"
