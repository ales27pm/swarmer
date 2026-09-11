from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from app.services.model_wire_schema import model_wire_schema
from app.services.orchestrator_service import OrchestratorService
from app.services.plan_validation import PlanValidationError, parse_swarm_plan_json
from app.services.swarm_contracts import PlannerSource, SwarmPlanProposal


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


class SwarmPlannerProviderError(RuntimeError):
    """The multi-agent planner transport or strict proposal contract failed."""


class SwarmPlannerProvider(Protocol):
    source: PlannerSource

    async def propose(self, context: Mapping[str, object]) -> SwarmPlanProposal: ...


class NoopSwarmPlannerProvider:
    source = PlannerSource.TEST

    async def propose(self, context: Mapping[str, object]) -> SwarmPlanProposal:
        del context
        raise SwarmPlannerProviderError("swarm planner is not configured")


class DeterministicSwarmPlannerProvider:
    """A fixed proposal provider for deterministic goal-manager tests."""

    source = PlannerSource.TEST

    def __init__(self, proposal: SwarmPlanProposal) -> None:
        self._proposal = proposal.model_copy(deep=True)

    async def propose(self, context: Mapping[str, object]) -> SwarmPlanProposal:
        del context
        return self._proposal.model_copy(deep=True)


class UbuntuSwarmPlannerProvider:
    """Strict OpenAI-compatible goal planner; it never receives an executor."""

    source = PlannerSource.UBUNTU_LOCAL
    SYSTEM_PROMPT = """You are the monGARS multi-agent goal planner.
Return exactly one JSON object matching the supplied schema and no prose.
Decompose only the bounded, redacted context supplied by the Ubuntu control plane.
Set the top-level objective to the exact card_id of the card whose kind is goal (goal:goal_<id>).
This binds your proposal to its goal; do not reconstruct or rewrite the redacted objective.
Keep all text concise: titles and criteria at most 500 characters, objectives and summaries
at most 4000 characters. The server enforces these limits independently of the generation schema.
Use only agent skills shown in that context. Prefer independent nodes when they can run safely
in parallel. Never invent a skill. If no agent cards are present, propose only a synthesis node
describing missing execution capabilities; do not pretend that the available runtime can create
or modify software. A synthesis node requires required_skill=null and preferred_agent_constraints=null.
Every node must have a unique temporary_id. Dependencies refer only to other nodes' temporary_id;
never depend on yourself. Independent nodes have dependencies=[] and optional_dependencies=[].
Minimal synthesis-node shape (replace the ID/text as needed):
{"temporary_id":"assess","node_type":"synthesis","title":"Assess capability gap","objective":"Identify missing execution capabilities","required_skill":null,"dependencies":[],"optional_dependencies":[],"expected_output":"A clear capability limitation","priority":1,"preferred_agent_constraints":null}
Never emit credentials, tool calls, shell commands, approval decisions, execution state, or claims
that work completed. The server validates the DAG, policy, budgets, and every later transition.
"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _response_format() -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "swarm_plan_proposal",
                "strict": True,
                "schema": model_wire_schema(SwarmPlanProposal),
            },
        }

    async def propose(self, context: Mapping[str, object]) -> SwarmPlanProposal:
        try:
            context_json = json.dumps(
                dict(context),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise SwarmPlannerProviderError("planner context is not canonical JSON") from exc
        if len(context_json.encode("utf-8")) > 262_144:
            raise SwarmPlannerProviderError("planner context exceeds the transport budget")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": context_json},
            ],
            "temperature": 0.0,
            "stream": False,
            "response_format": self._response_format(),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SwarmPlannerProviderError("local swarm planner unavailable") from exc
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise SwarmPlannerProviderError("invalid swarm planner response envelope") from exc
        if not isinstance(content, str):
            raise SwarmPlannerProviderError("swarm planner content is not text")
        try:
            return parse_swarm_plan_json(content.strip())
        except PlanValidationError as exc:
            raise SwarmPlannerProviderError("swarm planner returned an invalid proposal") from exc
