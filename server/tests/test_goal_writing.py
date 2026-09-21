from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import AgentCreate
from app.services.evaluator_provider import DeterministicEvaluatorProvider
from app.services.goal_manager import GoalManager
from app.services.planner_provider import DeterministicSwarmPlannerProvider
from app.services.swarm_contracts import (
    EvaluationDecision,
    EvaluationStatus,
    GoalCreateRequest,
    GoalStartRequest,
    SwarmPlanProposal,
)
from app.services.writing_drafts import read_writing_draft, writing_payload
from tests.test_goal_runtime_recovery import _manager, _worker_plan

OBJECTIVE = "Rédige un plan pour une application CRM native iOS en Swift, sans coder."
TEXT = "# Plan CRM\n\n" + "Clients, soumissions, courriels et calendrier.\n" * 100
RESULT = {
    "schema_version": "1.0",
    "content_trust": "untrusted",
    "text": TEXT,
    "summary": "Plan du CRM Swift avec étapes, fonctionnalités et stratégie de vérification.",
}


def plan() -> SwarmPlanProposal:
    result = _worker_plan(objective=OBJECTIVE)
    result.nodes[0] = result.nodes[0].model_copy(
        update={
            "required_skill": "writing.draft",
            "objective": "Rédiger le plan CRM",
            "title": "Plan CRM Swift",
            "expected_output": "Un plan rédigé en français",
        }
    )
    return result


async def register(manager: GoalManager) -> str:
    registration = await manager.state_service.register_agent(
        AgentCreate(
            name="Draft worker", endpoint="https://worker.invalid", skills=["writing.draft"]
        ),
        "test-phone",
    )
    await manager.state_service.heartbeat_agent(
        str(registration["id"]), "online", str(registration["credential"])
    )
    return str(registration["id"])


async def complete(manager: GoalManager, agent: str) -> dict[str, Any]:
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None
    assert job["max_attempts"] == 1
    assert job["payload"]["objective"] == OBJECTIVE
    result, changed = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="completed",
        result=RESULT,
        error=None,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    assert changed
    await manager.on_job_result(result)
    return result


def done() -> DeterministicEvaluatorProvider:
    return DeterministicEvaluatorProvider(
        EvaluationDecision(
            schema_version="1.0",
            status=EvaluationStatus.DONE,
            reason_summary="Le plan demandé est rédigé; aucun logiciel n'a été exécuté.",
            missing_requirements=[],
            invalid_results=[],
            suggested_new_nodes=[],
            completion_summary="Le plan CRM Swift est disponible.",
        )
    )


@pytest.mark.asyncio
async def test_writing_dispatch_budget_full_draft_and_no_execution(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "writing.db", plan(), evaluator=done())
    agent = await register(manager)
    goal = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    detail = await manager.start_goal(goal["id"], GoalStartRequest())
    node = detail["nodes"][0]
    assert detail["goal"]["model_call_count"] == 2  # Planner + reserved writer.
    assert detail["goal"]["step_count"] == 1
    assert await read_writing_draft(manager.db_path, goal["id"], node["id"]) is None
    job = await complete(manager, agent)
    final = await manager.get_goal(goal["id"])
    assert final is not None and final["goal"]["status"] == "completed"
    assert (
        final["goal"]["model_call_count"] == 3
    )  # Evaluator; duplicate delivery cannot charge again.
    assert final["nodes"][0]["status"] == "completed"
    assert TEXT not in json.dumps(final, ensure_ascii=False)
    draft = await read_writing_draft(manager.db_path, goal["id"], node["id"])
    assert draft is not None and draft.text == TEXT
    assert await read_writing_draft(manager.db_path, "other-goal", node["id"]) is None
    await manager.on_job_result(job)
    assert (await manager.graph.get_goal(goal["id"]))["model_call_count"] == 3
    async with aiosqlite.connect(manager.db_path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_writing_cannot_dispatch_without_a_reserved_model_call(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "budget.db", plan())
    await register(manager)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=OBJECTIVE, max_model_calls=1), actor_id="phone"
    )
    detail = await manager.start_goal(goal["id"], GoalStartRequest())
    assert detail["goal"]["status"] == "budget_exhausted"
    async with aiosqlite.connect(manager.db_path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM agent_jobs")).fetchone())[0] == 0


def test_payload_keeps_latest_guidance_with_utf8_budget_and_redacts_secrets() -> None:
    history = [{"role": "assistant", "content": "😀" * 4000} for _ in range(20)]
    history.append(
        {"role": "user", "content": "Inclure le calendrier. password=private-test-value"}
    )
    payload = writing_payload(OBJECTIVE, history)
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= 32000
    assert len(payload["conversation"]) <= 12
    assert "Inclure le calendrier" in payload["conversation"][-1]["content"]
    assert "private-test-value" not in json.dumps(payload)


def test_authenticated_draft_api_projects_only_completed_matching_node(
    test_app: FastAPI, client: TestClient, paired_headers: dict[str, str]
) -> None:
    manager = test_app.state.goal_manager
    manager.planner = DeterministicSwarmPlannerProvider(plan())
    manager.evaluator = done()
    assert client.portal is not None
    agent = client.portal.call(register, manager)
    response = client.post("/goals", headers=paired_headers, json={"objective": OBJECTIVE})
    goal_id = response.json()["goal"]["id"]
    detail = client.post(f"/goals/{goal_id}/start", headers=paired_headers, json={}).json()
    node_id = detail["nodes"][0]["id"]
    path = f"/goals/{goal_id}/nodes/{node_id}/writing-draft"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=paired_headers).status_code == 404
    client.portal.call(complete, manager, agent)
    response = client.get(path, headers=paired_headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    draft = response.json()
    assert draft["text"] == TEXT
    assert set(draft) == {*RESULT, "goal_run_id", "node_id", "worker_job_id", "sha256"}
    assert (
        client.get(path.replace(goal_id, "goal_other"), headers=paired_headers).status_code == 404
    )
    assert "text" not in client.get(f"/goals/{goal_id}", headers=paired_headers).json()["nodes"][0]


@pytest.mark.asyncio
async def test_recovered_payload_uses_stored_goal_not_node_rewording(tmp_path: Path) -> None:
    manager = await _manager(tmp_path / "recovery.db", plan())
    goal = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.conversations.append(
        goal["id"],
        message="Inclure les soumissions et le calendrier.",
        client_message_id="writing-guidance",
        reply_to_message_id=None,
        actor_id="phone",
    )
    recovered = {
        "required_skill": "writing.draft",
        "goal_run_id": goal["id"],
        "objective": "Ask the user to write the plan",
    }
    payload = await manager._worker_payload(recovered, recovered)
    assert payload["objective"] == OBJECTIVE
    assert payload["conversation"][-1]["content"] == "Inclure les soumissions et le calendrier."
    assert "Ask the user" not in json.dumps(payload)
