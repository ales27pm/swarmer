from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.context_builder import ContextBuilder, bound_evaluation_context
from app.services.execution_engine import ExecutionEngine
from app.services.goal_project import GoalProjectService
from app.services.project_context import ProjectContextService
from app.services.swarm_contracts import GoalCreateRequest, GoalEvaluationContext, GoalStartRequest
from app.services.writing_drafts import writing_payload
from tests.test_context_builder import _seed_goal
from tests.test_evaluator import evaluation_context
from tests.test_evaluator_requirement_budget import _required_context
from tests.test_goal_context_payloads import _CapturingEvaluator, _synthesis_plan
from tests.test_goal_manager import _manager
from tests.test_project_memory import _messages


def _capsule() -> dict[str, Any]:
    guide = "Execution permissions remain server-authoritative.\nKeep required sources."
    return {
        "version": 3,
        "fingerprint": "1" * 64,
        "base_revision_id": "prev_accepted",
        "requirements": [
            {"text": "Never send email automatically.", "source_id": "gmsg_permission"},
            {"text": "Use Canadian French and discuss accessibility.", "source_id": "gmsg_locale"},
        ],
        "operating_guidance": {
            "path": "AGENTS.md",
            "sha256": hashlib.sha256(guide.encode()).hexdigest(),
            "content": guide,
        },
        "project_guidance": [
            {
                "path": "src/AGENTS.md",
                "scope": "src/",
                "source_revision_id": "prev_guide",
                "sha256": "2" * 64,
                "content": "Keep source identifiers in results.",
            }
        ],
        "experiences": {
            "items": [
                {
                    "source_id": "node_prior",
                    "worker_job_id": "job_prior",
                    "required_skill": "code.build_project",
                    "outcome": "failed",
                    "observation_kind": "measured_failure",
                    "source_revision_id": "prev_failure",
                    "source_sha256": "3" * 64,
                    "summary": "Previous check failed; this is historical evidence.",
                    "content_trust": "untrusted",
                    "applicability": "historical",
                }
            ],
            "omitted_count": 2,
        },
    }


def test_evaluator_keeps_shared_capsule_complete_before_optional_result_details() -> None:
    raw = _required_context()
    raw.durable_context = _capsule()

    bounded = bound_evaluation_context(raw, max_tokens=8_192)

    assert bounded.durable_context == raw.durable_context
    assert bounded.durable_context is not raw.durable_context
    assert bounded.objective == raw.objective
    assert bounded.completion_criteria == raw.completion_criteria


def test_writer_and_evaluator_receive_the_same_required_guidance_and_historical_facts() -> None:
    capsule = _capsule()
    raw = evaluation_context()
    raw.durable_context = capsule

    writer = writing_payload(raw.objective, [], durable_context=capsule)
    evaluator = bound_evaluation_context(raw, max_tokens=8_192)

    assert writer["durable_context"] == evaluator.durable_context == capsule
    assert writer["durable_context"] is not capsule


def test_direct_evaluator_context_rejects_unqualified_capsule() -> None:
    raw = evaluation_context().model_dump(mode="json")
    raw["durable_context"] = _capsule()
    raw["durable_context"]["experiences"]["items"][0]["applicability"] = "verified_current"

    with pytest.raises(ValueError, match="invalid agent instruction capsule"):
        GoalEvaluationContext.model_validate(raw)


@pytest.mark.asyncio
async def test_capsule_source_ids_are_indexed_without_parsing_text_as_provenance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, _ = await _seed_goal(db_path)
    builder = ContextBuilder(db_path, max_tokens=8_192)
    await builder.initialize()
    raw = evaluation_context()
    raw.goal_run_id = goal_id
    raw.durable_context = _capsule()
    raw.durable_context["requirements"][0]["text"] += " Mention node_forged in prose."

    bounded, record = await builder.build_evaluation_context(raw, provenance_ids=(goal_id,))

    assert bounded.durable_context == raw.durable_context
    assert set(record.provenance_ids) == {
        goal_id,
        "gmsg_permission",
        "gmsg_locale",
        "prev_accepted",
        "prev_guide",
        "node_prior",
        "job_prior",
        "prev_failure",
    }
    restored = await builder.get_record(record.id)
    assert restored is not None
    assert restored.payload == bounded.model_dump(mode="json")
    assert restored.provenance_ids == record.provenance_ids


@pytest.mark.asyncio
async def test_capsule_that_cannot_fit_fails_before_context_is_recorded(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, _ = await _seed_goal(db_path)
    builder = ContextBuilder(db_path, max_tokens=512)
    await builder.initialize()
    raw = evaluation_context()
    raw.goal_run_id = goal_id
    raw.durable_context = _capsule()
    raw.durable_context["requirements"][0]["text"] = (
        "Keep the original policy. " * 150 + "Never send automatically."
    )

    with pytest.raises(ValueError, match="cannot fit the configured token budget"):
        await builder.build_evaluation_context(raw)

    async with aiosqlite.connect(db_path) as db:
        count = await (await db.execute("SELECT COUNT(*) FROM goal_contexts")).fetchone()
    assert count is not None and count[0] == 0


@pytest.mark.asyncio
async def test_evaluator_receives_old_project_requirements_beyond_recent_conversation(
    tmp_path: Path,
) -> None:
    objective = "Prepare a deployment guide."
    evaluator = _CapturingEvaluator()
    plan = _synthesis_plan(objective, suffix="capsule")
    manager = await _manager(tmp_path, plan, evaluator=evaluator)
    manager.context_builder = ContextBuilder(manager.db_path, max_tokens=8_192)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager.project_applications = GoalProjectService(
        manager.db_path, ExecutionEngine(manager.db_path, workspace, manager.permission_policy)
    )
    service = ProjectContextService(manager.db_path)
    manager.project_applications.context = service
    await manager.initialize()
    goal = await manager.create_goal(GoalCreateRequest(objective=objective), actor_id="test")
    source_ids = await _messages(
        manager,
        str(goal["id"]),
        ["Never send email automatically."] + [f"Discuss section {index}." for index in range(45)],
    )
    code_payload = await manager.project_applications.payload(
        str(goal["id"]), {"id": "node_context", "objective": objective}, []
    )

    await manager.start_goal(
        str(goal["id"]), GoalStartRequest(plan_proposal=plan, planner_source="manual")
    )

    assert evaluator.contexts
    context = evaluator.contexts[-1]
    assert all(
        "Never send email automatically." not in item.content for item in context.conversation
    )
    assert context.durable_context is not None
    assert context.durable_context == code_payload["durable_context"]
    assert {
        "text": "Never send email automatically.",
        "source_id": source_ids[0],
    } in context.durable_context["requirements"]
    assert context.conversation_revision == len(source_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("change_before_read", [True, False])
async def test_reply_during_capsule_read_prevents_stale_evaluator_call(
    tmp_path: Path, change_before_read: bool
) -> None:
    objective = "Prepare a deployment guide."
    evaluator = _CapturingEvaluator()
    plan = _synthesis_plan(objective, suffix="race")
    manager = await _manager(tmp_path, plan, evaluator=evaluator)
    manager.context_builder = ContextBuilder(manager.db_path, max_tokens=8_192)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager.project_applications = GoalProjectService(
        manager.db_path, ExecutionEngine(manager.db_path, workspace, manager.permission_policy)
    )

    class ReplyDuringRead(ProjectContextService):
        async def refresh(
            self, goal_id: str, *, expected_fingerprint: str | None = None
        ) -> dict[str, Any]:
            if change_before_read:
                await _messages(manager, goal_id, ["A newer required constraint."])
            value = await super().refresh(goal_id, expected_fingerprint=expected_fingerprint)
            if not change_before_read:
                await _messages(manager, goal_id, ["A newer required constraint."])
            return value

    manager.project_applications.context = ReplyDuringRead(manager.db_path)
    await manager.initialize()
    goal = await manager.create_goal(GoalCreateRequest(objective=objective), actor_id="test")

    await manager.start_goal(
        str(goal["id"]), GoalStartRequest(plan_proposal=plan, planner_source="manual")
    )

    assert evaluator.contexts == []
    async with aiosqlite.connect(manager.db_path) as db:
        count = await (
            await db.execute("SELECT COUNT(*) FROM goal_model_calls WHERE role='evaluator'")
        ).fetchone()
    assert count is not None and count[0] == 0


@pytest.mark.parametrize("field", ["requirements", "experiences", "operating_guidance"])
def test_malformed_capsule_is_rejected_at_the_evaluator_boundary(field: str) -> None:
    raw = evaluation_context()
    raw.durable_context = _capsule()
    if field == "requirements":
        raw.durable_context[field][0]["source_id"] = "../foreign"
    elif field == "experiences":
        raw.durable_context[field]["items"][0]["applicability"] = "verified_current"
    else:
        raw.durable_context[field]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match="invalid agent instruction capsule"):
        bound_evaluation_context(raw, max_tokens=8_192)
