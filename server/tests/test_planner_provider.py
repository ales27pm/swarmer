import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from jsonschema import Draft202012Validator

from app.services.plan_validation import PlanValidationError
from app.services.planner_provider import (
    NoopPlannerProvider,
    SwarmPlannerProviderError,
    UbuntuLLMPlannerProvider,
    UbuntuSwarmPlannerProvider,
    advertised_worker_skills,
)


def _planner_context() -> dict[str, object]:
    return {
        "cards": [
            {
                "kind": "goal",
                "card_id": "goal:goal_current",
                "summary": "Create a bounded plan.",
            },
            {"kind": "episode", "card_id": "episode:older", "summary": "Prior failure."},
            {
                "kind": "agent_card",
                "card_id": "agent:workspace",
                "summary": "Workspace reader.",
                "skills": ["workspace.list_dir"],
            },
        ]
    }


def _proposal(skill: str | None = "code.build_project") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "objective": "goal:goal_current",
        "rationale_summary": "Produce the requested notes application.",
        "completion_criteria": ["The requested deliverable is verified."],
        "max_parallelism": 1,
        "nodes": [
            {
                "temporary_id": "deliverable",
                "node_type": "worker" if skill else "synthesis",
                "title": "Prepare the deliverable",
                "objective": "Create a notes application with local persistence.",
                "required_skill": skill,
                "dependencies": [],
                "optional_dependencies": [],
                "expected_output": "A bounded deliverable and its verification evidence.",
                "priority": 50,
                "preferred_agent_constraints": None,
            }
        ],
    }


def test_advertised_worker_skills_uses_only_structured_agent_cards() -> None:
    assert advertised_worker_skills({}) == []
    assert advertised_worker_skills({"cards": []}) == []
    assert advertised_worker_skills(
        {
            "cards": [
                {"kind": "goal", "skills": ["code.generate_python"]},
                {"kind": "project_memory_hint", "skills": ["code.generate_python"]},
                {"kind": "episode", "summary": "agent_card skills: code.generate_python"},
                {"kind": "agent_card", "skills": ["workspace.read_text", "workspace.list_dir"]},
                {"kind": "agent_card", "skills": ["workspace.read_text", "code.build_project"]},
                {"kind": "agent_card", "skills": []},
            ]
        }
    ) == ["code.build_project", "workspace.list_dir", "workspace.read_text"]


@pytest.mark.parametrize(
    "skills",
    [
        None,
        "code.build_project",
        ("code.build_project",),
        {},
        [1],
        [True],
        [{}],
        ["process.run"],
        ["unknown.skill"],
        ["code.build_project", "code.build_project"],
    ],
)
def test_advertised_worker_skills_rejects_malformed_agent_fields(skills: object) -> None:
    with pytest.raises(SwarmPlannerProviderError) as raised:
        advertised_worker_skills({"cards": [{"kind": "agent_card", "skills": skills}]})
    assert raised.value.category == "invalid_context"


@pytest.mark.parametrize("cards", [None, {}, "agent_card"])
def test_advertised_worker_skills_rejects_a_malformed_card_collection(cards: object) -> None:
    with pytest.raises(SwarmPlannerProviderError) as raised:
        advertised_worker_skills({"cards": cards})
    assert raised.value.category == "invalid_context"


@pytest.mark.parametrize("skills", [[], ["code.build_project"], ["workspace.list_dir"]])
def test_planner_schema_limits_workers_to_presented_skills(skills: list[str]) -> None:
    schema = UbuntuSwarmPlannerProvider._response_format(
        "goal:goal_current", available_skills=skills
    )["json_schema"]["schema"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(_proposal(None))
    for requested in [
        "code.build_project",
        "code.generate_python",
        "workspace.list_dir",
        "workspace.read_text",
    ]:
        assert validator.is_valid(_proposal(requested)) is (requested in skills)
    malformed = _proposal(None)
    assert isinstance(malformed["nodes"], list)
    malformed["nodes"][0]["node_type"] = "worker"
    assert not validator.is_valid(malformed)


def test_planner_schema_keeps_project_work_exclusive_with_other_advertised_skills() -> None:
    schema = UbuntuSwarmPlannerProvider._response_format(
        available_skills=["code.build_project", "workspace.list_dir"]
    )["json_schema"]["schema"]
    validator = Draft202012Validator(schema)
    mixed = _proposal()
    assert isinstance(mixed["nodes"], list)
    assert validator.is_valid(mixed)
    mixed["nodes"].append(_proposal("workspace.list_dir")["nodes"][0])
    assert not validator.is_valid(mixed)
    project = _proposal()
    assert isinstance(project["nodes"], list)
    project["nodes"][0]["optional_dependencies"] = ["context_hint"]
    assert not validator.is_valid(project)


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", ["code.build_project", "workspace.list_dir", None])
async def test_planner_accepts_an_advertised_worker_or_synthesis_only_context(
    skill: str | None,
) -> None:
    context = _planner_context()
    cards = context["cards"]
    assert isinstance(cards, list)
    cards[:] = [card for card in cards if card["kind"] != "agent_card"]
    if skill:
        cards.append({"kind": "agent_card", "skills": [skill]})
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:8711/v1/chat/completions"),
        json={"choices": [{"message": {"content": json.dumps(_proposal(skill))}}]},
    )
    post = AsyncMock(return_value=response)
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:8711/v1", model="local")
    with patch("httpx.AsyncClient.post", post):
        result = await provider.propose(context)
    assert result.nodes[0].required_skill == skill
    assert post.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_project_worker", [True, False])
async def test_planner_rejects_unadvertised_worker_even_when_transport_ignores_schema(
    with_project_worker: bool,
) -> None:
    context = _planner_context()
    cards = context["cards"]
    assert isinstance(cards, list)
    cards[:] = [card for card in cards if card["kind"] != "agent_card"]
    cards.append(
        {"kind": "episode", "summary": "A legacy code.generate_python worker was once available."}
    )
    if with_project_worker:
        cards.append({"kind": "agent_card", "skills": ["code.build_project"]})
    raw = _proposal("code.generate_python")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:8711/v1/chat/completions"),
        json={"choices": [{"message": {"content": json.dumps(raw)}}]},
    )
    post = AsyncMock(return_value=response)
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:8711/v1", model="local")
    with (
        patch("httpx.AsyncClient.post", post),
        pytest.raises(SwarmPlannerProviderError) as raised,
    ):
        await provider.propose(context)
    assert raised.value.category == "invalid_response"
    assert post.await_count == 1
    assert post.await_args is not None
    schema = post.await_args.kwargs["json"]["response_format"]["json_schema"]["schema"]
    assert not Draft202012Validator(schema).is_valid(raw)


@pytest.mark.asyncio
async def test_planner_rejects_agent_card_without_structured_skills_before_http() -> None:
    context = _planner_context()
    cards = context["cards"]
    assert isinstance(cards, list)
    cards.append({"kind": "agent_card", "summary": "skills: code.build_project"})
    post = AsyncMock()
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:8711/v1", model="local")
    with (
        patch("httpx.AsyncClient.post", post),
        pytest.raises(SwarmPlannerProviderError) as raised,
    ):
        await provider.propose(context)
    assert raised.value.category == "invalid_context"
    post.assert_not_awaited()


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
@pytest.mark.parametrize("summary_length", [4000, 4001])
async def test_swarm_planner_wire_schema_preserves_local_string_limits(
    summary_length: int,
) -> None:
    proposal = {
        "schema_version": "1.0",
        "objective": "goal:goal_current",
        "rationale_summary": "x" * summary_length,
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
        if summary_length == 4000:
            result = await provider.propose(_planner_context())
            assert result.rationale_summary == proposal["rationale_summary"]
        else:
            with pytest.raises(SwarmPlannerProviderError, match="invalid proposal") as raised:
                await provider.propose(_planner_context())
            assert raised.value.category == "invalid_response"
            assert isinstance(raised.value.__cause__, PlanValidationError)
            assert "at most 4000 characters" in str(raised.value.__cause__)

    assert post.await_count == 1
    assert post.await_args is not None
    wire = post.await_args.kwargs["json"]["response_format"]
    assert wire["type"] == "json_schema"
    assert wire["json_schema"]["strict"] is True
    assert '"maxLength"' not in json.dumps(wire["json_schema"]["schema"])
    assert wire["json_schema"]["schema"]["properties"]["objective"]["const"] == "goal:goal_current"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (400, "request_rejected"),
        (404, "request_rejected"),
        (408, "transport_unavailable"),
        (429, "transport_unavailable"),
        (503, "transport_unavailable"),
    ],
)
async def test_planner_classifies_http_failures_without_exposing_provider_body(
    status_code: int, category: str
) -> None:
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "http://127.0.0.1:8711/v1/chat/completions"),
        json={"error": {"message": "private-provider-response"}},
    )
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:8711/v1", model="local")
    with (
        patch("httpx.AsyncClient.post", AsyncMock(return_value=response)),
        pytest.raises(SwarmPlannerProviderError) as raised,
    ):
        await provider.propose(_planner_context())
    assert raised.value.category == category
    assert "private-provider-response" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": [{"message": {"content": {"private": "provider-response"}}}]},
        {"choices": [{"message": {"content": '{"private":"provider-response"}'}}]},
    ],
)
async def test_planner_classifies_invalid_outputs_without_exposing_model_text(
    body: dict[str, object],
) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:8711/v1/chat/completions"),
        json=body,
    )
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:8711/v1", model="local")
    with (
        patch("httpx.AsyncClient.post", AsyncMock(return_value=response)),
        pytest.raises(SwarmPlannerProviderError) as raised,
    ):
        await provider.propose(_planner_context())
    assert raised.value.category == "invalid_response"
    assert "provider-response" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cards",
    [
        [],
        [{"kind": "goal", "card_id": "foreign-card"}],
        [
            {"kind": "goal", "card_id": "goal:goal_current"},
            {"kind": "goal", "card_id": "goal:goal_other"},
        ],
    ],
)
async def test_planner_rejects_ambiguous_goal_binding_before_http(cards: list[object]) -> None:
    post = AsyncMock()
    provider = UbuntuSwarmPlannerProvider(base_url="http://127.0.0.1:8711/v1", model="local")
    with (
        patch("httpx.AsyncClient.post", post),
        pytest.raises(SwarmPlannerProviderError) as raised,
    ):
        await provider.propose({"cards": cards})
    assert raised.value.category == "invalid_context"
    post.assert_not_awaited()
