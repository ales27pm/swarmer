from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import aiosqlite
import pytest

from app.services.agent_dispatcher import AgentDispatchConflict
from app.services.project_context import ProjectContextService
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest
from app.services.writing_drafts import writing_payload as next_writing_payload
from app.services.writing_drafts import writing_retry_provenance_locked
from tests.test_goal_context_payloads import _CapturingEvaluator
from tests.test_goal_runtime_recovery import _manager
from tests.test_goal_writing import plan, register
from tests.test_writing_failure_diagnostics import diagnostics
from tests.test_writing_requirements import worker as worker  # noqa: PLC0414
from tests.test_writing_requirements import writing_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected",
    [
        ("unsupported_citation", "unsupported_citation"),
        ("invalid_output", "invalid_output"),
        ("invalid_output: private generated text", "remote worker reported failure"),
        ("unsupported_citation: private generated text", "remote worker reported failure"),
    ],
)
async def test_closed_writing_code_reaches_evaluator_as_fixed_failure_only(
    tmp_path: Path, worker: ModuleType, error: str, expected: str
) -> None:
    evaluator = _CapturingEvaluator()
    manager = await _manager(tmp_path / "writing.db", plan(), evaluator=evaluator)
    agent = await register(manager)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=plan().objective), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None
    result, changed = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="failed",
        result=None,
        error=error,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )

    assert changed and result["status"] == "failed" and result["result"] is None
    await manager.on_job_result(result)
    assert len(evaluator.contexts) == 1
    node = evaluator.contexts[0].node_results[0]
    assert node.status.value == "failed" and node.result_summary is None
    assert node.failure_reason == expected
    detail = await manager.get_goal(goal["id"])
    assert detail is not None and detail["goal"]["status"] != "completed"
    assert detail["goal"]["model_call_count"] == 3  # One planner, worker and evaluator.
    assert "private generated text" not in evaluator.contexts[0].model_dump_json()
    if error == "invalid_output":
        service = ProjectContextService(manager.db_path)
        capsule = service.prompt_state(await service.refresh(goal["id"]))
        experience = capsule["experiences"]["items"][0]
        assert experience["source_id"] == node.node_id
        assert experience["worker_job_id"] == job["id"]
        assert experience["observation_kind"] == "reported_failure"
        assert experience["applicability"] == "historical"
        assert "invalid_output" in experience["summary"]
        assert "No word-count or citation measurements" in experience["summary"]
        payload = next_writing_payload(plan().objective, [], durable_context=capsule)
        model_input, _schema = worker._model_input(worker.validate_payload(payload))
        assert model_input["durable_context"]["experiences"]["items"][0] == experience
        assert "previous_attempt_feedback" not in model_input
        # A reported output-contract failure is not an authoritative measurement.
        async with aiosqlite.connect(manager.db_path) as db:
            with pytest.raises(ValueError):
                await writing_retry_provenance_locked(db, goal["id"], 0, node.node_id)
        after = await manager.get_goal(goal["id"])
        assert after is not None
        assert after["goal"]["model_call_count"] == 3
        assert after["goal"]["step_count"] == detail["goal"]["step_count"]


@pytest.mark.asyncio
async def test_rejected_draft_measurements_reach_evaluator_and_stay_failed(tmp_path: Path) -> None:
    evaluator = _CapturingEvaluator()
    proposal = plan().model_copy(update={"objective": writing_payload()["objective"]})
    manager = await _manager(tmp_path / "writing.db", proposal, evaluator=evaluator)
    agent = await register(manager)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=writing_payload()["objective"]), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None
    # The plan has no source dependency; only real supplied sources may be cited.
    observation = diagnostics(
        citation_count=0,
        cited_source_domains=[],
        failures=["min_words", "min_citations", "required_source_domains"],
    )
    result, changed = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="failed",
        result=observation,
        error="writing_requirements_unmet",
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    assert changed and result["status"] == "failed"
    assert result["result"] == observation
    assert "word_count=128" in result["error"]
    replay, changed = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="failed",
        result=observation,
        error="private replacement error",
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    assert not changed and replay["error"] == result["error"]
    await manager.on_job_result(result)
    assert len(evaluator.contexts) == 1
    node = evaluator.contexts[0].node_results[0]
    assert node.status.value == "failed" and node.result_summary is None
    assert node.failure_reason is not None
    assert "word_count=128" in node.failure_reason
    assert "min_words=150" in node.failure_reason
    assert "max_words=200" in node.failure_reason
    assert "min_citations=2" in node.failure_reason
    detail = await manager.get_goal(goal["id"])
    assert detail is not None and detail["goal"]["status"] != "completed"
    assert detail["goal"]["model_call_count"] == 3  # planner + one worker + evaluator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,updates",
    [
        ("failed", {"text": "private rejected draft"}),
        ("failed", {"min_words": 1}),
        ("failed", {"failures": ["max_words"]}),
        ("completed", {}),
    ],
)
async def test_invalid_diagnostics_cannot_finish_a_job(
    tmp_path: Path, status: str, updates: dict[str, Any]
) -> None:
    proposal = plan().model_copy(update={"objective": writing_payload()["objective"]})
    manager = await _manager(tmp_path / "writing.db", proposal)
    agent = await register(manager)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=writing_payload()["objective"]), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None
    value = diagnostics(
        citation_count=0,
        cited_source_domains=[],
        failures=["min_words", "min_citations", "required_source_domains"],
    )
    value.update(updates)
    with pytest.raises(AgentDispatchConflict):
        await manager.agent_dispatcher.submit_result(
            agent,
            job["id"],
            job["claim_token"],
            status=status,
            result=value,
            error="writing_requirements_unmet",
            lease_id=job["lease_id"],
            lease_generation=job["lease_generation"],
        )
    recorded = await manager.agent_dispatcher.get_job(job["id"])
    assert recorded is not None and recorded["status"] == "claimed"
