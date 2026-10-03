"""Real evaluator context, provenance, and source/configuration race fences."""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import aiosqlite
import pytest

from app.models import MemoryUpdate
from app.services import goal_manager as goal_manager_module
from app.services.memory_symbolic_contracts import SymbolicEvidence
from app.services.swarm_contracts import EvaluationStatus
from tests.test_goal_context_payloads import _CapturingEvaluator
from tests.test_symbolic_context_execution import prepared


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("symbolic evaluator tests must not call a remote provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield
    assert not attempts


async def evaluation_ready(tmp_path: Path):
    manager, goal, proposal, label, first, second = await prepared(tmp_path)
    manager.evaluator = _CapturingEvaluator()
    assert manager.state_service.embedding_service is None
    assert manager.state_service.memory_normalizer is None
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            """UPDATE goal_runs SET status='running',started_at=created_at,
            step_count=1,current_phase='evaluating' WHERE id=?""",
            (goal["id"],),
        )
        await db.execute(
            """INSERT INTO plan_nodes(id,goal_run_id,node_type,title,objective,required_skill,
            status,expected_output,result_summary,conversation_revision,created_at,updated_at)
            VALUES(?,?,'worker','Inspect files','Inspect the silent clock','workspace.list_dir',
            'completed','Observed files','Observed one file in the disposable workspace',?,?,?)""",
            (
                "node_symbolic_eval",
                goal["id"],
                goal["conversation_revision"],
                goal["created_at"],
                goal["created_at"],
            ),
        )
        await db.commit()
    return manager, goal, proposal, label, first, second


async def mutate_evidence(manager, goal, second, change):
    if change == "source":
        await manager.state_service.update_memory(
            second["id"], MemoryUpdate(content="The source changed during evaluation."), "test"
        )
    elif change == "catalogs":
        manager.strategy_retrieval.symbolic_catalogs = ()
    else:
        assert change == "project"
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute(
                "INSERT INTO coding_projects VALUES('different-project','2026','2026')"
            )
            await db.execute(
                "UPDATE goal_project_links SET project_id='different-project' WHERE goal_run_id=?",
                (goal["id"],),
            )
            await db.commit()


async def assert_decision_not_applied(manager, goal_id):
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await (
                await db.execute("SELECT id FROM goal_evaluations WHERE goal_run_id=?", (goal_id,))
            ).fetchall()
            == []
        )
        assert await (
            await db.execute(
                "SELECT status FROM goal_model_calls WHERE goal_run_id=? AND role='evaluator'",
                (goal_id,),
            )
        ).fetchall() == [("failed",)]
        assert await (
            await db.execute("SELECT id,status FROM plan_nodes WHERE goal_run_id=?", (goal_id,))
        ).fetchall() == [("node_symbolic_eval", "completed")]
        assert await (await db.execute("SELECT id FROM agent_jobs")).fetchall() == []


@pytest.mark.asyncio
async def test_symbolic_evidence_reaches_evaluator_and_exact_recorded_provenance(tmp_path):
    manager, goal, proposal, _, first, second = await evaluation_ready(tmp_path)
    await manager._evaluate_if_quiescent(goal["id"], explicit_user_action=True)
    [context] = manager.evaluator.contexts
    assert context.symbolic_context is not None
    assert context.symbolic_context.status == "available"
    [evidence] = context.symbolic_context.evidence
    assert isinstance(evidence, SymbolicEvidence)
    assert evidence.proposal == proposal
    assert evidence.validation_status == "unvalidated" and evidence.grants_authority is False
    assert {source.binding.memory_id for source in evidence.proposal.sources} == {
        first["id"],
        second["id"],
    }
    assert evidence.proposal.claim.polarity == "negated"
    assert evidence.proposal.claim.modality == "forbidden"
    assert (
        evidence.proposal.claim.effective_conditions[0].argument.lexical_value == "Cache/State.py"
    )
    payload = context.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()
    async with aiosqlite.connect(manager.db_path) as db:
        [saved] = await (
            await db.execute(
                "SELECT context_json,provenance_json FROM goal_contexts WHERE purpose='evaluator'"
            )
        ).fetchall()
        assert json.loads(saved[0]) == payload
        provenance = json.loads(saved[1])
        assert provenance["symbolic_context"] == payload["symbolic_context"]
        assert {proposal.proposal_id, first["id"], second["id"]}.issubset(provenance["source_ids"])
        assert await (
            await db.execute(
                "SELECT input_digest,status FROM goal_model_calls WHERE role='evaluator'"
            )
        ).fetchall() == [(digest, "completed")]
        assert await (
            await db.execute(
                "SELECT status FROM goal_evaluations WHERE goal_run_id=?", (goal["id"],)
            )
        ).fetchall() == [("continue",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "project", "catalogs"])
async def test_changed_symbolic_context_after_reservation_never_calls_evaluator(
    tmp_path, monkeypatch, change
):
    manager, goal, _, _, _, second = await evaluation_ready(tmp_path)
    original = manager._reserve_model_call

    async def reserve_then_change(*args, **kwargs):
        receipt = await original(*args, **kwargs)
        await mutate_evidence(manager, goal, second, change)
        return receipt

    monkeypatch.setattr(manager, "_reserve_model_call", reserve_then_change)
    await manager._evaluate_if_quiescent(goal["id"], explicit_user_action=True)
    assert manager.evaluator.contexts == []
    await assert_decision_not_applied(manager, goal["id"])


@pytest.mark.asyncio
async def test_source_changed_while_evaluator_awaits_never_applies_decision(tmp_path, monkeypatch):
    manager, goal, proposal, _, _, second = await evaluation_ready(tmp_path)
    original = manager.evaluator.evaluate

    async def evaluate_then_change(context):
        decision = await original(context)
        await mutate_evidence(manager, goal, second, "source")
        return decision

    monkeypatch.setattr(manager.evaluator, "evaluate", evaluate_then_change)
    await manager._evaluate_if_quiescent(goal["id"], explicit_user_action=True)
    [context] = manager.evaluator.contexts
    assert context.symbolic_context.evidence[0].proposal == proposal
    await assert_decision_not_applied(manager, goal["id"])


@pytest.mark.asyncio
async def test_source_changed_during_final_goal_read_never_applies_decision(tmp_path, monkeypatch):
    manager, goal, _, _, _, second = await evaluation_ready(tmp_path)
    original = manager.graph.get_goal
    changed = False

    async def read_then_change(*args, **kwargs):
        nonlocal changed
        current = await original(*args, **kwargs)
        if manager.evaluator.contexts and not changed:
            changed = True
            await mutate_evidence(manager, goal, second, "source")
        return current

    monkeypatch.setattr(manager.graph, "get_goal", read_then_change)
    await manager._evaluate_if_quiescent(goal["id"], explicit_user_action=True)
    assert changed
    assert len(manager.evaluator.contexts) == 1
    await assert_decision_not_applied(manager, goal["id"])


@pytest.mark.asyncio
async def test_catalogs_changed_during_terminal_audit_roll_back_evaluation(tmp_path, monkeypatch):
    manager, goal, _, _, _, _ = await evaluation_ready(tmp_path)
    original_evaluate = manager.evaluator.evaluate
    original_audit = goal_manager_module.append_audit_event
    changed = False

    async def evaluate_terminal(context):
        decision = await original_evaluate(context)
        return decision.model_copy(update={"status": EvaluationStatus.FAILED})

    async def audit_then_change(db, event_type, *args, **kwargs):
        nonlocal changed
        receipt = await original_audit(db, event_type, *args, **kwargs)
        if event_type == "goal.failed":
            changed = True
            manager.strategy_retrieval.symbolic_catalogs = ()
        return receipt

    monkeypatch.setattr(manager.evaluator, "evaluate", evaluate_terminal)
    monkeypatch.setattr(goal_manager_module, "append_audit_event", audit_then_change)
    await manager._evaluate_if_quiescent(goal["id"], explicit_user_action=True)
    assert changed
    assert len(manager.evaluator.contexts) == 1
    await assert_decision_not_applied(manager, goal["id"])
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT status FROM goal_runs WHERE id=?", (goal["id"],))
        ).fetchall() == [("running",)]
        assert (
            await (
                await db.execute(
                    "SELECT event_type FROM audit_events WHERE event_type='goal.failed'"
                )
            ).fetchall()
            == []
        )
