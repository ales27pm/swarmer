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
