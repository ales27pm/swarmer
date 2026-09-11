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
@pytest.mark.parametrize("objective_length", [4000, 4001])
async def test_swarm_planner_wire_schema_preserves_local_string_limits(
    objective_length: int,
) -> None:
    proposal = {
        "schema_version": "1.0",
        "objective": "x" * objective_length,
        "rationale_summary": "Inspect bounded evidence.",
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
        if objective_length == 4000:
            result = await provider.propose({"cards": []})
            assert result.objective == proposal["objective"]
        else:
            with pytest.raises(SwarmPlannerProviderError, match="invalid proposal") as raised:
                await provider.propose({"cards": []})
            assert isinstance(raised.value.__cause__, PlanValidationError)
            assert "at most 4000 characters" in str(raised.value.__cause__)

    assert post.await_count == 1
    assert post.await_args is not None
    wire = post.await_args.kwargs["json"]["response_format"]
    assert wire["type"] == "json_schema"
    assert wire["json_schema"]["strict"] is True
    assert '"maxLength"' not in json.dumps(wire["json_schema"]["schema"])
