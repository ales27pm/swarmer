from __future__ import annotations

import copy
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import aiosqlite
import pytest
from jsonschema import Draft202012Validator

from app.services.evaluator_provider import UbuntuEvaluatorProvider
from app.services.goal_manager import GoalManagerConflict
from app.services.model_wire_schema import model_wire_schema
from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import PlanValidationError, validate_evaluation_decision
from app.services.planner_provider import worker_node_array_schema
from app.services.swarm_contracts import (
    EvaluationDecision,
    EvaluationStatus,
    GoalCreateRequest,
    GoalStartRequest,
    PlannerSource,
    SwarmPlanNodeProposal,
    SwarmPlanProposal,
)
from app.services.writing_contracts import WritingPayload
from app.services.writing_drafts import attach_writing_retry_feedback
from tests.test_evaluator import wire_decision
from tests.test_goal_context_payloads import _CapturingEvaluator
from tests.test_goal_runtime_recovery import _manager
from tests.test_goal_writing import plan, register
from tests.test_writing_failure_diagnostics import diagnostics
from tests.test_writing_requirements import worker as worker  # noqa: PLC0414
from tests.test_writing_requirements import writing_payload

OBJECTIVE = "Rédige une note de 150 à 200 mots."


def repair(reference: str | None = "node_failed", **updates: Any) -> SwarmPlanNodeProposal:
    value = plan().nodes[0].model_dump()
    value.update(
        temporary_id="repair", objective="Rédiger une note corrigée.", retry_of_node_id=reference
    )
    return SwarmPlanNodeProposal.model_validate({**value, **updates})


def decision(nodes: list[SwarmPlanNodeProposal]) -> EvaluationDecision:
    return EvaluationDecision(
        schema_version="1.0",
        status=EvaluationStatus.CONTINUE,
        reason_summary="Corriger uniquement la longueur mesurée.",
        missing_requirements=["Longueur"],
        invalid_results=[],
        suggested_new_nodes=nodes,
    )


def measurement() -> dict[str, Any]:
    return diagnostics(
        word_count=225,
        citation_count=0,
        min_citations=None,
        cited_source_domains=[],
        required_source_domains=[],
        failures=["max_words"],
    )


async def failed_attempt(tmp_path: Path, evaluator: Any = None) -> tuple[Any, Any, Any, Any]:
    proposal = plan().model_copy(update={"objective": OBJECTIVE})
    manager = await _manager(
        tmp_path / "retry.db", proposal, evaluator=evaluator or _CapturingEvaluator()
    )
    agent = await register(manager)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=OBJECTIVE, max_steps=6, max_model_calls=10, max_replans=1),
        actor_id="phone",
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None and "previous_attempt_feedback" not in job["payload"]
    failed, _ = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="failed",
        result=measurement(),
        error="writing_requirements_unmet",
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    await manager.on_job_result(failed)
    node = next(
        n for n in await manager.graph.list_nodes(goal["id"]) if n["worker_job_id"] == job["id"]
    )
    return manager, goal, node, agent


async def insert_repair(manager: Any, goal: Any, reference: str | None, **updates: Any) -> Any:
    current = await manager.graph.get_goal(goal["id"])
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await manager._insert_evaluator_nodes_locked(
            db,
            current,
            await manager.graph.list_nodes(goal["id"]),
            [repair(reference, **updates)],
            count_replan=False,
            now=manager._now(),
        )
        await db.commit()
    return next(
        n
        for n in await manager.graph.list_nodes(goal["id"])
        if n["planner_metadata"].get("temporary_id") == "repair"
    )


@pytest.mark.asyncio
async def test_225_measurement_reaches_exact_repair_model_input_with_existing_budgets(
    tmp_path: Path, worker: ModuleType
) -> None:
    class RepairEvaluator:
        source = PlannerSource.UBUNTU_LOCAL
        model = "repair-test"

        async def evaluate(self, context: Any) -> EvaluationDecision:
            return decision([repair(context.node_results[0].node_id)])

    manager, goal, previous, agent = await failed_attempt(tmp_path, RepairEvaluator())
    next_job = await manager.agent_dispatcher.claim(agent)
    assert next_job and next_job["max_attempts"] == 1
    payload = next_job["payload"]
    expected = {
        "node_id": previous["id"],
        "worker_job_id": previous["worker_job_id"],
        "diagnostics": measurement(),
    }
    assert payload["previous_attempt_feedback"] == expected
    assert (
        worker._model_input(worker.validate_payload(payload))[0]["previous_attempt_feedback"]
        == expected
    )
    assert payload["requirements"] == {"min_words": 150, "max_words": 200}
    state = await manager.graph.get_goal(goal["id"])
    assert (state["step_count"], state["model_call_count"], state["replan_count"]) == (2, 4, 0)
    assert (state["max_steps"], state["max_model_calls"], state["max_replans"]) == (6, 10, 1)


@pytest.mark.asyncio
async def test_new_deliverable_never_infers_latest_failure(tmp_path: Path) -> None:
    manager, goal, _previous, _ = await failed_attempt(tmp_path)
    consumer = await insert_repair(manager, goal, None)
    before = await manager.graph.get_goal(goal["id"])
    payload = await manager._worker_payload(before, consumer)
    assert "previous_attempt_feedback" not in payload
    assert await manager.graph.get_goal(goal["id"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_goal",
        "new_revision",
        "not_failed",
        "wrong_task_source",
        "wrong_skill",
        "raw_diagnostics",
    ],
)
async def test_invalid_parent_cannot_be_persisted_as_repair(tmp_path: Path, mutation: str) -> None:
    manager, goal, previous, _ = await failed_attempt(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        if mutation == "foreign_goal":
            other = await manager.create_goal(
                GoalCreateRequest(objective=OBJECTIVE), actor_id="phone"
            )
            await db.execute(
                "UPDATE plan_nodes SET goal_run_id=? WHERE id=?", (other["id"], previous["id"])
            )
        elif mutation == "new_revision":
            await db.execute(
                "UPDATE goal_runs SET conversation_revision=1 WHERE id=?", (goal["id"],)
            )
        elif mutation == "not_failed":
            await db.execute(
                "UPDATE agent_jobs SET status='completed' WHERE id=?", (previous["worker_job_id"],)
            )
        elif mutation == "wrong_task_source":
            await db.execute(
                "UPDATE tasks SET source='goal:other-project' WHERE id=?", (previous["task_id"],)
            )
        elif mutation == "wrong_skill":
            await db.execute(
                "UPDATE agent_jobs SET required_skill='code.build_project' WHERE id=?",
                (previous["worker_job_id"],),
            )
        else:
            await db.execute(
                "UPDATE agent_jobs SET result_json=? WHERE id=?",
                (
                    json.dumps({**measurement(), "text": "private rejected draft"}),
                    previous["worker_job_id"],
                ),
            )
        await db.commit()
    with pytest.raises(GoalManagerConflict, match="lineage"):
        await insert_repair(manager, goal, previous["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "objective",
        "requirements",
        "sources",
        "conversation",
        "receipt",
        "consumer_revision",
        "non_evaluator",
        "parallel",
    ],
)
async def test_dispatch_rechecks_exact_admission_and_linear_provenance(
    tmp_path: Path, change: str
) -> None:
    manager, goal, previous, _ = await failed_attempt(tmp_path)
    consumer = await insert_repair(manager, goal, previous["id"])
    payload = (await manager.agent_dispatcher.get_job(previous["worker_job_id"]))["payload"]
    payload = copy.deepcopy(payload)
    async with aiosqlite.connect(manager.db_path) as db:
        if change == "objective":
            payload["objective"] = "A different deliverable."
        elif change == "requirements":
            payload["requirements"] = {"max_words": 300}
        elif change == "sources":
            payload["research_sources"] = writing_payload()["research_sources"]
        elif change == "conversation":
            payload["conversation"] = [{"role": "user", "content": "Different instruction."}]
        elif change == "receipt":
            await db.execute(
                "UPDATE agent_jobs SET result_json=? WHERE id=?",
                (json.dumps({**measurement(), "word_count": 226}), previous["worker_job_id"]),
            )
        elif change == "consumer_revision":
            await db.execute(
                "UPDATE goal_runs SET conversation_revision=1 WHERE id=?", (goal["id"],)
            )
        elif change == "non_evaluator":
            metadata = {**consumer["planner_metadata"], "source": "planner"}
            await db.execute(
                "UPDATE plan_nodes SET planner_metadata_json=? WHERE id=?",
                (json.dumps(metadata), consumer["id"]),
            )
        else:
            await db.execute(
                "UPDATE plan_nodes SET planner_metadata_json=? WHERE id=?",
                (json.dumps(consumer["planner_metadata"]), previous["id"]),
            )
        await db.commit()
    with pytest.raises(ValueError):
        await attach_writing_retry_feedback(manager.db_path, goal["id"], consumer["id"], payload)


@pytest.mark.asyncio
async def test_parallel_or_repeated_repairs_cannot_claim_same_failure(tmp_path: Path) -> None:
    manager, goal, previous, _ = await failed_attempt(tmp_path)
    await insert_repair(manager, goal, previous["id"])
    with pytest.raises(GoalManagerConflict, match="lineage"):
        await insert_repair(manager, goal, previous["id"], temporary_id="another_repair")


@pytest.mark.asyncio
async def test_retry_allows_new_historical_observations_but_pins_required_capsule(
    tmp_path: Path,
) -> None:
    from tests.test_agent_capsule import capsule

    manager, goal, previous, _ = await failed_attempt(tmp_path)
    old = (await manager.agent_dispatcher.get_job(previous["worker_job_id"]))["payload"]
    old["durable_context"] = capsule()
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE agent_jobs SET payload_json=? WHERE id=?",
            (json.dumps(old), previous["worker_job_id"]),
        )
        await db.commit()
    consumer = await insert_repair(manager, goal, previous["id"])
    current = copy.deepcopy(old)
    current["durable_context"]["version"] += 1
    current["durable_context"]["fingerprint"] = "f" * 64
    current["durable_context"]["experiences"]["omitted_count"] += 1
    current["durable_context"]["experiences"]["items"][0]["summary"] = "Rejected: word_count=225."
    attached = await attach_writing_retry_feedback(
        manager.db_path, goal["id"], consumer["id"], current
    )
    assert attached["previous_attempt_feedback"]["diagnostics"] == measurement()
    assert attached["durable_context"] == current["durable_context"]
    for field in ("requirements", "project_guidance"):
        changed = copy.deepcopy(current)
        key = "text" if field == "requirements" else "sha256"
        changed["durable_context"][field][0][key] = (
            "Different requirement." if key == "text" else "e" * 64
        )
        with pytest.raises(ValueError, match="admitted inputs"):
            await attach_writing_retry_feedback(
                manager.db_path, goal["id"], consumer["id"], changed
            )


@pytest.mark.parametrize(
    "change",
    [
        None,
        {},
        {"text": "private"},
        {"node_id": "bad/path"},
        {"worker_job_id": "foreign"},
        {"diagnostics": {**diagnostics(), "word_count": True}},
        {"diagnostics": {**diagnostics(), "max_words": 300}},
        {"diagnostics": {**diagnostics(), "text": "private"}},
    ],
)
def test_server_and_worker_reject_same_invalid_feedback(worker: ModuleType, change: Any) -> None:
    base = {"node_id": "node_failed", "worker_job_id": "job_failed", "diagnostics": diagnostics()}
    feedback = {**base, **change} if isinstance(change, dict) and change else change
    payload = {**writing_payload(), "previous_attempt_feedback": feedback}
    with pytest.raises(ValueError):
        WritingPayload.model_validate(payload)
    with pytest.raises(worker.GenerationError):
        worker.validate_payload(payload)


def test_actual_wire_schema_exposes_reference_only_for_evaluator_writing_branch() -> None:
    schema = model_wire_schema(EvaluationDecision)
    array = schema["properties"]["suggested_new_nodes"]
    node = schema["$defs"]["SwarmPlanNodeProposal"]
    for evaluator in (False, True):
        branch = worker_node_array_schema(
            array,
            node,
            available_skills=["writing.draft", "research.query"],
            allow_existing_node_dependencies=evaluator,
        )
        branch["$defs"] = schema["$defs"]
        wire = repair().model_dump()
        wire["00_required_skill"] = wire.pop("required_skill")
        assert Draft202012Validator(branch).is_valid([wire]) is evaluator
        wire["retry_of_node_id"] = None
        assert Draft202012Validator(branch).is_valid([wire])
        wire["retry_of_node_id"] = "node_failed"
        wire["00_required_skill"] = "research.query"
        wire["search_query"] = wire.pop("objective")
        assert not Draft202012Validator(branch).is_valid([wire])
    actual = UbuntuEvaluatorProvider._response_format(["writing.draft"])["json_schema"]["schema"]
    assert Draft202012Validator(actual).is_valid(
        wire_decision(decision([repair()]).model_dump(mode="json"))
    )


def test_unknown_duplicate_and_initial_repair_references_are_rejected() -> None:
    with pytest.raises(PlanValidationError):
        validate_evaluation_decision(
            decision([repair()]),
            policy=PermissionPolicy.from_yaml(
                Path(__file__).resolve().parents[2] / "configs/permissions.yaml"
            ),
            known_node_ids=[],
        )
    with pytest.raises(PlanValidationError):
        validate_evaluation_decision(
            decision([repair(), repair(temporary_id="second")]),
            policy=PermissionPolicy.from_yaml(
                Path(__file__).resolve().parents[2] / "configs/permissions.yaml"
            ),
            known_node_ids=["node_failed"],
        )
    value = plan().model_dump()
    value["nodes"] = [repair().model_dump()]
    with pytest.raises(ValueError):
        SwarmPlanProposal.model_validate(value)
    with pytest.raises(ValueError):
        repair(required_skill="code.build_project")
