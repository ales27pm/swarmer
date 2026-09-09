from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from app.services.evaluator_provider import NoopEvaluatorProvider
from app.services.swarm_contracts import GoalDetail

_INTERNAL_FIELDS = {
    "evaluation_fingerprint",
    "last_state_fingerprint",
    "plan_fingerprint",
    "planner_metadata",
    "planner_metadata_json",
    "result_json",
    "worker_result_json",
}


def _all_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _all_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_keys(nested)


def _assert_public_projection(value: object) -> None:
    assert _INTERNAL_FIELDS.isdisjoint(_all_keys(value))


def _goal_payload(objective: str, *, autonomy_profile: str = "assisted") -> dict[str, Any]:
    return {
        "objective": objective,
        "autonomy_profile": autonomy_profile,
        "completion_criteria": ["Return bounded, evidence-backed output"],
        "max_steps": 4,
        "max_parallelism": 1,
        "max_replans": 2,
        "max_runtime_seconds": 300,
        "max_model_calls": 4,
    }


def _plan(
    objective: str,
    *,
    temporary_id: str,
    node_type: str = "worker",
) -> dict[str, Any]:
    worker = node_type == "worker"
    return {
        "schema_version": "1.0",
        "objective": objective,
        "rationale_summary": "Use one bounded node with a server-validated contract.",
        "nodes": [
            {
                "temporary_id": temporary_id,
                "node_type": node_type,
                "title": "Inspect safely" if worker else "Synthesize safely",
                "objective": "List the repository root" if worker else "Summarize known evidence",
                "required_skill": "workspace.list_dir" if worker else None,
                "dependencies": [],
                "expected_output": "A bounded public summary",
                "priority": 10,
                "preferred_agent_constraints": None,
            }
        ],
        "completion_criteria": ["Return bounded, evidence-backed output"],
        "max_parallelism": 1,
    }


def _create_goal(
    client: TestClient,
    paired_headers: dict[str, str],
    *,
    objective: str,
    autonomy_profile: str = "assisted",
) -> dict[str, Any]:
    response = client.post(
        "/goals",
        headers=paired_headers,
        json=_goal_payload(objective, autonomy_profile=autonomy_profile),
    )
    assert response.status_code == 201
    detail = cast(dict[str, Any], response.json())
    _assert_public_projection(detail)
    return detail


def test_goal_api_requires_device_authentication(
    client: TestClient,
) -> None:
    objective = "Inspect the authenticated goal boundary"

    assert client.get("/goals").status_code == 401
    assert client.post("/goals", json=_goal_payload(objective)).status_code == 401
    assert client.get("/goals/goal_missing").status_code == 401
    assert client.get("/goals/goal_missing/nodes").status_code == 401
    assert client.get("/goals/goal_missing/result").status_code == 401


def test_goal_api_rejects_extra_fields_for_every_command(
    client: TestClient,
    paired_headers: dict[str, str],
) -> None:
    objective = "Reject unknown goal command fields"
    goal_id = str(_create_goal(client, paired_headers, objective=objective)["goal"]["id"])

    invalid_create = _goal_payload(objective)
    invalid_create["unexpected"] = True
    requests = (
        ("/goals", invalid_create),
        (f"/goals/{goal_id}/start", {"unexpected": True}),
        (f"/goals/{goal_id}/cancel", {"unexpected": True}),
        (f"/goals/{goal_id}/replan", {"unexpected": True}),
        (f"/goals/{goal_id}/feedback", {"score": 5, "unexpected": True}),
    )

    for path, payload in requests:
        response = client.post(path, headers=paired_headers, json=payload)
        assert response.status_code == 422, (path, response.text)


def test_goal_response_model_rejects_internal_fields_at_nested_boundaries(
    client: TestClient,
    paired_headers: dict[str, str],
) -> None:
    detail = _create_goal(
        client,
        paired_headers,
        objective="Keep internal goal state outside the response boundary",
    )
    GoalDetail.model_validate(detail)

    detail["goal"]["plan_fingerprint"] = "internal"
    with pytest.raises(PydanticValidationError):
        GoalDetail.model_validate(detail)


def test_goal_api_lifecycle_result_and_feedback_are_public_safe(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    objective = "Inspect the repository through a bounded worker"
    created = _create_goal(
        client,
        paired_headers,
        objective=objective,
        autonomy_profile="manual",
    )
    goal_id = str(created["goal"]["id"])

    listed = client.get("/goals", headers=paired_headers)
    assert listed.status_code == 200
    assert any(goal["id"] == goal_id for goal in listed.json())
    _assert_public_projection(listed.json())

    fetched = client.get(f"/goals/{goal_id}", headers=paired_headers)
    assert fetched.status_code == 200
    assert fetched.json() == created

    started = client.post(
        f"/goals/{goal_id}/start",
        headers=paired_headers,
        json={
            "plan_proposal": _plan(objective, temporary_id="inspect"),
            "planner_source": "manual",
        },
    )
    assert started.status_code == 200, started.text
    started_detail = started.json()
    assert started_detail["goal"]["status"] == "running"
    assert started_detail["goal"]["planner_source"] == "manual"
    assert len(started_detail["nodes"]) == 1
    node = started_detail["nodes"][0]
    assert node["status"] == "dispatched"
    assert node["worker_job_id"]
    _assert_public_projection(started_detail)

    nodes = client.get(f"/goals/{goal_id}/nodes", headers=paired_headers)
    assert nodes.status_code == 200
    assert nodes.json() == started_detail["nodes"]
    _assert_public_projection(nodes.json())

    hidden = "never-return-this-worker-secret"
    raw_worker_output = {
        "token": hidden,
        "result_json": {"private_key": hidden},
        "authorization": f"Bearer {hidden}",
    }
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        internal = db.execute(
            """
            SELECT g.plan_fingerprint,n.planner_metadata_json
            FROM goal_runs AS g JOIN plan_nodes AS n ON n.goal_run_id=g.id
            WHERE g.id=?
            """,
            (goal_id,),
        ).fetchone()
        assert internal is not None
        assert internal[0]
        assert json.loads(str(internal[1]))["temporary_id"] == "inspect"
        db.execute(
            "UPDATE agent_jobs SET result_json=? WHERE id=?",
            (json.dumps(raw_worker_output), node["worker_job_id"]),
        )
        db.commit()

    cancelled = client.post(
        f"/goals/{goal_id}/cancel",
        headers=paired_headers,
        json={},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["goal"]["status"] == "cancelled"
    assert cancelled.json()["nodes"][0]["status"] == "cancelled"
    _assert_public_projection(cancelled.json())

    result = client.get(f"/goals/{goal_id}/result", headers=paired_headers)
    assert result.status_code == 200
    result_body = result.json()
    assert result_body["goal_run_id"] == goal_id
    assert result_body["status"] == "cancelled"
    assert result_body["failed_nodes"] == [node["id"]]
    assert hidden not in json.dumps(result_body)
    _assert_public_projection(result_body)

    feedback = client.post(
        f"/goals/{goal_id}/feedback",
        headers=paired_headers,
        json={
            "score": 4.5,
            "note": f"Keep the bounded behavior; token={hidden}",
            "corrected_final_answer": "Return only verified summaries.",
            "corrected_plan_summary": "Use one read-only worker.",
            "reviewed": True,
        },
    )
    assert feedback.status_code == 201, feedback.text
    feedback_body = feedback.json()
    assert feedback_body["goal_run_id"] == goal_id
    assert feedback_body["reviewed"] is True
    assert hidden not in json.dumps(feedback_body)
    _assert_public_projection(feedback_body)


def test_goal_api_replan_appends_a_validated_public_node(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    objective = "Refine a bounded deployment summary"
    goal_id = str(_create_goal(client, paired_headers, objective=objective)["goal"]["id"])
    test_app.state.goal_manager.evaluator = NoopEvaluatorProvider()

    started = client.post(
        f"/goals/{goal_id}/start",
        headers=paired_headers,
        json={
            "plan_proposal": _plan(
                objective,
                temporary_id="first_synthesis",
                node_type="synthesis",
            ),
            "planner_source": "manual",
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["goal"]["status"] == "running"
    assert started.json()["nodes"][0]["status"] == "completed"

    replan_proposal = _plan(
        objective,
        temporary_id="second_synthesis",
        node_type="synthesis",
    )
    replan_proposal["nodes"][0]["objective"] = "Summarize evidence and explicit limitations"
    replan_proposal["nodes"][0]["expected_output"] = "A bounded limitations summary"
    replanned = client.post(
        f"/goals/{goal_id}/replan",
        headers=paired_headers,
        json={
            "reason": "Add a second bounded synthesis pass.",
            "plan_proposal": replan_proposal,
            "planner_source": "manual",
        },
    )
    assert replanned.status_code == 200, replanned.text
    detail = replanned.json()
    assert detail["goal"]["replan_count"] == 1
    assert detail["goal"]["planner_source"] == "manual"
    assert len(detail["nodes"]) == 2
    assert all(node["status"] == "completed" for node in detail["nodes"])
    _assert_public_projection(detail)

    nodes = client.get(f"/goals/{goal_id}/nodes", headers=paired_headers)
    assert nodes.status_code == 200
    assert nodes.json() == detail["nodes"]


def test_goal_websocket_broadcast_is_a_safe_public_projection(
    client: TestClient,
    paired_headers: dict[str, str],
) -> None:
    ticket = client.post("/ws/ticket", headers=paired_headers).json()["ticket"]
    objective = "Observe a safe goal invalidation"

    with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "connected"
        created = client.post(
            "/goals",
            headers=paired_headers,
            json=_goal_payload(objective),
        )
        assert created.status_code == 201
        event = websocket.receive_json()

    assert event["type"] == "goal.updated"
    goal = created.json()["goal"]
    assert event["payload"] == {
        "id": goal["id"],
        "root_task_id": goal["root_task_id"],
        "status": goal["status"],
        "current_phase": goal["current_phase"],
        "updated_at": goal["updated_at"],
        "completed_at": goal["completed_at"],
        "refetch_required": True,
    }
    assert objective not in json.dumps(event)
    _assert_public_projection(event)
