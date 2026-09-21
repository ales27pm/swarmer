from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft202012Validator

from app.services.evaluator_provider import EvaluatorProviderError, UbuntuEvaluatorProvider
from app.services.permission_policy import PermissionPolicy
from app.services.swarm_contracts import EvaluationDecision
from tests.test_evaluator import (
    REPO_ROOT,
    _response_for,
    continue_decision,
    evaluation_context,
    wire_decision,
)


@pytest.fixture
def policy() -> PermissionPolicy:
    return PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["continue", "done", "needs_user"])
async def test_numbered_wire_response_returns_unchanged_public_decision(
    policy: PermissionPolicy, status: str
) -> None:
    raw = continue_decision()
    raw["status"] = status
    if status != "continue":
        raw["suggested_new_nodes"] = []
    if status == "needs_user":
        raw["user_question"] = "Which document should be used?"
    wire = wire_decision(raw)
    post = AsyncMock(return_value=_response_for(json.dumps(wire, sort_keys=True)))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:1/v1", model="test", policy=policy
    )
    with patch("httpx.AsyncClient.post", post):
        actual = await provider.evaluate(evaluation_context())
    assert actual == EvaluationDecision.model_validate(raw)
    assert set(actual.model_dump()) == set(raw)
    assert post.await_count == 1
    sent = post.await_args.kwargs["json"]
    schema = sent["response_format"]["json_schema"]["schema"]
    assert Draft202012Validator(schema).is_valid(wire)
    assert not Draft202012Validator(schema).is_valid(raw)
    for branch in schema["anyOf"]:
        ordered = sorted(branch["properties"])
        assert ordered == [
            "00_schema_version",
            "10_invalid_results",
            "20_missing_requirements",
            "30_reason_summary",
            "40_status",
            "50_suggested_new_nodes",
            "60_user_question",
            "70_completion_summary",
        ]
        assert set(branch["required"]) == set(ordered)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["mixed", "unknown", "missing", "duplicate", "nested_duplicate"])
async def test_numbered_wire_response_rejects_ambiguous_or_lossy_decode(
    policy: PermissionPolicy, case: str
) -> None:
    wire = wire_decision(continue_decision())
    if case == "mixed":
        wire["status"] = "done"
    elif case == "unknown":
        wire["99_approval"] = "allow"
    elif case == "missing":
        wire.pop("60_user_question")
    content = json.dumps(wire)
    if case == "duplicate":
        content = content[:-1] + ', "40_status": "done"}'
    elif case == "nested_duplicate":
        content = content.replace(
            '"node_type": "worker"', '"node_type": "worker", "node_type": "synthesis"'
        )
    post = AsyncMock(return_value=_response_for(content))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:1/v1", model="test", policy=policy
    )
    with patch("httpx.AsyncClient.post", post), pytest.raises(EvaluatorProviderError) as raised:
        await provider.evaluate(evaluation_context())
    assert raised.value.category == "invalid_response"
    assert post.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "privileged_skill",
        "unknown_dependency",
        "terminal_nodes",
        "question",
        "oversize",
        "nested_alias",
    ],
)
async def test_numbered_wire_decoding_preserves_authoritative_validation(
    policy: PermissionPolicy, case: str
) -> None:
    raw = continue_decision()
    node = raw["suggested_new_nodes"][0]
    if case == "privileged_skill":
        node["required_skill"] = "process.run"
    elif case == "unknown_dependency":
        node["dependencies"] = ["unknown_node"]
    elif case == "terminal_nodes":
        raw["status"] = "done"
    elif case == "question":
        raw["user_question"] = "May I continue?"
    elif case == "oversize":
        raw["reason_summary"] = "x" * 4001
    elif case == "nested_alias":
        node["40_status"] = "done"
    post = AsyncMock(return_value=_response_for(json.dumps(wire_decision(raw))))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:1/v1", model="test", policy=policy
    )
    with patch("httpx.AsyncClient.post", post), pytest.raises(EvaluatorProviderError) as raised:
        await provider.evaluate(evaluation_context())
    assert raised.value.category == "invalid_response"
    assert post.await_count == 1
