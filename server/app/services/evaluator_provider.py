from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import (
    PlanValidationError,
    parse_evaluation_json,
    validate_evaluation_decision,
)
from app.services.swarm_contracts import (
    EvaluationDecision,
    EvaluationStatus,
    GoalEvaluationContext,
    PlannerSource,
)


class EvaluatorProviderError(RuntimeError):
    """The evaluator transport or proposal contract failed."""


class EvaluatorProvider(Protocol):
    source: PlannerSource

    async def evaluate(self, context: GoalEvaluationContext) -> EvaluationDecision: ...


class NoopEvaluatorProvider:
    """An inert evaluator for tests and deployments without an evaluator model."""

    source = PlannerSource.TEST

    async def evaluate(self, context: GoalEvaluationContext) -> EvaluationDecision:
        del context
        return EvaluationDecision(
            schema_version="1.0",
            status=EvaluationStatus.CONTINUE,
            reason_summary="No evaluator decision was generated.",
            missing_requirements=[],
            invalid_results=[],
            suggested_new_nodes=[],
        )


class DeterministicEvaluatorProvider:
    """A fixed proposal provider for deterministic state-machine tests."""

    source = PlannerSource.TEST

    def __init__(self, decision: EvaluationDecision) -> None:
        self._decision = decision.model_copy(deep=True)

    async def evaluate(self, context: GoalEvaluationContext) -> EvaluationDecision:
        del context
        return self._decision.model_copy(deep=True)


class UbuntuEvaluatorProvider:
    """OpenAI-compatible local evaluator which can only return a proposal."""

    source = PlannerSource.UBUNTU_LOCAL
    SYSTEM_PROMPT = """You are the monGARS goal evaluator.
Return exactly one JSON object matching the supplied schema and no prose.
Evaluate only the bounded goal state in the user message.
You may propose continue, replan, done, failed, or needs_user.
Never claim that a tool, worker, permission, native capability, or task already executed.
Never emit credentials, executable commands, tool calls, approval decisions, or side effects.
The Ubuntu control plane independently validates your proposal and remains authoritative.
"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        policy: PermissionPolicy,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.policy = policy
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _response_format() -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "goal_evaluation_decision",
                "strict": True,
                "schema": EvaluationDecision.model_json_schema(),
            },
        }

    async def evaluate(self, context: GoalEvaluationContext) -> EvaluationDecision:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        context.model_dump(mode="json"),
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
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
            raise EvaluatorProviderError("local evaluator unavailable") from exc

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EvaluatorProviderError("invalid evaluator response envelope") from exc
        if not isinstance(content, str):
            raise EvaluatorProviderError("evaluator content is not text")
        try:
            decision = parse_evaluation_json(content.strip())
            validated = validate_evaluation_decision(
                decision,
                policy=self.policy,
                known_node_ids=context.known_node_ids,
            )
        except PlanValidationError as exc:
            raise EvaluatorProviderError("evaluator returned an invalid proposal") from exc
        return validated.decision
