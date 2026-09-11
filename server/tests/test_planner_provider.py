import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.plan_validation import PlanValidationError
from app.services.planner_provider import (
    NoopPlannerProvider,
    SwarmPlannerProviderError,
    UbuntuLLMPlannerProvider,
    UbuntuSwarmPlannerProvider,
)


def _planner_context() -> dict[str, object]:
    return {
        "cards": [
            {
                "kind": "goal",
                "card_id": "goal:goal_current",
                "summary": "Create a bounded plan.",
            },
            {"kind": "episode", "card_id": "episode:older", "summary": "Prior failure."},
        ]
    }


@pytest.mark.asyncio
async def test_ubuntu_provider_preserves_current_planner_contract() -> None:
    orchestrator = AsyncMock()
    orchestrator.plan.return_value = {"tool_name": "none", "arguments": {}, "summary": "ok"}
    provider = UbuntuLLMPlannerProvider(orchestrator)
    assert provider.source == "ubuntu_local"
    assert await provider.plan("talk", "normal") == orchestrator.plan.return_value
    orchestrator.plan.assert_awaited_once_with("talk", "normal")


@pytest.mark.asyncio
async def test_noop_provider_is_inert_and_test_labeled() -> None:
    provider = NoopPlannerProvider()
    assert provider.source == "test"
    assert (await provider.plan("anything"))["tool_name"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_length", [4000, 4001])
async def test_swarm_planner_wire_schema_preserves_local_string_limits(
    summary_length: int,
) -> None:
    proposal = {
        "schema_version": "1.0",
        "objective": "goal:goal_current",
        "rationale_summary": "x" * summary_length,
        "completion_criteria": ["Evidence is available."],
        "max_parallelism": 1,
        "nodes": [
            {
                "temporary_id": "files",
                "node_type": "worker",
                "title": "List files",
                "objective": "Inspect the root listing.",
                "required_skill": "workspace.list_dir",
                "dependencies": [],
                "expected_output": "A root listing.",
                "priority": 1,
            }
        ],
    }
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:8711/v1/chat/completions"),
        json={"choices": [{"message": {"content": json.dumps(proposal)}}]},
    )
    post = AsyncMock(return_value=response)
    provider = UbuntuSwarmPlannerProvider(
        base_url="http://127.0.0.1:8711/v1", model="local-planner"
    )
    with patch("httpx.AsyncClient.post", post):
        if summary_length == 4000:
            result = await provider.propose(_planner_context())
            assert result.rationale_summary == proposal["rationale_summary"]
        else:
            with pytest.raises(SwarmPlannerProviderError, match="invalid proposal") as raised:
                await provider.propose(_planner_context())
            assert raised.value.category == "invalid_response"
            assert isinstance(raised.value.__cause__, PlanValidationError)
            assert "at most 4000 characters" in str(raised.value.__cause__)

    assert post.await_count == 1
    assert post.await_args is not None
    wire = post.await_args.kwargs["json"]["response_format"]
    assert wire["type"] == "json_schema"
    assert wire["json_schema"]["strict"] is True
    assert '"maxLength"' not in json.dumps(wire["json_schema"]["schema"])
    assert wire["json_schema"]["schema"]["properties"]["objective"]["const"] == "goal:goal_current"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (400, "request_rejected"),
        (404, "request_rejected"),
        (408, "transport_unavailable"),
        (429, "transport_unavailable"),
        (503, "transport_unavailable"),
    ],
)
async def test_planner_classifies_http_failures_without_exposing_provider_body(
    status_code: int, category: str
) -> None:
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "http://127.0.0.1:8711/v1/chat/completions"),
        json={"error": {"message": "private-provider-response"}},
    )
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:8711/v1", model="local")
    with (
        patch("httpx.AsyncClient.post", AsyncMock(return_value=response)),
        pytest.raises(SwarmPlannerProviderError) as raised,
    ):
        await provider.propose(_planner_context())
    assert raised.value.category == category
    assert "private-provider-response" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": [{"message": {"content": {"private": "provider-response"}}}]},
        {"choices": [{"message": {"content": '{"private":"provider-response"}'}}]},
    ],
)
async def test_planner_classifies_invalid_outputs_without_exposing_model_text(
    body: dict[str, object],
) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:8711/v1/chat/completions"),
        json=body,
    )
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:8711/v1", model="local")
    with (
        patch("httpx.AsyncClient.post", AsyncMock(return_value=response)),
        pytest.raises(SwarmPlannerProviderError) as raised,
    ):
        await provider.propose(_planner_context())
    assert raised.value.category == "invalid_response"
    assert "provider-response" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cards",
    [
        [],
        [{"kind": "goal", "card_id": "foreign-card"}],
        [
            {"kind": "goal", "card_id": "goal:goal_current"},
            {"kind": "goal", "card_id": "goal:goal_other"},
        ],
    ],
)
async def test_planner_rejects_ambiguous_goal_binding_before_http(cards: list[object]) -> None:
    post = AsyncMock()
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:8711/v1", model="local")
    with (
        patch("httpx.AsyncClient.post", post),
        pytest.raises(SwarmPlannerProviderError) as raised,
    ):
        await provider.propose({"cards": cards})
    assert raised.value.category == "invalid_context"
    post.assert_not_awaited()
