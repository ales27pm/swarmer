from unittest.mock import AsyncMock

import pytest

from app.services.planner_provider import NoopPlannerProvider, UbuntuLLMPlannerProvider


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
