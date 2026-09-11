from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol

import httpx
from pydantic import ValidationError

from app.services.model_wire_schema import model_wire_schema
from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import (
    MAX_PROPOSAL_BYTES,
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

EvaluatorFailureCategory = Literal[
    "transport_unavailable", "request_rejected", "invalid_response", "invalid_context"
]
EvaluatorDiagnostic = Literal[
    "transport", "http_status", "envelope", "json", "schema", "graph", "context"
]


class EvaluatorProviderError(RuntimeError):
    """A safe failure classification; model values never enter diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        category: EvaluatorFailureCategory = "transport_unavailable",
        diagnostic: EvaluatorDiagnostic = "transport",
        output_digest: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.diagnostic = diagnostic
        self.output_digest = output_digest


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
Judge the original objective and requested deliverables/actions, not just generic completion
criteria or a completed synthesis node. Generic criteria cannot weaken the user's request.
A synthesis with no implementation evidence does not fulfill an application request.
Node titles and expected_output are planner intent, not worker evidence. Use node_type and
required_skill to distinguish synthesis from worker results; null means unknown in older records.
available_skills is the control plane's current fresh online-or-busy, protocol-compatible,
policy-allowed worker capability snapshot. null means unknown; [] means none observed. A busy worker can still
provide a skill. This snapshot proves availability, not execution, successful checks or approval.
Do not infer missing capabilities from a planner's title when available_skills lists them.
For a requested application, code.build_project can implement and check a private project;
if it is available and implementation is missing, propose that work rather than asking the user
how to build it. Applying files to the user's workspace still requires separate approval.
If the plan skipped requested implementation, propose continue or replan grounded in the
available evidence; missing implementation is work remaining, not a question about how to code.
Write all user-facing summaries, requirements and questions in the user's language, taken
from the original objective even when criteria, node metadata or diagnostics are English.
Language examples: objective "Crée une application" -> user_question "Quelles fonctionnalités souhaitez-vous ?"
Objective "Create an application" -> user_question "Which features do you need?"
Apply that objective's language to reason_summary, missing_requirements and completion_summary too.
Use needs_user only when missing material product requirements, unavailable user data, or
required user authorization prevents further progress. Ask one concrete question about that
missing input. Do not ask the user how to set up a development environment, choose a framework or libraries,
plan implementation, build, or test; those are routine agent decisions.
Keep all text concise: titles, criteria, requirements and questions at most 500 characters;
objectives and summaries at most 4000 characters. The server independently enforces these limits.
You may propose continue, replan, done, failed, or needs_user.
Always include user_question and completion_summary. For needs_user, user_question must be
a nonempty concrete question; for every other status it must be null. For done, provide a
completion_summary grounded in the recorded results; otherwise use null when unavailable.
Use [] for missing_requirements, invalid_results and suggested_new_nodes when empty.
Only continue or replan may suggest new nodes; done, failed and needs_user require [].
An approved Python file-write receipt proves only that the proposed source was saved. It
does not prove execution, tests, installation or deployment. State those limitations clearly.
If any of those actions was explicitly required, do not mark done without its own evidence.
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
                "schema": model_wire_schema(EvaluationDecision),
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
        except httpx.HTTPStatusError as exc:
            rejected = 400 <= exc.response.status_code < 500 and exc.response.status_code not in {
                408,
                429,
            }
            raise EvaluatorProviderError(
                "local evaluator request failed",
                category="request_rejected" if rejected else "transport_unavailable",
                diagnostic="http_status",
            ) from exc
        except httpx.HTTPError as exc:
            raise EvaluatorProviderError("local evaluator unavailable") from exc

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EvaluatorProviderError(
                "invalid evaluator response envelope",
                category="invalid_response",
                diagnostic="envelope",
            ) from exc
        if not isinstance(content, str):
            raise EvaluatorProviderError(
                "evaluator content is not text", category="invalid_response", diagnostic="envelope"
            )
        output_digest = None
        try:
            encoded = content.encode("utf-8")
            if len(encoded) <= MAX_PROPOSAL_BYTES:
                output_digest = hashlib.sha256(encoded).hexdigest()
        except UnicodeError:
            pass
        try:
            decision = parse_evaluation_json(content.strip())
        except PlanValidationError as exc:
            raise EvaluatorProviderError(
                "evaluator returned an invalid proposal",
                category="invalid_response",
                diagnostic="schema" if isinstance(exc.__cause__, ValidationError) else "json",
                output_digest=output_digest,
            ) from exc
        try:
            validated = validate_evaluation_decision(
                decision,
                policy=self.policy,
                known_node_ids=context.known_node_ids,
            )
        except PlanValidationError as exc:
            raise EvaluatorProviderError(
                "evaluator returned an invalid proposal",
                category="invalid_response",
                diagnostic="graph",
                output_digest=output_digest,
            ) from exc
        return validated.decision
