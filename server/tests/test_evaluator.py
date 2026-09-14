from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.services.evaluator_provider import (
    DeterministicEvaluatorProvider,
    EvaluatorProviderError,
    NoopEvaluatorProvider,
    UbuntuEvaluatorProvider,
)
from app.services.model_router import ModelRouter, ModelRouterError
from app.services.permission_policy import PermissionPolicy
from app.services.plan_validation import (
    PlanValidationError,
    validate_evaluation_decision,
)
from app.services.swarm_contracts import (
    EvaluationConversationMessage,
    EvaluationDecision,
    EvaluationStatus,
    GoalEvaluationContext,
    ModelRole,
    ModelRoleConfig,
    PlannerSource,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def policy() -> PermissionPolicy:
    return PermissionPolicy.from_yaml(REPO_ROOT / "configs" / "permissions.yaml")


def evaluation_context() -> GoalEvaluationContext:
    return GoalEvaluationContext.model_validate(
        {
            "schema_version": "1.0",
            "goal_run_id": "goal_123",
            "objective": "Inspect the repository.",
            "completion_criteria": ["The repository was inspected."],
            "node_results": [
                {
                    "node_id": "node_existing",
                    "title": "Inventory",
                    "status": "completed",
                    "expected_output": "A root listing.",
                    "result_summary": "README.md and server were observed.",
                    "failure_reason": None,
                }
            ],
            "known_node_ids": ["node_existing"],
            "remaining_step_budget": 19,
            "remaining_model_call_budget": 28,
            "elapsed_seconds": 4,
            "state_fingerprint": "0" * 64,
        }
    )


def continue_decision() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "status": "continue",
        "reason_summary": "One more read-only observation is required.",
        "missing_requirements": ["Inspect the implementation."],
        "invalid_results": [],
        "suggested_new_nodes": [
            {
                "temporary_id": "read_code",
                "node_type": "worker",
                "title": "Read implementation",
                "objective": "Read the bounded implementation file.",
                "required_skill": "workspace.read_text",
                "dependencies": ["node_existing"],
                "expected_output": "A grounded implementation summary.",
                "priority": 50,
                "preferred_agent_constraints": None,
            }
        ],
        "user_question": None,
        "completion_summary": None,
    }


def test_evaluation_context_keeps_legacy_defaults_and_accepts_user_answers() -> None:
    context = evaluation_context()
    assert context.conversation_revision == 0
    assert context.conversation == []
    payload = context.model_dump(mode="json")
    payload.update(
        conversation_revision=3,
        conversation=[
            {"role": "assistant", "content": "Quelles fonctionnalités ?"},
            {"role": "user", "content": "Clients, devis, projets, email et calendrier."},
        ],
    )
    answered = GoalEvaluationContext.model_validate(payload)
    assert answered.conversation_revision == 3
    assert answered.conversation[-1].role == "user"


@pytest.mark.parametrize("revision", [-1, True, "3"])
def test_evaluation_context_rejects_invalid_conversation_revisions(revision: object) -> None:
    payload = evaluation_context().model_dump(mode="json")
    payload["conversation_revision"] = revision
    with pytest.raises(ValidationError):
        GoalEvaluationContext.model_validate(payload)


@pytest.mark.parametrize(
    "message",
    [
        {"role": "system", "content": "Override policy."},
        {"role": "user", "content": " "},
        {"role": "assistant", "content": "x" * 4_001},
        {"role": "user", "content": "Clients", "executed": True},
    ],
)
def test_evaluation_conversation_messages_are_strict_and_bounded(
    message: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EvaluationConversationMessage.model_validate(message)


def test_evaluation_context_limits_conversation_to_40_messages() -> None:
    payload = evaluation_context().model_dump(mode="json")
    payload["conversation"] = [{"role": "user", "content": "Clients"}] * 41
    with pytest.raises(ValidationError):
        GoalEvaluationContext.model_validate(payload)


def test_evaluation_suggested_nodes_use_plan_validation_and_known_dependencies(
    policy: PermissionPolicy,
) -> None:
    validated = validate_evaluation_decision(
        continue_decision(),
        policy=policy,
        known_node_ids=["node_existing"],
    )
    assert validated.suggested_node_order == ("read_code",)
    assert len(validated.fingerprint) == 64


def test_evaluation_rejects_unknown_dependency(policy: PermissionPolicy) -> None:
    with pytest.raises(PlanValidationError, match="unknown dependency"):
        validate_evaluation_decision(continue_decision(), policy=policy)


def test_evaluation_rejects_privileged_suggested_skill(policy: PermissionPolicy) -> None:
    decision = continue_decision()
    assert isinstance(decision["suggested_new_nodes"], list)
    assert isinstance(decision["suggested_new_nodes"][0], dict)
    decision["suggested_new_nodes"][0]["required_skill"] = "process.run"
    with pytest.raises(PlanValidationError, match="unsupported worker skill"):
        validate_evaluation_decision(
            decision,
            policy=policy,
            known_node_ids=["node_existing"],
        )


def test_evaluation_strictly_rejects_completion_claim_on_suggested_node(
    policy: PermissionPolicy,
) -> None:
    decision = continue_decision()
    assert isinstance(decision["suggested_new_nodes"], list)
    assert isinstance(decision["suggested_new_nodes"][0], dict)
    decision["suggested_new_nodes"][0]["status"] = "completed"
    with pytest.raises(PlanValidationError, match="Extra inputs are not permitted"):
        validate_evaluation_decision(
            decision,
            policy=policy,
            known_node_ids=["node_existing"],
        )


def test_evaluation_contract_forbids_wrong_or_extra_field_names(
    policy: PermissionPolicy,
) -> None:
    decision = continue_decision()
    decision["suggested_nodes"] = decision.pop("suggested_new_nodes")
    with pytest.raises(PlanValidationError, match="suggested_new_nodes"):
        validate_evaluation_decision(decision, policy=policy)


def test_terminal_evaluation_cannot_add_nodes(policy: PermissionPolicy) -> None:
    decision = continue_decision()
    decision.update(
        {
            "status": "done",
            "completion_summary": "All verified requirements are satisfied.",
        }
    )
    with pytest.raises(PlanValidationError, match="cannot suggest new nodes"):
        validate_evaluation_decision(
            decision,
            policy=policy,
            known_node_ids=["node_existing"],
        )


def test_done_evaluation_keeps_completion_summary_optional(policy: PermissionPolicy) -> None:
    decision = {
        **continue_decision(),
        "status": "done",
        "suggested_new_nodes": [],
        "completion_summary": None,
    }
    validated = validate_evaluation_decision(decision, policy=policy)
    assert validated.decision.status is EvaluationStatus.DONE


def test_needs_user_requires_question_and_other_states_reject_it(
    policy: PermissionPolicy,
) -> None:
    needs_user = {
        **continue_decision(),
        "status": "needs_user",
        "suggested_new_nodes": [],
        "user_question": None,
    }
    with pytest.raises(PlanValidationError, match="require user_question"):
        validate_evaluation_decision(needs_user, policy=policy)

    unexpected = {
        **continue_decision(),
        "suggested_new_nodes": [],
        "user_question": "Should I continue?",
    }
    with pytest.raises(PlanValidationError, match="only valid for needs_user"):
        validate_evaluation_decision(unexpected, policy=policy)


def test_evaluation_fingerprint_detects_repeated_control_decision(
    policy: PermissionPolicy,
) -> None:
    first = validate_evaluation_decision(
        continue_decision(), policy=policy, known_node_ids=["node_existing"]
    )
    repeated = deepcopy(continue_decision())
    repeated["reason_summary"] = "Different free-form reasoning, same control decision."
    repeated["completion_summary"] = "Different non-authoritative explanatory prose."
    second = validate_evaluation_decision(repeated, policy=policy, known_node_ids=["node_existing"])
    assert second.fingerprint == first.fingerprint

    changed = deepcopy(continue_decision())
    changed["missing_requirements"] = ["A different requirement is missing."]
    third = validate_evaluation_decision(changed, policy=policy, known_node_ids=["node_existing"])
    assert third.fingerprint != first.fingerprint


@pytest.mark.asyncio
async def test_noop_and_deterministic_evaluators_are_proposal_only() -> None:
    context = evaluation_context()
    noop = await NoopEvaluatorProvider().evaluate(context)
    assert noop.status is EvaluationStatus.CONTINUE
    assert noop.suggested_new_nodes == []

    expected = EvaluationDecision.model_validate(
        {
            "schema_version": "1.0",
            "status": "done",
            "reason_summary": "The evidence is complete.",
            "missing_requirements": [],
            "invalid_results": [],
            "suggested_new_nodes": [],
            "completion_summary": "Verified work is complete.",
        }
    )
    provider = DeterministicEvaluatorProvider(expected)
    first = await provider.evaluate(context)
    second = await provider.evaluate(context)
    assert first == expected == second
    assert first is not second


def _response_for(content: str) -> httpx.Response:
    request = httpx.Request("POST", "http://127.0.0.1:8711/v1/chat/completions")
    return httpx.Response(
        200,
        request=request,
        json={"choices": [{"message": {"content": content}}]},
    )


@pytest.mark.asyncio
async def test_ubuntu_evaluator_returns_only_a_validated_proposal(
    policy: PermissionPolicy,
) -> None:
    raw = continue_decision()
    post = AsyncMock(return_value=_response_for(json.dumps(raw)))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:8711/v1",
        model="local-evaluator",
        policy=policy,
    )
    with patch("httpx.AsyncClient.post", post):
        decision = await provider.evaluate(evaluation_context())

    assert decision.status is EvaluationStatus.CONTINUE
    call = post.await_args
    assert call is not None
    payload = call.kwargs["json"]
    assert payload["stream"] is False
    assert payload["temperature"] == 0.0
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert "execution" not in payload
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert "remains authoritative" in payload["messages"][0]["content"]


@pytest.mark.parametrize("skills", [[], ["code.build_project"], ["workspace.read_text"], None])
def test_evaluator_schema_limits_workers_but_preserves_unknown_availability(
    skills: list[str] | None,
) -> None:
    schema = UbuntuEvaluatorProvider._response_format(skills)["json_schema"]["schema"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for required_skill in ["code.build_project", "code.generate_python", "workspace.read_text"]:
        raw = continue_decision()
        assert isinstance(raw["suggested_new_nodes"], list)
        raw["suggested_new_nodes"][0].update(required_skill=required_skill, dependencies=[])
        assert validator.is_valid(raw) is (skills is None or required_skill in skills)
    raw = continue_decision()
    assert isinstance(raw["suggested_new_nodes"], list)
    raw["suggested_new_nodes"][0].update(node_type="synthesis", required_skill=None)
    assert validator.is_valid(raw)
    assert validator.is_valid({**raw, "suggested_new_nodes": []})


def test_evaluator_schema_preserves_project_exclusivity() -> None:
    schema = UbuntuEvaluatorProvider._response_format(
        ["code.build_project", "workspace.read_text"]
    )["json_schema"]["schema"]
    raw = continue_decision()
    assert isinstance(raw["suggested_new_nodes"], list)
    raw["suggested_new_nodes"][0].update(required_skill="code.build_project", dependencies=[])
    assert Draft202012Validator(schema).is_valid(raw)
    raw["suggested_new_nodes"].append(continue_decision()["suggested_new_nodes"][0])
    assert not Draft202012Validator(schema).is_valid(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("skills", [[], ["code.build_project"], ["workspace.read_text"], None])
async def test_evaluator_rejects_unadvertised_skill_without_retry_or_rewrite(
    policy: PermissionPolicy,
    skills: list[str] | None,
) -> None:
    context = evaluation_context()
    context.available_skills = skills
    post = AsyncMock(return_value=_response_for(json.dumps(continue_decision())))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:8711/v1", model="local-evaluator", policy=policy
    )
    with patch("httpx.AsyncClient.post", post):
        if skills is None or "workspace.read_text" in skills:
            decision = await provider.evaluate(context)
            assert decision.suggested_new_nodes[0].required_skill == "workspace.read_text"
        else:
            with pytest.raises(EvaluatorProviderError) as raised:
                await provider.evaluate(context)
            assert raised.value.category == "invalid_response"
            assert raised.value.diagnostic == "graph"
    assert post.await_count == 1
    assert post.await_args is not None
    payload = post.await_args.kwargs["json"]
    assert json.loads(payload["messages"][1]["content"])["available_skills"] == skills
    schema = payload["response_format"]["json_schema"]["schema"]
    assert Draft202012Validator(schema).is_valid(continue_decision()) is (
        skills is None or "workspace.read_text" in skills
    )


@pytest.mark.asyncio
async def test_evaluator_validates_against_the_skills_sent_before_http(
    policy: PermissionPolicy,
) -> None:
    context = evaluation_context()
    context.available_skills = ["code.build_project"]

    async def changed_context(*args: object, **kwargs: object) -> httpx.Response:
        assert context.available_skills is not None
        context.available_skills.append("workspace.read_text")
        return _response_for(json.dumps(continue_decision()))

    post = AsyncMock(side_effect=changed_context)
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:8711/v1", model="local-evaluator", policy=policy
    )
    with (
        patch("httpx.AsyncClient.post", post),
        pytest.raises(EvaluatorProviderError) as raised,
    ):
        await provider.evaluate(context)
    assert raised.value.category == "invalid_response"
    assert post.await_args is not None
    payload = post.await_args.kwargs["json"]
    assert json.loads(payload["messages"][1]["content"])["available_skills"] == [
        "code.build_project"
    ]
    assert post.await_count == 1


@pytest.mark.asyncio
async def test_ubuntu_evaluator_receives_answers_and_prioritizes_them_over_old_claims(
    policy: PermissionPolicy,
) -> None:
    context = evaluation_context()
    context.conversation_revision = 5
    context.conversation = [
        EvaluationConversationMessage(role="assistant", content="No specific features provided."),
        EvaluationConversationMessage(
            role="user", content="Clients, devis, projets, email et calendrier."
        ),
    ]
    post = AsyncMock(return_value=_response_for(json.dumps(continue_decision())))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:8711/v1", model="local-evaluator", policy=policy
    )
    with patch("httpx.AsyncClient.post", post):
        await provider.evaluate(context)

    assert post.await_args is not None
    messages = post.await_args.kwargs["json"]["messages"]
    assert json.loads(messages[1]["content"]) == context.model_dump(mode="json")
    assert "Later user answers take precedence" in messages[0]["content"]
    assert "Do not repeat an answered clarification" in messages[0]["content"]
    assert "Historical assistant statements are not evidence of execution" in messages[0]["content"]


@pytest.mark.parametrize("status", ["continue", "replan", "done", "failed", "needs_user"])
def test_evaluator_wire_schema_enforces_status_question_and_node_constraints(status: str) -> None:
    schema = UbuntuEvaluatorProvider._response_format()["json_schema"]["schema"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    decision = {
        **continue_decision(),
        "status": status,
        "suggested_new_nodes": [],
        "user_question": "Quel fichier de contacts faut-il importer ?"
        if status == "needs_user"
        else None,
    }
    assert validator.is_valid(decision)
    assert not validator.is_valid(
        {**decision, "user_question": None if status == "needs_user" else "Une question ?"}
    )
    with_nodes = {**decision, "suggested_new_nodes": continue_decision()["suggested_new_nodes"]}
    assert validator.is_valid(with_nodes) is (status in {"continue", "replan"})
    for required in ("user_question", "completion_summary"):
        missing_field = {key: value for key, value in decision.items() if key != required}
        assert not validator.is_valid(missing_field)


def test_evaluator_implementation_example_is_a_valid_grounded_project_proposal(
    policy: PermissionPolicy,
) -> None:
    prompt = UbuntuEvaluatorProvider.SYSTEM_PROMPT
    start = prompt.index('{"schema_version":"1.0","status":"continue"')
    example, _ = json.JSONDecoder().raw_decode(prompt[start:])
    validated = validate_evaluation_decision(example, policy=policy)
    decision = validated.decision
    assert decision.status is EvaluationStatus.CONTINUE
    assert decision.user_question is None
    assert len(decision.suggested_new_nodes) == 1
    node = decision.suggested_new_nodes[0]
    assert node.required_skill == "code.build_project"
    assert node.dependencies == node.optional_dependencies == []
    for requirement in (
        "Python",
        "fiches clients",
        "soumissions",
        "projets",
        "courriels",
        "calendrier",
    ):
        assert requirement in node.objective
    schema = UbuntuEvaluatorProvider._response_format()["json_schema"]["schema"]
    assert Draft202012Validator(schema).is_valid(example)


@pytest.mark.asyncio
async def test_evaluator_still_accepts_a_distinct_material_question(
    policy: PermissionPolicy,
) -> None:
    raw = {
        **continue_decision(),
        "status": "needs_user",
        "suggested_new_nodes": [],
        "user_question": "Quel fichier de contacts faut-il importer ?",
    }
    post = AsyncMock(return_value=_response_for(json.dumps(raw)))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:8711/v1", model="local-evaluator", policy=policy
    )
    with patch("httpx.AsyncClient.post", post):
        decision = await provider.evaluate(evaluation_context())
    assert decision.status is EvaluationStatus.NEEDS_USER
    assert decision.user_question == raw["user_question"]


@pytest.mark.asyncio
async def test_ubuntu_evaluator_fails_closed_on_invalid_output(
    policy: PermissionPolicy,
) -> None:
    raw = continue_decision()
    raw["approval"] = "allow"
    post = AsyncMock(return_value=_response_for(json.dumps(raw)))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:8711/v1",
        model="local-evaluator",
        policy=policy,
    )
    with (
        patch("httpx.AsyncClient.post", post),
        pytest.raises(EvaluatorProviderError, match="invalid proposal"),
    ):
        await provider.evaluate(evaluation_context())


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_length", [4000, 4001])
async def test_evaluator_wire_schema_preserves_local_string_limits(
    policy: PermissionPolicy, summary_length: int
) -> None:
    raw = continue_decision()
    raw["reason_summary"] = "x" * summary_length
    post = AsyncMock(return_value=_response_for(json.dumps(raw)))
    provider = UbuntuEvaluatorProvider(
        base_url="http://127.0.0.1:8711/v1", model="local-evaluator", policy=policy
    )
    with patch("httpx.AsyncClient.post", post):
        if summary_length == 4000:
            decision = await provider.evaluate(evaluation_context())
            assert decision.reason_summary == raw["reason_summary"]
        else:
            with pytest.raises(EvaluatorProviderError, match="invalid proposal") as raised:
                await provider.evaluate(evaluation_context())
            assert isinstance(raised.value.__cause__, PlanValidationError)
            assert "at most 4000 characters" in str(raised.value.__cause__)

    assert post.await_count == 1
    assert post.await_args is not None
    wire = post.await_args.kwargs["json"]["response_format"]
    assert wire["type"] == "json_schema"
    assert wire["json_schema"]["strict"] is True
    assert '"maxLength"' not in json.dumps(wire["json_schema"]["schema"])


def test_model_router_is_deterministic_metadata_without_execution_authority() -> None:
    router = ModelRouter(
        [
            ModelRoleConfig(
                role=ModelRole.PLANNER,
                source=PlannerSource.UBUNTU_LOCAL,
                model_id="planner-v1",
            ),
            ModelRoleConfig(
                role=ModelRole.EVALUATOR,
                source=PlannerSource.TEST,
                model_id="evaluator-fixture",
            ),
        ]
    )
    assert router.route_for(ModelRole.PLANNER).model_id == "planner-v1"
    assert router.public_metadata() == (
        {"role": "evaluator", "source": "test", "model_id": "evaluator-fixture"},
        {"role": "planner", "source": "ubuntu_local", "model_id": "planner-v1"},
    )
    assert not hasattr(router, "execute")
    assert not hasattr(router, "invoke")


def test_model_router_rejects_duplicate_or_missing_role() -> None:
    route = ModelRoleConfig(
        role=ModelRole.PLANNER,
        source=PlannerSource.MANUAL,
        model_id="planner-v1",
    )
    with pytest.raises(ModelRouterError, match="duplicate"):
        ModelRouter([route, route])
    with pytest.raises(ModelRouterError, match="no model route"):
        ModelRouter([route]).route_for(ModelRole.EVALUATOR)
