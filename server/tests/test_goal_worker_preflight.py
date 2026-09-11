from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from functools import partial
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.swarm_contracts import GoalStartRequest, PlannerSource, SwarmPlanProposal

OBJECTIVE = "Crées une Application CRM en python"


def _plan(objective: str) -> SwarmPlanProposal:
    return SwarmPlanProposal.model_validate(
        {
            "schema_version": "1.0",
            "objective": objective,
            "rationale_summary": "Inspect the available workspace before proposing further work.",
            "completion_criteria": ["Workspace evidence is available."],
            "max_parallelism": 1,
            "nodes": [
                {
                    "temporary_id": "files",
                    "node_type": "worker",
                    "title": "Inspect workspace",
                    "objective": "List the workspace root",
                    "required_skill": "workspace.list_dir",
                    "dependencies": [],
                    "expected_output": "A bounded root listing",
                    "priority": 1,
                }
            ],
        }
    )


class _FinitePlanner:
    source = PlannerSource.TEST

    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, context: Mapping[str, object]) -> SwarmPlanProposal:
        self.calls += 1
        assert self.calls == 1, "The waiting goal must not repeatedly invoke a planner"
        cards = context["cards"]
        assert isinstance(cards, list)
        goal_card = next(card for card in cards if card["kind"] == "goal")
        return _plan(str(goal_card["card_id"]))


@pytest.fixture
def finite_planner(test_app: FastAPI) -> _FinitePlanner:
    planner = _FinitePlanner()
    test_app.state.goal_manager.planner = planner
    return planner


def _create(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/goals",
        headers=headers,
        json={"objective": OBJECTIVE, "max_model_calls": 1},
    )
    assert response.status_code == 201
    return str(response.json()["goal"]["id"])


def _start(client: TestClient, headers: dict[str, str], goal_id: str) -> dict[str, Any]:
    response = client.post(f"/goals/{goal_id}/start", headers=headers, json={})
    assert response.status_code == 200, response.text
    return response.json()


def _assert_waiting(detail: dict[str, Any]) -> None:
    assert detail["goal"]["status"] == "planning"
    assert detail["goal"]["current_phase"] == "waiting_for_workers"
    assert detail["goal"]["model_call_count"] == 0
    assert detail["goal"]["step_count"] == 0
    assert detail["goal"]["replan_count"] == 0
    assert detail["nodes"] == []
    assert detail["result"] is None


def test_factory_start_without_workers_returns_waiting_detail_without_model_or_fake_plan(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    finite_planner: _FinitePlanner,
) -> None:
    goal_id = _create(client, paired_headers)

    _assert_waiting(_start(client, paired_headers, goal_id))

    assert finite_planner.calls == 0
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM goal_model_calls").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM agent_jobs").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone() == (0,)
        assert db.execute("SELECT status FROM tasks").fetchone() == ("planned",)


def test_waiting_worker_goal_repeated_start_and_reconcile_preserve_model_budget(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    finite_planner: _FinitePlanner,
) -> None:
    goal_id = _create(client, paired_headers)
    _assert_waiting(_start(client, paired_headers, goal_id))
    assert client.portal is not None

    for _ in range(3):
        _assert_waiting(_start(client, paired_headers, goal_id))
        assert client.portal.call(test_app.state.goal_manager.reconcile) == 0

    detail = client.get(f"/goals/{goal_id}", headers=paired_headers)
    assert detail.status_code == 200
    _assert_waiting(detail.json())
    assert finite_planner.calls == 0
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM goal_model_calls").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM agent_jobs").fetchone() == (0,)


def test_waiting_goal_does_not_hide_later_ready_plan_at_recovery_limit(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    finite_planner: _FinitePlanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting_id = _create(client, paired_headers)
    _assert_waiting(_start(client, paired_headers, waiting_id))
    ready_id = _create(client, paired_headers)
    manager = test_app.state.goal_manager
    assert client.portal is not None

    async def crash_before_dispatch(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated exit after plan commit")

    # Leave a real accepted manual plan at the durable recovery boundary.
    with monkeypatch.context() as crash:
        crash.setattr(manager, "_advance_ready", crash_before_dispatch)
        with pytest.raises(RuntimeError, match="simulated exit after plan commit"):
            client.portal.call(
                manager.start_goal,
                ready_id,
                GoalStartRequest(
                    plan_proposal=_plan(OBJECTIVE), planner_source=PlannerSource.MANUAL
                ),
            )
    before = client.get(f"/goals/{ready_id}", headers=paired_headers).json()
    assert before["nodes"][0]["status"] == "ready"
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE goal_runs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (waiting_id,),
        )

    assert client.portal.call(partial(manager.reconcile, limit=1)) == 1

    _assert_waiting(client.get(f"/goals/{waiting_id}", headers=paired_headers).json())
    recovered = client.get(f"/goals/{ready_id}", headers=paired_headers).json()
    assert recovered["nodes"][0]["status"] == "dispatched"
    assert recovered["goal"]["model_call_count"] == 0
    assert finite_planner.calls == 0
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_jobs").fetchone() == (1,)


@pytest.mark.parametrize("unavailable_status", ["offline", "busy", "draining"])
def test_waiting_goal_resumes_only_after_registered_worker_is_online(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    finite_planner: _FinitePlanner,
    unavailable_status: str,
) -> None:
    goal_id = _create(client, paired_headers)
    _assert_waiting(_start(client, paired_headers, goal_id))
    registration = client.post(
        "/agents/register",
        headers=paired_headers,
        json={
            "name": "Workspace worker",
            "endpoint": "https://worker.invalid",
            "skills": ["workspace.list_dir"],
        },
    )
    assert registration.status_code == 201
    agent = registration.json()
    unavailable = client.post(
        f"/agents/{agent['id']}/heartbeat",
        headers={"Authorization": f"Bearer {agent['credential']}"},
        json={"status": unavailable_status},
    )
    assert unavailable.status_code == 200
    _assert_waiting(_start(client, paired_headers, goal_id))
    assert finite_planner.calls == 0
    online = client.post(
        f"/agents/{agent['id']}/heartbeat",
        headers={"Authorization": f"Bearer {agent['credential']}"},
        json={"status": "online"},
    )
    assert online.status_code == 200
    assert client.portal is not None

    client.portal.call(test_app.state.goal_manager.reconcile)

    detail = client.get(f"/goals/{goal_id}", headers=paired_headers).json()
    assert detail["goal"]["status"] == "running"
    assert detail["goal"]["model_call_count"] == 1
    assert detail["goal"]["step_count"] == 1
    assert detail["nodes"][0]["status"] == "dispatched"
    assert finite_planner.calls == 1
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_jobs").fetchone() == (1,)


def test_online_agent_without_execution_skills_does_not_consume_goal_budget(
    client: TestClient,
    paired_headers: dict[str, str],
    finite_planner: _FinitePlanner,
) -> None:
    registration = client.post(
        "/agents/register",
        headers=paired_headers,
        json={"name": "No execution skills", "endpoint": "https://worker.invalid", "skills": []},
    )
    assert registration.status_code == 201
    agent = registration.json()
    online = client.post(
        f"/agents/{agent['id']}/heartbeat",
        headers={"Authorization": f"Bearer {agent['credential']}"},
        json={"status": "online"},
    )
    assert online.status_code == 200
    goal_id = _create(client, paired_headers)

    _assert_waiting(_start(client, paired_headers, goal_id))

    assert finite_planner.calls == 0


def test_explicit_manual_plan_remains_valid_without_registered_workers(
    client: TestClient,
    paired_headers: dict[str, str],
    finite_planner: _FinitePlanner,
) -> None:
    goal_id = _create(client, paired_headers)

    response = client.post(
        f"/goals/{goal_id}/start",
        headers=paired_headers,
        json={
            "plan_proposal": _plan(OBJECTIVE).model_dump(mode="json"),
            "planner_source": "manual",
        },
    )

    assert response.status_code == 200
    detail = response.json()
    assert detail["goal"]["status"] == "running"
    assert detail["goal"]["model_call_count"] == 0
    assert detail["nodes"][0]["status"] == "dispatched"
    assert finite_planner.calls == 0
