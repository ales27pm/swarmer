import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.orchestrator_service import OrchestratorError, OrchestratorService


def response_for(content: str) -> httpx.Response:
    request = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
    return httpx.Response(
        200,
        request=request,
        json={"choices": [{"message": {"content": content}}]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proposal",
    [
        {"tool_name": "workspace.list_dir", "summary": "List files"},
        {"tool_name": "workspace.list_dir", "arguments": {}},
        {
            "tool_name": "workspace.list_dir",
            "arguments": {},
            "summary": "List files",
            "claim": "already completed",
        },
    ],
)
async def test_plan_rejects_missing_or_extra_proposal_fields(proposal: dict[str, object]) -> None:
    service = OrchestratorService("http://127.0.0.1:11434/v1", "local-model")
    post = AsyncMock(return_value=response_for(json.dumps(proposal)))
    with (
        patch("httpx.AsyncClient.post", post),
        pytest.raises(OrchestratorError, match="exactly tool_name"),
    ):
        await service.plan("inspect")


@pytest.mark.asyncio
async def test_plan_accepts_exact_typed_proposal() -> None:
    proposal = {
        "tool_name": "workspace.list_dir",
        "arguments": {"path": "."},
        "summary": "List files",
    }
    service = OrchestratorService("http://127.0.0.1:11434/v1", "local-model")
    post = AsyncMock(return_value=response_for(json.dumps(proposal)))
    with patch("httpx.AsyncClient.post", post):
        assert await service.plan("inspect") == proposal

    await_args = post.await_args
    assert await_args is not None
    payload = await_args.kwargs["json"]
    assert payload["stream"] is False
    assert payload["response_format"] == OrchestratorService.RESPONSE_FORMAT
    assert payload["response_format"]["json_schema"]["strict"] is False
    schema = payload["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["tool_name", "arguments", "summary"]
    assert schema["properties"]["tool_name"]["enum"] == [
        "none",
        "workspace.list_dir",
        "workspace.read_text",
        "workspace.write_text",
        "process.run",
    ]
    arguments = schema["properties"]["arguments"]
    assert arguments["additionalProperties"] is False
    assert set(arguments["properties"]) == {
        "path",
        "content",
        "argv",
        "cwd",
        "timeout_seconds",
    }


@pytest.mark.asyncio
async def test_plan_discards_inert_arguments_for_none_proposal() -> None:
    proposal = {
        "tool_name": "none",
        "arguments": {"path": "."},
        "summary": "No tool needed",
    }
    service = OrchestratorService("http://127.0.0.1:11434/v1", "local-model")
    post = AsyncMock(return_value=response_for(json.dumps(proposal)))
    with patch("httpx.AsyncClient.post", post):
        result = await service.plan("explain")
    assert result == {**proposal, "arguments": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "summary", "message"),
    [
        ("unknown.tool", "Unknown tool", "unsupported tool"),
        ("none", "   ", "summary is invalid"),
        ("none", "x" * 2_001, "summary is invalid"),
    ],
)
async def test_plan_rejects_values_outside_the_response_contract(
    tool_name: str, summary: str, message: str
) -> None:
    proposal = {"tool_name": tool_name, "arguments": {}, "summary": summary}
    service = OrchestratorService("http://127.0.0.1:11434/v1", "local-model")
    post = AsyncMock(return_value=response_for(json.dumps(proposal)))
    with patch("httpx.AsyncClient.post", post), pytest.raises(OrchestratorError, match=message):
        await service.plan("explain")
