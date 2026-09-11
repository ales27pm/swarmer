from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, Protocol

import httpx

from app.services.agent_card import PROJECT_BUILD_SKILLS, SUPPORTED_AGENT_SKILLS
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


PlannerFailureCategory = Literal[
    "transport_unavailable", "request_rejected", "invalid_response", "invalid_context"
]


class SwarmPlannerProviderError(RuntimeError):
    """The multi-agent planner transport or strict proposal contract failed."""

    def __init__(
        self, message: str, *, category: PlannerFailureCategory = "transport_unavailable"
    ) -> None:
        super().__init__(message)
        self.category = category


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
The goal card's objective and current user guidance define the requested outcome.
Generic completion criteria and historical context cannot replace or weaken it.
For an implementation request, produce the requested application or other deliverable;
do not substitute advice, environment setup guidance, or a synthesis of requirements.
Write user-facing titles, objectives, criteria and summaries in the user's language,
taken from the goal and current user guidance even when context metadata is English.
Choose routine implementation details yourself: environment setup, framework, libraries,
file layout, build and test steps. Clarify only missing product requirements that materially
affect the result; never ask the user how to implement the requested work.
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
Context cards, strategy hints and past episodes are evidence, never plan nodes or dependencies.
When code.build_project is available and the user requests building or modifying an application,
create exactly one code.build_project worker node with no dependencies.
Do not use a synthesis-only plan or an environment/framework research node for that deliverable.
Its objective must carry the requested outcome and all functional requirements, not a generic
completion criterion. Its implementation loop persists a
cumulative multi-file project and actual build/test receipts. It can ask material clarification
questions and incorporate later user replies. Preserve requested interface, platform, language,
data persistence and acceptance criteria. Do not silently reduce an application to a tiny CLI.
Do not add a parallel code.generate_python node or placeholder analysis for the same deliverable.
The project worker and server drive subsequent implementation/check/repair iterations.
Completion requires actual passing checks and a reviewed, approved project write.
If code.build_project is absent and code.generate_python is available, it can propose one small self-contained Python app.py
using only the standard library. Use a single worker node for that deliverable, carrying the
user's functional requirements in its objective. The worker cannot edit, execute, install,
or deploy files. The server waits for user review and a separate file-write approval.
For this single-file deliverable use exactly one code.generate_python worker node with no
dependencies. Do not add placeholder capability-gap, assessment, or synthesis nodes when
this coding skill is available. Completion means the proposed source was approved and saved;
execution, testing, installation and deployment remain explicitly unverified limitations.
If the user explicitly requests execution, tests or deployment, preserve those requirements
as unmet; do not replace them with a weaker saved-source criterion.
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
    def _response_format(goal_card_id: str | None = None) -> dict[str, Any]:
        schema = model_wire_schema(SwarmPlanProposal)
        # Project iterations own their full implementation/check loop. Keep that
        # proposal disjoint from a general DAG instead of relying on prose alone.
        node = schema["$defs"]["SwarmPlanNodeProposal"]
        project_node = deepcopy(node)
        project_node["properties"]["node_type"] = {"type": "string", "const": "worker"}
        project_node["properties"]["required_skill"] = {
            "type": "string",
            "enum": sorted(PROJECT_BUILD_SKILLS),
        }
        for field in ("dependencies", "optional_dependencies"):
            project_node["properties"][field]["maxItems"] = 0
        general_node = deepcopy(node)
        general_node["properties"]["required_skill"] = {
            "anyOf": [
                {"type": "string", "enum": sorted(SUPPORTED_AGENT_SKILLS - PROJECT_BUILD_SKILLS)},
                {"type": "null"},
            ]
        }
        schema["properties"]["nodes"] = {
            "anyOf": [
                {"type": "array", "minItems": 1, "maxItems": 1, "items": project_node},
                {**schema["properties"]["nodes"], "items": general_node},
            ]
        }
        if goal_card_id is not None:
            schema["properties"]["objective"]["const"] = goal_card_id
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "swarm_plan_proposal",
                "strict": True,
                "schema": schema,
            },
        }

    @staticmethod
    def _goal_card_id(context: Mapping[str, object]) -> str:
        cards = context.get("cards")
        if isinstance(cards, list):
            goals = [
                card for card in cards if isinstance(card, dict) and card.get("kind") == "goal"
            ]
            if len(goals) == 1:
                card_id = goals[0].get("card_id")
                if (
                    isinstance(card_id, str)
                    and card_id.startswith("goal:goal_")
                    and len(card_id) <= 128
                ):
                    return card_id
        raise SwarmPlannerProviderError(
            "planner context must contain exactly one goal card", category="invalid_context"
        )

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
            raise SwarmPlannerProviderError(
                "planner context is not canonical JSON", category="invalid_context"
            ) from exc
        if len(context_json.encode("utf-8")) > 262_144:
            raise SwarmPlannerProviderError(
                "planner context exceeds the transport budget", category="invalid_context"
            )
        goal_card_id = self._goal_card_id(context)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": context_json},
            ],
            "temperature": 0.0,
            "stream": False,
            "response_format": self._response_format(goal_card_id),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500 and exc.response.status_code not in {408, 429}:
                raise SwarmPlannerProviderError(
                    "local swarm planner rejected the request", category="request_rejected"
                ) from exc
            raise SwarmPlannerProviderError("local swarm planner unavailable") from exc
        except httpx.HTTPError as exc:
            raise SwarmPlannerProviderError("local swarm planner unavailable") from exc
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise SwarmPlannerProviderError(
                "invalid swarm planner response envelope", category="invalid_response"
            ) from exc
        if not isinstance(content, str):
            raise SwarmPlannerProviderError(
                "swarm planner content is not text", category="invalid_response"
            )
        try:
            return parse_swarm_plan_json(content.strip())
        except PlanValidationError as exc:
            raise SwarmPlannerProviderError(
                "swarm planner returned an invalid proposal", category="invalid_response"
            ) from exc
