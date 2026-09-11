from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from pydantic import ValidationError

from app.services.agent_card import SUPPORTED_AGENT_SKILLS
from app.services.context_builder import ContextBuilder, bound_evaluation_context
from app.services.swarm_contracts import (
    GoalCreateRequest,
    GoalEvaluationContext,
    GoalStartRequest,
    PlannerSource,
    SwarmPlanNodeProposal,
)
from tests.test_evaluator import evaluation_context
from tests.test_goal_context_payloads import _CapturingEvaluator, _synthesis_plan
from tests.test_goal_manager import _manager

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def test_legacy_evaluator_context_does_not_invent_capability_or_node_facts() -> None:
    context = evaluation_context()
    assert context.available_skills is None
    assert context.node_results[0].node_type is None
    assert context.node_results[0].required_skill is None


@pytest.mark.parametrize("available", [[], sorted(SUPPORTED_AGENT_SKILLS)])
def test_bounded_context_preserves_structural_facts(available: list[str]) -> None:
    raw = evaluation_context().model_dump(mode="json")
    raw["available_skills"] = available
    raw["objective"] = "Crées une Application CRM en python " + "x" * 3_000
    raw["node_results"][0].update(
        node_type="worker", required_skill="code.build_project", result_summary="x" * 4_000
    )
    bounded = bound_evaluation_context(GoalEvaluationContext.model_validate(raw), max_tokens=512)
    assert bounded.available_skills == available
    assert bounded.node_results[0].node_type == "worker"
    assert bounded.node_results[0].required_skill == "code.build_project"
    encoded = json.dumps(bounded.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    assert len(encoded.encode()) <= 512 * 4


@pytest.mark.parametrize("available", [["code.build_project"] * 2, ["shell.execute"]])
def test_evaluator_context_rejects_duplicate_or_unsupported_skills(available: list[str]) -> None:
    raw = evaluation_context().model_dump(mode="json")
    raw["available_skills"] = available
    with pytest.raises(ValidationError):
        GoalEvaluationContext.model_validate(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "age", "protocol", "allowed", "expected"),
    [
        ("online", 1, "mongars-worker-v0.9", True, ["code.build_project"]),
        ("online", 89, "mongars-worker-v0.9", True, ["code.build_project"]),
        ("busy", 1, "mongars-worker-v0.9", True, ["code.build_project"]),
        ("busy", 90, "mongars-worker-v0.9", True, []),
        ("online", 90, "mongars-worker-v0.9", True, []),
        ("online", -1, "mongars-worker-v0.9", True, []),
        ("draining", 1, "mongars-worker-v0.9", True, []),
        ("offline", 1, "mongars-worker-v0.9", True, []),
        ("online", 1, "unsupported", True, []),
        ("online", 1, "mongars-worker-v0.9", False, []),
    ],
)
async def test_real_evaluator_context_uses_fresh_policy_allowed_worker_facts(
    tmp_path: Path,
    status: str,
    age: int,
    protocol: str,
    allowed: bool,
    expected: list[str],
) -> None:
    objective = "Crées une Application CRM en python"
    plan = _synthesis_plan(objective, suffix="legacy")
    plan.nodes[0].title = "Missing Execution Capabilities"
    evaluator = _CapturingEvaluator()
    manager = await _manager(tmp_path, plan, evaluator=evaluator)
    manager.agent_dispatcher.clock = lambda: NOW
    manager.agent_dispatcher.scheduler.clock = lambda: NOW
    manager.context_builder = ContextBuilder(manager.db_path, max_tokens=512)
    await manager.context_builder.initialize()
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        snapshot = await manager.agent_dispatcher.worker_skill_policy.load_locked(
            db, now=NOW.isoformat()
        )
        assert snapshot is not None
        if not allowed:
            # Change only the durable policy: the manager's cached policy still allows it.
            rules, _ = manager.agent_dispatcher.worker_skill_policy.encode_rules(snapshot.rules)
            changed = json.loads(rules)
            changed["code.build_project"]["decision"] = "deny"
            encoded = json.dumps(changed, separators=(",", ":"), sort_keys=True)
            await db.execute(
                "UPDATE worker_skill_policy_state SET rules_json=?,rules_digest=?",
                (encoded, "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()),
            )
        await db.execute(
            """INSERT INTO agents(id,name,version,endpoint,status,skills_json,last_seen_at,
            supported_protocol_version,max_concurrency,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "agent_context",
                "Test project worker",
                "1.0.0",
                "https://worker.invalid",
                status,
                '["code.build_project","shell.execute"]',
                (NOW - timedelta(seconds=age)).isoformat(),
                protocol,
                1,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        await db.execute(
            """INSERT INTO tasks(id,title,input,mode,source,status,created_at,updated_at)
            VALUES('task_busy','Other work','Other work','normal','test','running',?,?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )
        await db.execute(
            """INSERT INTO agent_jobs(id,task_id,required_skill,payload_json,status,
            claimed_by,created_at,updated_at) VALUES('job_busy','task_busy','code.build_project',
            '{}','running','agent_context',?,?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )
        await db.commit()
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective=objective,
            completion_criteria=["Provide an evidence-backed response to the objective"],
        ),
        actor_id="test-phone",
    )
    await manager.start_goal(str(goal["id"]), GoalStartRequest())

    assert len(evaluator.contexts) == 1
    context = evaluator.contexts[0]
    assert context.available_skills == expected
    assert context.node_results == []
    stored_nodes = await manager.graph.list_nodes(str(goal["id"]))
    assert context.known_node_ids == [stored_nodes[0]["id"]]
    assert stored_nodes[0]["node_type"] == "synthesis"
    assert stored_nodes[0]["status"] == "completed"
    assert stored_nodes[0]["title"] == "Missing Execution Capabilities"
    assert stored_nodes[0]["result_summary"] == "No evidence summary was available for synthesis."
    saved_goal = await manager.graph.get_goal(str(goal["id"]))
    assert saved_goal is not None
    assert saved_goal["step_count"] == 1
    assert saved_goal["model_call_count"] == 2  # One normal planner and one evaluator credit.
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                """SELECT c.context_json,m.input_digest FROM goal_model_calls m
                JOIN goal_contexts c ON c.id=m.context_id WHERE m.goal_run_id=? AND m.role='evaluator'""",
                (goal["id"],),
            )
        ).fetchone()
    assert row is not None
    payload = context.model_dump(mode="json")
    assert json.loads(row[0]) == payload
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert row[1] == hashlib.sha256(encoded.encode()).hexdigest()


@pytest.mark.asyncio
async def test_evaluator_projection_preserves_worker_optional_hard_and_failed_results(
    tmp_path: Path,
) -> None:
    objective = "Comparer les fichiers"
    plan = _synthesis_plan(objective, suffix="failed")
    worker = SwarmPlanNodeProposal(
        temporary_id="worker",
        node_type="worker",
        title="Root worker",
        objective=objective,
        required_skill="workspace.list_dir",
        dependencies=[],
        expected_output="File listing",
        priority=1,
    )
    hard = plan.nodes[0].model_copy(
        update={"temporary_id": "hard", "title": "Hard synthesis", "dependencies": ["worker"]}
    )
    optional = plan.nodes[0].model_copy(
        update={
            "temporary_id": "optional",
            "title": "Optional synthesis",
            "optional_dependencies": ["worker"],
        }
    )
    plan.nodes = [worker, hard, optional, plan.nodes[0]]
    evaluator = _CapturingEvaluator()
    manager = await _manager(tmp_path, plan, evaluator=evaluator)
    created = await manager.create_goal(
        GoalCreateRequest(objective=objective), actor_id="test-phone"
    )
    goal = await manager._mark_start_requested(str(created["id"]))
    await manager._persist_initial_plan(goal, plan, source=PlannerSource.TEST, model_call_id=None)
    # Recreate a persisted terminal graph, without running worker code or a model.
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET status='completed',result_summary='Recorded result' WHERE goal_run_id=?",
            (created["id"],),
        )
        await db.execute(
            "UPDATE plan_nodes SET status='failed',error_summary='Recorded failure' WHERE title='Synthesis failed'"
        )
        await db.execute("UPDATE goal_runs SET step_count=4 WHERE id=?", (created["id"],))
        await db.commit()
    before = await manager.graph.list_nodes(str(created["id"]))

    await manager._evaluate_if_quiescent(str(created["id"]))

    context = evaluator.contexts[0]
    assert set(context.known_node_ids) == {node["id"] for node in before}
    assert {node.title for node in context.node_results} == {
        "Root worker",
        "Hard synthesis",
        "Optional synthesis",
        "Synthesis failed",
    }
    by_title = {node.title: node for node in context.node_results}
    assert by_title["Root worker"].node_type == "worker"
    assert by_title["Root worker"].required_skill == "workspace.list_dir"
    assert by_title["Synthesis failed"].failure_reason == "Recorded failure"
    assert await manager.graph.list_nodes(str(created["id"])) == before
    saved = await manager.graph.get_goal(str(created["id"]))
    assert saved is not None
    assert saved["step_count"] == 4
    assert saved["replan_count"] == 0
    assert saved["model_call_count"] == 1
