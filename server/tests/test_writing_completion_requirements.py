from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from app.models import AgentCreate
from app.services.agent_dispatcher import AgentDispatchConflict
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest
from app.services.writing_drafts import writing_completion_valid_locked, writing_payload
from tests.test_goal_runtime_recovery import _manager
from tests.test_goal_writing import RESULT, done, plan, register
from tests.test_goal_writing_evaluation import completed_writing

OBJECTIVE = "Rédige une note de 150 à 200 mots pour comparer SQLite et JSON."


@pytest.mark.asyncio
async def test_research_only_plan_cannot_complete_a_requested_note(tmp_path: Path) -> None:
    proposal = plan().model_copy(update={"objective": OBJECTIVE}, deep=True)
    proposal.nodes[0] = proposal.nodes[0].model_copy(
        update={"required_skill": "research.query", "objective": "SQLite JSON storage"}
    )
    manager = await _manager(tmp_path / "state.db", proposal, evaluator=done())
    agent = await manager.state_service.register_agent(
        AgentCreate(name="Search", endpoint="https://worker.invalid", skills=["research.query"]),
        "phone",
    )
    await manager.state_service.heartbeat_agent(agent["id"], "online", agent["credential"])
    goal = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent["id"])
    completed, _ = await manager.agent_dispatcher.submit_result(
        agent["id"],
        job["id"],
        job["claim_token"],
        status="completed",
        result={
            "content_trust": "untrusted",
            "results": [
                {
                    "title": "SQLite",
                    "url": "https://sqlite.org/about.html",
                    "snippet": "SQLite documentation.",
                }
            ],
        },
        error=None,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    detail = await manager.on_job_result(completed)
    assert detail["goal"]["status"] == "failed"
    assert detail["nodes"][0]["status"] == "completed"  # Research itself succeeded.


@pytest.mark.asyncio
async def test_short_note_cannot_be_submitted_as_completed(tmp_path: Path) -> None:
    manager = await _manager(
        tmp_path / "state.db", plan().model_copy(update={"objective": OBJECTIVE}), evaluator=done()
    )
    agent = await register(manager)
    goal = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent)
    assert job["payload"]["requirements"] == {"min_words": 150, "max_words": 200}
    with pytest.raises(AgentDispatchConflict, match="writing_requirements_unmet"):
        await manager.agent_dispatcher.submit_result(
            agent,
            job["id"],
            job["claim_token"],
            status="completed",
            result={**RESULT, "text": "SQLite conserve les données de manière structurée. " * 5},
            error=None,
            lease_id=job["lease_id"],
            lease_generation=job["lease_generation"],
        )
    assert (await manager.agent_dispatcher.get_job(job["id"]))["status"] == "claimed"


@pytest.mark.asyncio
@pytest.mark.parametrize("word_count,expected", [(55, "failed"), (160, "completed")])
async def test_done_evaluator_cannot_promote_a_nonconforming_stored_note(
    tmp_path: Path,
    word_count: int,
    expected: str,
) -> None:
    manager = await _manager(
        tmp_path / "state.db", plan().model_copy(update={"objective": OBJECTIVE}), evaluator=done()
    )
    agent = await register(manager)
    goal = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    detail = await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent)
    # Simulate a receipt persisted by the older backend, bypassing today's
    # submission validator. The independently stored completion gate must hold.
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE agent_jobs SET status='completed',result_json=? WHERE id=?",
            (json.dumps({**RESULT, "text": "mot " * word_count}), job["id"]),
        )
        await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (job["task_id"],))
        await db.execute(
            "UPDATE plan_nodes SET status='completed' WHERE id=?", (detail["nodes"][0]["id"],)
        )
        await db.commit()
    await manager._evaluate_if_quiescent(goal["id"])
    assert (await manager.graph.get_goal(goal["id"]))["status"] == expected


def test_constraints_survive_the_twelve_message_payload_window() -> None:
    history = [{"role": "user", "content": "La note doit contenir 150 à 200 mots."}]
    history += [{"role": "assistant", "content": "Étape enregistrée."} for _ in range(20)]
    payload = writing_payload("Comparer SQLite et JSON", history)
    assert len(payload["conversation"]) == 12
    assert payload["requirements"] == {"min_words": 150, "max_words": 200}


@pytest.mark.asyncio
async def test_old_draft_cannot_satisfy_newer_length_instruction(tmp_path: Path) -> None:
    manager, _, goal_id, _ = await completed_writing(tmp_path, text="mot " * 160)
    await manager.conversations.append(
        goal_id,
        message="La note doit désormais contenir 250 à 300 mots.",
        client_message_id="new-contract",
        reply_to_message_id=None,
        actor_id="phone",
    )
    async with aiosqlite.connect(manager.db_path) as db:
        assert not await writing_completion_valid_locked(db, goal_id)
