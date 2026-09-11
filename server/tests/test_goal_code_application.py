from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from app.models import AgentCreate
from app.services.approval_gateway import ApprovalConflict, ApprovalGateway
from app.services.evaluator_provider import DeterministicEvaluatorProvider
from app.services.execution_engine import AuthenticatedRequester, ExecutionConflict, ExecutionEngine
from app.services.goal_code_application import (
    GoalCodeApplicationConflict,
    GoalCodeApplicationService,
)
from app.services.goal_manager import GoalManager
from app.services.state_service import SCHEMA_VERSION, StateService
from app.services.swarm_contracts import (
    EvaluationDecision,
    EvaluationStatus,
    GoalCreateRequest,
    GoalStartRequest,
    SwarmPlanProposal,
)
from tests.test_goal_runtime_recovery import _manager, _worker_plan

CONTENT = '"""Private generated application marker."""\nprint("CRM prototype")\n'
REQUESTER = AuthenticatedRequester(id="test-phone", name="Test phone")


def _coding_plan() -> SwarmPlanProposal:
    plan = _worker_plan(objective="Create a Python CRM prototype")
    plan.nodes[0] = plan.nodes[0].model_copy(
        update={
            "required_skill": "code.generate_python",
            "objective": "Create a Python CRM prototype",
            "title": "Propose a Python application",
            "expected_output": "A reviewed Python file",
        }
    )
    return plan


async def _coding_manager(tmp_path: Path) -> tuple[GoalManager, ExecutionEngine]:
    decision = EvaluationDecision(
        schema_version="1.0",
        status=EvaluationStatus.DONE,
        reason_summary="The approved file was written; application behavior has not been tested.",
        missing_requirements=[],
        invalid_results=[],
        suggested_new_nodes=[],
    )
    manager = await _manager(
        tmp_path / "code.db", _coding_plan(), evaluator=DeterministicEvaluatorProvider(decision)
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    engine = ExecutionEngine(manager.db_path, workspace, manager.permission_policy)
    manager.code_applications = GoalCodeApplicationService(manager.db_path, engine)
    return manager, engine


async def _start(manager: GoalManager, *, max_model_calls: int = 10) -> dict[str, Any]:
    goal = await manager.create_goal(
        GoalCreateRequest(
            objective="Create a Python CRM prototype", max_model_calls=max_model_calls
        ),
        actor_id=REQUESTER.id,
    )
    return await manager.start_goal(str(goal["id"]), GoalStartRequest())


async def _generate(manager: GoalManager, detail: dict[str, Any]) -> dict[str, Any]:
    registration = await manager.state_service.register_agent(
        AgentCreate(
            name="Python proposal worker",
            endpoint="https://worker.invalid",
            skills=["code.generate_python"],
        ),
        REQUESTER.id,
    )
    agent_id = str(registration["id"])
    await manager.state_service.heartbeat_agent(agent_id, "online", str(registration["credential"]))
    claimed = await manager.agent_dispatcher.claim(agent_id)
    assert claimed is not None and claimed["id"] == detail["nodes"][0]["worker_job_id"]
    job, changed = await manager.agent_dispatcher.submit_result(
        agent_id,
        str(claimed["id"]),
        str(claimed["claim_token"]),
        status="completed",
        result={
            "schema_version": "1.0",
            "path": "app.py",
            "content": CONTENT,
            "summary": "A bounded Python CRM prototype proposal.",
        },
        error=None,
        lease_id=str(claimed["lease_id"]),
        lease_generation=int(claimed["lease_generation"]),
    )
    assert changed
    captured = await manager.on_job_result(job)
    assert captured is not None
    return job


async def _prepared(tmp_path: Path) -> tuple[GoalManager, ExecutionEngine, str, str]:
    manager, engine = await _coding_manager(tmp_path)
    detail = await _start(manager)
    await _generate(manager, detail)
    return manager, engine, str(detail["goal"]["id"]), str(detail["nodes"][0]["id"])


async def _apply(manager: GoalManager, goal_id: str, node_id: str) -> dict[str, Any]:
    assert manager.code_applications is not None
    return await manager.code_applications.apply(
        goal_id,
        node_id,
        requester=REQUESTER,
        reviewed_sha256=hashlib.sha256(CONTENT.encode()).hexdigest(),
    )


@pytest.mark.asyncio
async def test_codegen_is_inert_until_review_approval_and_verified_write(tmp_path: Path) -> None:
    manager, engine = await _coding_manager(tmp_path)
    initial = await _start(manager)
    goal_id, node_id = str(initial["goal"]["id"]), str(initial["nodes"][0]["id"])
    assert initial["goal"]["model_call_count"] == 2
    job = await _generate(manager, initial)
    assert manager.code_applications is not None
    preview = await manager.code_applications.get_proposal(goal_id, node_id)
    assert preview is not None
    assert preview["content"] == CONTENT
    assert preview["path"] == f"generated/{goal_id}/{node_id}/app.py"
    assert preview["status"] == "proposal"
    target = engine.workspace_root / preview["path"]
    assert not target.exists()
    waiting = await manager.get_goal(goal_id)
    assert waiting is not None
    assert waiting["goal"]["status"] == waiting["nodes"][0]["status"] == "waiting_permission"
    assert waiting["goal"]["model_call_count"] == 2
    assert CONTENT not in json.dumps(waiting)
    assert CONTENT not in json.dumps(await manager.state_service.bootstrap())
    assert await manager.on_job_result(job) == waiting
    assert await manager.reconcile() == 0

    call = await _apply(manager, goal_id, node_id)
    assert call["task_id"] not in {initial["goal"]["root_task_id"], initial["nodes"][0]["task_id"]}
    assert call["status"] == "waiting_permission"
    assert not target.exists()
    assert CONTENT not in json.dumps(call)
    gateway = ApprovalGateway(manager.db_path)
    approval = await gateway.get(str(call["approval_id"]))
    assert approval is not None
    assert approval["binding_valid"] and approval["consent_context_valid"]
    assert approval["requester"]["id"] == REQUESTER.id
    await gateway.decide(str(call["approval_id"]), "approve", actor_id=REQUESTER.id)
    assert not target.exists()
    receipt = await engine.execute(str(call["id"]))
    assert receipt["status"] == "completed"
    assert target.read_text() == CONTENT
    assert (await manager.graph.get_node(node_id))["status"] == "waiting_permission"

    completed = await manager.on_tool_call_updated(str(call["id"]))
    assert completed is not None
    assert completed["nodes"][0]["status"] == completed["goal"]["status"] == "completed"
    assert completed["goal"]["model_call_count"] == 3
    assert "not been executed or tested" in completed["nodes"][0]["result_summary"]
    assert CONTENT not in json.dumps(completed)
    assert (await _apply(manager, goal_id, node_id))["id"] == call["id"]
    with pytest.raises(ApprovalConflict):
        await gateway.decide(str(call["approval_id"]), "approve", actor_id=REQUESTER.id)
    with pytest.raises(ExecutionConflict):
        await engine.execute(str(call["id"]))


@pytest.mark.asyncio
async def test_codegen_budget_reservation_is_once_and_dispatch_requires_credit(
    tmp_path: Path,
) -> None:
    manager, _ = await _coding_manager(tmp_path)
    exhausted = await _start(manager, max_model_calls=1)
    assert exhausted["goal"]["status"] == "budget_exhausted"
    assert exhausted["goal"]["model_call_count"] == 1
    assert exhausted["nodes"][0]["worker_job_id"] is None
    normal = await _start(manager)
    await manager.reconcile()
    await manager.start_goal(str(normal["goal"]["id"]), GoalStartRequest())
    record = await manager.graph.get_goal(str(normal["goal"]["id"]))
    assert record is not None and record["model_call_count"] == 2
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT max_attempts FROM agent_jobs")).fetchall() == [(1,)]
        assert await (
            await db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type='goal.codegen.reserved'"
            )
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_review_hash_and_goal_binding_fail_before_creating_application(
    tmp_path: Path,
) -> None:
    manager, _, goal_id, node_id = await _prepared(tmp_path)
    assert manager.code_applications is not None
    with pytest.raises(GoalCodeApplicationConflict, match="digest"):
        await manager.code_applications.apply(goal_id, node_id, REQUESTER, "0" * 64)
    with pytest.raises(GoalCodeApplicationConflict, match="not found"):
        await manager.code_applications.apply(
            "goal_other", node_id, REQUESTER, hashlib.sha256(CONTENT.encode()).hexdigest()
        )
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone() == (0,)
        assert await (
            await db.execute("SELECT apply_task_id FROM goal_code_proposals")
        ).fetchone() == (None,)


@pytest.mark.asyncio
async def test_competing_apply_requests_share_one_task_call_and_approval(tmp_path: Path) -> None:
    manager, engine, goal_id, node_id = await _prepared(tmp_path)
    other = GoalCodeApplicationService(manager.db_path, engine)
    results = await asyncio.gather(
        _apply(manager, goal_id, node_id),
        other.apply(goal_id, node_id, REQUESTER, hashlib.sha256(CONTENT.encode()).hexdigest()),
    )
    assert results[0]["id"] == results[1]["id"]
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone() == (1,)
        assert await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["denied", "expired", "cancelled", "budget_exhausted"])
async def test_unapproved_or_expired_applications_never_write(tmp_path: Path, outcome: str) -> None:
    manager, engine, goal_id, node_id = await _prepared(tmp_path)
    call = await _apply(manager, goal_id, node_id)
    gateway = ApprovalGateway(manager.db_path)
    if outcome == "denied":
        await gateway.decide(str(call["approval_id"]), "deny", actor_id=REQUESTER.id)
    elif outcome == "expired":
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute(
                "UPDATE approvals SET expires_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call["approval_id"]),
            )
            await db.commit()
    elif outcome == "cancelled":
        await manager.cancel_goal(goal_id, actor_id=REQUESTER.id)
    else:
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute(
                "UPDATE goal_runs SET started_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(days=2)).isoformat(), goal_id),
            )
            await db.commit()
    await manager.reconcile()
    with pytest.raises((ApprovalConflict, ExecutionConflict)):
        await gateway.decide(str(call["approval_id"]), "approve", actor_id=REQUESTER.id)
    with pytest.raises(ExecutionConflict):
        await engine.execute(str(call["id"]))
    assert list(engine.workspace_root.iterdir()) == []
    assert (await manager.graph.get_node(node_id))["status"] in {"failed", "cancelled"}


@pytest.mark.asyncio
async def test_cancellation_between_application_child_and_tool_creation_fences_call(
    tmp_path: Path,
) -> None:
    manager, engine, goal_id, node_id = await _prepared(tmp_path)
    original = engine.create_tool_call

    async def cancelled_create(**kwargs: Any) -> dict[str, Any]:
        await manager.cancel_goal(goal_id, actor_id=REQUESTER.id)
        return await original(**kwargs)

    with (
        patch.object(engine, "create_tool_call", side_effect=cancelled_create),
        pytest.raises(ExecutionConflict),
    ):
        await _apply(manager, goal_id, node_id)
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone() == (0,)
        assert await (
            await db.execute(
                "SELECT t.status FROM tasks AS t JOIN goal_code_proposals AS p ON p.apply_task_id=t.id"
            )
        ).fetchone() == ("cancelled",)
    assert list(engine.workspace_root.iterdir()) == []


@pytest.mark.asyncio
async def test_restart_recovers_prepared_application_and_terminal_receipt_without_replay(
    tmp_path: Path,
) -> None:
    manager, engine, goal_id, node_id = await _prepared(tmp_path)
    with (
        patch.object(
            engine, "create_tool_call", new=AsyncMock(side_effect=RuntimeError("interrupted"))
        ),
        pytest.raises(RuntimeError, match="interrupted"),
    ):
        await _apply(manager, goal_id, node_id)
    async with aiosqlite.connect(manager.db_path) as db:
        prepared_child = await (
            await db.execute(
                "SELECT apply_task_id FROM goal_code_proposals WHERE node_id=?", (node_id,)
            )
        ).fetchone()
    assert prepared_child is not None and prepared_child[0] is not None
    restarted, second_engine = await _coding_manager(tmp_path)
    assert restarted.code_applications is not None
    review = await restarted.code_applications.get_proposal(goal_id, node_id)
    assert review is not None
    assert review["status"] == "proposal"
    assert review["task_id"] is None
    call = await _apply(restarted, goal_id, node_id)
    assert call["task_id"] == prepared_child[0]
    await ApprovalGateway(manager.db_path).decide(
        str(call["approval_id"]), "approve", actor_id=REQUESTER.id
    )
    await second_engine.execute(str(call["id"]))
    final_manager, _ = await _coding_manager(tmp_path)

    assert await final_manager.reconcile() >= 1

    assert (await final_manager.graph.get_node(node_id))["status"] == "completed"
    assert (await _apply(final_manager, goal_id, node_id))["id"] == call["id"]
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_new_application_requires_explicit_one_use_approval_policy(
    tmp_path: Path,
    decision: str,
) -> None:
    manager, engine, goal_id, node_id = await _prepared(tmp_path)
    original = engine.policy.evaluate_tool("workspace.write_text")
    rule = replace(original, decision=decision, approval_ttl_seconds=None)
    with (
        patch.object(engine.policy, "evaluate_tool", return_value=rule),
        pytest.raises(GoalCodeApplicationConflict, match="one-use write approval"),
    ):
        await _apply(manager, goal_id, node_id)
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT apply_task_id FROM goal_code_proposals")
        ).fetchone() == (None,)
        assert await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone() == (0,)


@pytest.mark.asyncio
async def test_approved_write_cannot_execute_after_goal_deadline_before_maintenance(
    tmp_path: Path,
) -> None:
    manager, engine, goal_id, node_id = await _prepared(tmp_path)
    call = await _apply(manager, goal_id, node_id)
    await ApprovalGateway(manager.db_path).decide(
        str(call["approval_id"]), "approve", actor_id=REQUESTER.id
    )
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET started_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(days=2)).isoformat(), goal_id),
        )
        await db.commit()

    with pytest.raises(ExecutionConflict, match="goal is no longer executable"):
        await engine.execute(str(call["id"]))

    assert list(engine.workspace_root.iterdir()) == []
    assert (await engine.get(str(call["id"])))["status"] == "queued"


@pytest.mark.asyncio
async def test_v020_upgrade_adds_code_proposals_without_changing_goal_budget(
    tmp_path: Path,
) -> None:
    manager, _ = await _coding_manager(tmp_path)
    detail = await _start(manager)
    before = await manager.graph.get_goal(str(detail["goal"]["id"]))
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DROP TABLE goal_code_proposals")
        await db.execute("PRAGMA user_version=20")
        await db.commit()
    await StateService(manager.db_path, permission_policy=manager.permission_policy).initialize()
    assert await manager.graph.get_goal(str(detail["goal"]["id"])) == before
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (await db.execute("PRAGMA user_version")).fetchone() == (SCHEMA_VERSION,)
        assert await (await db.execute("PRAGMA foreign_key_check")).fetchall() == []
        assert await (await db.execute("SELECT COUNT(*) FROM goal_code_proposals")).fetchone() == (
            0,
        )
