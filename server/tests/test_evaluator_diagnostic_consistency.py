from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft202012Validator

from app.services.evaluator_provider import EvaluatorProviderError, UbuntuEvaluatorProvider
from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import validate_evaluation_decision
from tests.test_evaluator import (
    REPO_ROOT,
    _response_for,
    continue_decision,
    evaluation_context,
    wire_decision,
)
from tests.test_evaluator_recovery import expire_cooldown, failed_worker_goal
from tests.test_goal_runtime_recovery import _manager, _worker_plan


def decision(status: str, missing: list[str], invalid: list[str]) -> dict[str, object]:
    return {
        **continue_decision(),
        "status": status,
        "reason_summary": "Assessment of the recorded result.",
        "missing_requirements": missing,
        "invalid_results": invalid,
        "suggested_new_nodes": [],
        "completion_summary": "The requested text was delivered." if status == "done" else None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize(
    ("status", "missing", "invalid", "message"),
    [
        ("done", ["No research evidence was returned."], [], "unresolved diagnostics"),
        ("done", [], ["The cited source concerns another subject."], "unresolved diagnostics"),
        (
            "done",
            [],
            ["The writing worker declined the requested draft."],
            "unresolved diagnostics",
        ),
        ("failed", [], [], "structured diagnostic"),
    ],
)
async def test_provider_rejects_contradictory_terminal_diagnostics_once(
    wire: bool, status: str, missing: list[str], invalid: list[str], message: str
) -> None:
    raw = decision(status, missing, invalid)
    content = json.dumps(wire_decision(raw) if wire else raw)
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    post = AsyncMock(return_value=_response_for(content))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:1/v1", model="test", policy=policy
    )
    with patch("httpx.AsyncClient.post", post), pytest.raises(EvaluatorProviderError) as raised:
        await provider.evaluate(evaluation_context())
    assert message in str(raised.value)
    assert raised.value.category == "invalid_response"
    assert raised.value.diagnostic == "schema"
    assert raised.value.output_digest == hashlib.sha256(content.encode()).hexdigest()
    assert post.await_count == 1
    schema = post.await_args.kwargs["json"]["response_format"]["json_schema"]["schema"]
    assert not Draft202012Validator(schema).is_valid(wire_decision(raw))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "missing", "invalid"),
    [
        ("done", [], []),
        ("failed", ["Required evidence remains unavailable."], []),
        ("failed", [], ["The worker explicitly declined the requested text."]),
        ("failed", ["The requested text is missing."], ["The worker declined."]),
        ("continue", [], []),
    ],
)
async def test_provider_preserves_consistent_decisions_without_inventing_recovery(
    status: str, missing: list[str], invalid: list[str]
) -> None:
    raw = decision(status, missing, invalid)
    post = AsyncMock(return_value=_response_for(json.dumps(wire_decision(raw))))
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:1/v1", model="test", policy=policy
    )
    with patch("httpx.AsyncClient.post", post):
        result = await provider.evaluate(evaluation_context())
    assert result.model_dump(mode="json") == raw
    assert post.await_count == 1
    schema = post.await_args.kwargs["json"]["response_format"]["json_schema"]["schema"]
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(wire_decision(raw))


def test_existing_server_decision_contract_is_unchanged() -> None:
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    raw = decision("failed", [], [])
    result = validate_evaluation_decision(raw, policy=policy)
    assert result.decision.model_dump(mode="json") == raw


@pytest.mark.asyncio
async def test_diagnostic_rejection_keeps_paid_attempts_cooldown_and_pause(tmp_path: Path) -> None:
    policy = PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:1/v1", model="test", policy=policy
    )
    manager = await _manager(tmp_path / "state.db", _worker_plan(), evaluator=provider)
    raw = decision("failed", [], [])
    post = AsyncMock(return_value=_response_for(json.dumps(wire_decision(raw))))
    with patch("httpx.AsyncClient.post", post):
        goal_id = await failed_worker_goal(manager)
        before = await manager.graph.get_goal(goal_id)
        for attempt in range(1, 4):
            await expire_cooldown(manager, goal_id)
            assert await manager.reconcile() == 1
            assert post.await_count == attempt
            assert await manager.reconcile() == 0
            assert post.await_count == attempt
    after = await manager.graph.get_goal(goal_id)
    assert before and after
    assert after["model_call_count"] == before["model_call_count"] + 3
    assert after["status"] == "waiting_permission"
    assert after["current_phase"] == "evaluator_retry_required"
    assert after["paused_at"]
    assert after["step_count"] == before["step_count"]
    assert after["replan_count"] == before["replan_count"]
