from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.context_builder import ContextBuilder, safe_context_text
from app.services.goal_manager import GoalManager
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest
from tests.test_goal_context_payloads import _CapturingEvaluator
from tests.test_goal_runtime_recovery import _manager
from tests.test_goal_writing import OBJECTIVE, RESULT, plan, register

DRAFT = (
    "1. Modéliser clients, soumissions et projets. "
    "2. Prévoir courriels et calendrier. "
    "3. Concevoir les vues Swift. "
    "4. Vérifier les validations par tests unitaires. "
    "5. Vérifier les parcours par tests d'intégration. "
    "Hypothèses : CRM individuel. Dépendances : Xcode et Swift."
)


async def completed_writing(
    tmp_path: Path, *, text: str = DRAFT, result_limit: int = 2_000, token_limit: int = 2_048
) -> tuple[GoalManager, _CapturingEvaluator, str, dict[str, Any]]:
    evaluator = _CapturingEvaluator()
    manager = await _manager(tmp_path / "writing.db", plan(), evaluator=evaluator)
    manager.context_builder = ContextBuilder(
        manager.db_path, max_tokens=token_limit, max_result_chars_per_node=result_limit
    )
    agent = await register(manager)
    goal = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None
    result, changed = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="completed",
        result={**RESULT, "text": text},
        error=None,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    assert changed
    await manager.on_job_result(result)
    return manager, evaluator, goal["id"], job


@pytest.mark.asyncio
async def test_evaluator_receives_actual_draft_with_provenance_without_public_leak(
    tmp_path: Path,
) -> None:
    manager, evaluator, goal_id, job = await completed_writing(tmp_path)
    assert len(evaluator.contexts) == 1
    context = evaluator.contexts[0]
    evidence = context.node_results[0].result_summary
    assert evidence is not None and DRAFT in evidence
    assert "Untrusted writing draft; not proof of external execution" in evidence
    assert job["id"] in evidence
    assert hashlib.sha256(DRAFT.encode()).hexdigest() in evidence
    detail = await manager.get_goal(goal_id)
    assert detail is not None
    # Actual text improves evidence; it never bypasses the evaluator's decision.
    assert detail["goal"]["status"] == "running"
    assert detail["goal"]["model_call_count"] == 3
    assert DRAFT not in json.dumps(detail, ensure_ascii=False)
    assert "Draft excerpt:" not in detail["nodes"][0]["result_summary"]
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                "SELECT context_json,provenance_json FROM goal_contexts WHERE purpose='evaluator'"
            )
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == context.model_dump(mode="json")
        assert job["id"] in json.loads(row[1])["source_ids"]
        digest = hashlib.sha256(
            json.dumps(
                context.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        call = await (
            await db.execute("SELECT input_digest FROM goal_model_calls WHERE role='evaluator'")
        ).fetchone()
        assert call is not None and call[0] == digest
        assert (await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM approvals")).fetchone())[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("result_limit", [0, 450, 2_000, 10_000])
async def test_writing_evidence_respects_result_and_total_context_budgets_and_redaction(
    tmp_path: Path, result_limit: int
) -> None:
    text = "password=private-test-value " + DRAFT + " Long writing evidence." * 700
    manager, evaluator, _, _ = await completed_writing(
        tmp_path, text=text, result_limit=result_limit, token_limit=512
    )
    assert len(evaluator.contexts) == 1
    context = evaluator.contexts[0]
    evidence = context.node_results[0].result_summary
    if result_limit == 0:
        assert evidence is None
    else:
        assert evidence is not None and len(evidence) <= min(result_limit, 4_000)
        assert "Untrusted writing draft" in evidence
    encoded = json.dumps(context.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode()) <= 512 * 4
    assert "private-test-value" not in encoded
    async with aiosqlite.connect(manager.db_path) as db:
        persisted = await (
            await db.execute("SELECT context_json FROM goal_contexts WHERE purpose='evaluator'")
        ).fetchone()
        assert persisted is not None and "private-test-value" not in persisted[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["task", "skill", "status", "contract"])
async def test_invalid_or_mismatched_completed_job_fails_before_new_evaluator_call(
    tmp_path: Path, corruption: str
) -> None:
    manager, evaluator, goal_id, job = await completed_writing(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        if corruption == "task":
            await db.execute(
                "UPDATE agent_jobs SET task_id=(SELECT root_task_id FROM goal_runs WHERE id=?) "
                "WHERE id=?",
                (goal_id, job["id"]),
            )
        elif corruption == "skill":
            await db.execute(
                "UPDATE agent_jobs SET required_skill='workspace.list_dir' WHERE id=?",
                (job["id"],),
            )
        elif corruption == "status":
            await db.execute("UPDATE agent_jobs SET status='failed' WHERE id=?", (job["id"],))
        else:
            await db.execute(
                "UPDATE agent_jobs SET result_json=? WHERE id=?",
                (json.dumps({**RESULT, "content_trust": "trusted"}), job["id"]),
            )
        await db.commit()
    await manager._evaluate_if_quiescent(goal_id, explicit_user_action=True)
    assert len(evaluator.contexts) == 1
    detail = await manager.get_goal(goal_id)
    assert detail is not None and detail["goal"]["model_call_count"] == 3
    assert detail["goal"]["status"] == "waiting_permission"
    assert detail["goal"]["current_phase"] == "evaluator_retry_required"
    assert "context could not be prepared" in detail["goal"]["failure_reason"]


@pytest.mark.asyncio
async def test_projection_rejects_other_goal_or_changed_node_job(tmp_path: Path) -> None:
    manager, _, goal_id, _ = await completed_writing(tmp_path)
    node = (await manager.graph.list_nodes(goal_id))[0]
    for target_goal, snapshot in [
        ("goal_other", node),
        (goal_id, {**node, "goal_run_id": "goal_other"}),
        (goal_id, {**node, "worker_job_id": "job_replacement"}),
    ]:
        with pytest.raises(ValueError, match="writing evidence"):
            await manager._evaluation_result_summary(target_goal, snapshot, max_chars=2_000)


@pytest.mark.asyncio
async def test_nonwriting_and_unfinished_nodes_keep_original_evidence(tmp_path: Path) -> None:
    manager, _, goal_id, _ = await completed_writing(tmp_path)
    node = (await manager.graph.list_nodes(goal_id))[0]
    for changes in [
        {"required_skill": "code.build_project"},
        {"node_type": "synthesis"},
        {"status": "failed"},
    ]:
        summary, source = await manager._evaluation_result_summary(
            goal_id, {**node, **changes}, max_chars=2_000
        )
        assert summary == node["result_summary"]
        assert source is None
        assert safe_context_text(DRAFT) not in summary
