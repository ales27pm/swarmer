from __future__ import annotations

from typing import Any, Protocol

from app.services.orchestrator_service import OrchestratorService


class PlannerProvider(Protocol):
    source: str

    async def plan(self, task_input: str, mode: str = "normal") -> dict[str, Any]: ...


class UbuntuLLMPlannerProvider:
    source = "ubuntu_local"

    def __init__(self, orchestrator: OrchestratorService) -> None:
        self.orchestrator = orchestrator

    async def plan(self, task_input: str, mode: str = "normal") -> dict[str, Any]:
        return await self.orchestrator.plan(task_input, mode)


class NoopPlannerProvider:
    source = "test"

    async def plan(self, task_input: str, mode: str = "normal") -> dict[str, Any]:
        del task_input, mode
        return {"tool_name": "none", "arguments": {}, "summary": "No plan generated."}
