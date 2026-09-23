from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from app.services.goal_manager import GoalManagerConflict
from app.services.plan_validation import PlanValidationError, parse_swarm_plan_json
from app.services.planner_provider import UbuntuSwarmPlannerProvider
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan
from tests.test_plan_validation import valid_plan
from tests.test_planner_dependency_contract import response_for


@pytest.mark.parametrize(
    ("defect", "code"),
    [
        ("unknown", "unknown_dependency"),
        ("self", "self_dependency"),
        ("cycle", "cyclic_dependencies"),
        ("duplicate", "duplicate_node"),
        ("repeat", "repeated_dependency"),
        ("fields", "invalid_fields"),
    ],
)
def test_parser_exposes_stable_diagnostics_without_model_identifiers(defect, code):
    plan = valid_plan()
    if defect == "unknown":
        plan["nodes"][-1]["dependencies"] = ["private_dependency_do_not_persist"]
    elif defect == "self":
        plan["nodes"][0]["dependencies"] = ["inventory"]
    elif defect == "cycle":
        plan["nodes"][0]["dependencies"] = ["synthesis"]
    elif defect == "duplicate":
        plan["nodes"][1]["temporary_id"] = "inventory"
    elif defect == "repeat":
        plan["nodes"][-1]["dependencies"] = ["inventory", "inventory"]
    else:
        plan["nodes"][0]["priority"] = "private_invalid_priority"
    with pytest.raises(PlanValidationError) as caught:
        parse_swarm_plan_json(json.dumps(plan))
    assert caught.value.diagnostic_code == code


async def test_invalid_plan_records_safe_diagnostic_and_next_charged_attempt_receives_it(
    tmp_path: Path,
):
    manager = await _manager(tmp_path / "diagnostics.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Summarize the public project documentation"),
        actor_id="test-phone",
    )
    plan = _worker_plan(objective=f"goal:{goal['id']}").model_dump(mode="json")
    plan["nodes"][0].update(
        node_type="synthesis",
        required_skill=None,
        dependencies=["private_dependency_do_not_persist"],
    )
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:8711/v1", model="test")
    manager.planner = provider
    post = AsyncMock(return_value=response_for(plan))
    with patch("httpx.AsyncClient.post", post):
        with pytest.raises(GoalManagerConflict) as caught:
            await manager.start_goal(goal["id"], GoalStartRequest())
        assert "private_dependency" not in str(caught.value)
        assert await manager.reconcile() == 0
    assert post.await_count == 1
    failed = await manager.graph.get_goal(goal["id"])
    assert "dépendance inconnue" in failed["failure_reason"]
    assert await manager.graph.list_nodes(goal["id"]) == []
    async with aiosqlite.connect(manager.db_path) as db:
        events = await (
            await db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='goal.plan.rejected'"
            )
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0][0])
        assert payload["validation_code"] == "unknown_dependency"
        assert payload["category"] == "invalid_response"
        assert "private_dependency" not in events[0][0]
        await db.execute(
            "UPDATE goal_runs SET updated_at=? WHERE id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=61)).isoformat(),
                goal["id"],
            ),
        )
        await db.commit()
    captured = []

    class RecoveredPlanner:
        source = provider.source

        async def propose(self, context):
            captured.append(context)
            return _worker_plan(objective=f"goal:{goal['id']}")

    manager.planner = RecoveredPlanner()
    await manager.reconcile()
    assert len(captured) == 1
    feedback = captured[0]["planner_validation_feedback"]
    assert feedback["code"] == "unknown_dependency"
    assert "temporary_id" in feedback["instruction"]
    assert "private_dependency" not in json.dumps(feedback)
    recovered = await manager.graph.get_goal(goal["id"])
    assert recovered["status"] == "running"
    assert recovered["model_call_count"] == 2
    assert recovered["started_at"] == failed["started_at"]


async def test_diagnostic_audit_is_idempotent_and_cannot_accept_arbitrary_text(tmp_path: Path):
    manager = await _manager(tmp_path / "audit.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Summarize documentation"), actor_id="test-phone"
    )
    await manager._mark_start_requested(goal["id"])
    call = await manager._reserve_model_call(
        goal["id"],
        role="planner",
        conversation_revision=0,
        context_id=None,
        input_digest="a" * 64,
        provider_source="test",
        model_id=None,
    )
    assert await manager._record_planner_failure(
        goal["id"], call, category="invalid_response", diagnostic_code="private-model-text"
    )
    assert not await manager._record_planner_failure(
        goal["id"], call, category="invalid_response", diagnostic_code="unknown_dependency"
    )
    async with aiosqlite.connect(manager.db_path) as db:
        rows = await (
            await db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='goal.plan.rejected'"
            )
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["validation_code"] is None
    assert "private-model-text" not in rows[0][0]


@pytest.mark.parametrize("replacement", ["new_message", "success", "transport", "unknown_code"])
async def test_old_or_unclassified_failure_cannot_supply_repair_guidance(
    tmp_path: Path, replacement
):
    manager = await _manager(tmp_path / "feedback.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Summarize documentation"), actor_id="test-phone"
    )
    await manager._mark_start_requested(goal["id"])
    call = await manager._reserve_model_call(
        goal["id"],
        role="planner",
        conversation_revision=0,
        context_id=None,
        input_digest="a" * 64,
        provider_source="test",
        model_id=None,
    )
    await manager._record_planner_failure(
        goal["id"], call, category="invalid_response", diagnostic_code="unknown_dependency"
    )
    current = await manager.graph.get_goal(goal["id"])
    assert (await manager._planner_validation_feedback(current))["code"] == "unknown_dependency"
    async with aiosqlite.connect(manager.db_path) as db:
        if replacement == "new_message":
            await db.execute(
                "UPDATE goal_runs SET conversation_revision=1 WHERE id=?", (goal["id"],)
            )
        elif replacement == "success":
            await db.execute("UPDATE goal_model_calls SET status='completed' WHERE id=?", (call,))
        elif replacement == "transport":
            await db.execute(
                "UPDATE goal_model_calls SET error_category='transport_unavailable' WHERE id=?",
                (call,),
            )
        else:
            await db.execute(
                "UPDATE audit_events SET payload_json=json_set(payload_json,'$.validation_code',?) WHERE event_type='goal.plan.rejected'",
                ("private-model-instruction",),
            )
        await db.commit()
    current = await manager.graph.get_goal(goal["id"])
    assert await manager._planner_validation_feedback(current) is None


async def test_late_rejection_cannot_overwrite_new_user_revision_or_append_audit(tmp_path: Path):
    manager = await _manager(tmp_path / "late.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Summarize documentation"), actor_id="test-phone"
    )
    await manager._mark_start_requested(goal["id"])
    call = await manager._reserve_model_call(
        goal["id"],
        role="planner",
        conversation_revision=0,
        context_id=None,
        input_digest="a" * 64,
        provider_source="test",
        model_id=None,
    )
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE goal_runs SET conversation_revision=1 WHERE id=?", (goal["id"],))
        await db.commit()
    assert not await manager._record_planner_failure(
        goal["id"], call, category="invalid_response", diagnostic_code="unknown_dependency"
    )
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute(
                "SELECT count(*) FROM audit_events WHERE event_type='goal.plan.rejected'"
            )
        ).fetchone() == (0,)
