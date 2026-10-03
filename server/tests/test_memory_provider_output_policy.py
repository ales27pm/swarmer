"""Operator reasoning opt-in and bounded schema-grounded memory requests."""

from __future__ import annotations

import copy
import json

import httpx
import pytest
from pydantic import ValidationError

from app.main import create_app
from app.services import memory_normalization as normalization
from app.services.memory_normalization import MemoryNormalizationError
from app.settings import Settings
from tests.test_memory_normalization import Model, envelope, provider, source
from tests.test_memory_normalization_api import _configuration
from tests.test_memory_presentation import Models, batch
from tests.test_memory_presentation import provider as presenter_provider


@pytest.mark.parametrize("effort", [None, "", " \t ", "none"])
def test_memory_reasoning_environment_reaches_both_providers(tmp_path, monkeypatch, effort):
    monkeypatch.delenv("MONGARS_MEMORY_NORMALIZATION_REASONING_EFFORT", raising=False)
    if effort is not None:
        monkeypatch.setenv("MONGARS_MEMORY_NORMALIZATION_REASONING_EFFORT", effort)
    app = create_app(
        Settings(
            _env_file=None,
            **_configuration(),
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
            memory_normalization_timeout_seconds=37,
        )
    )
    expected = "none" if effort == "none" else None
    for service in (
        app.state.state_service.memory_normalizer,
        app.state.state_service.memory_presenter,
    ):
        assert service.reasoning_effort == expected
        assert service.timeout_seconds == 37
        assert service.max_output_tokens == 2048
        assert service.translator_model == "explicit-translator"
        assert service.reviewer_model == "explicit-reviewer"
    assert app.state.swarm_planner.reasoning_effort is None


@pytest.mark.parametrize("effort", ["high", "low", "false", "None", True, 0])
def test_memory_reasoning_rejects_unsupported_values(effort):
    with pytest.raises(ValidationError, match="memory_normalization_reasoning_effort"):
        Settings(_env_file=None, memory_normalization_reasoning_effort=effort)


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", [False, True])
@pytest.mark.parametrize("effort", [None, "none"])
async def test_schema_and_reasoning_reach_each_request_without_promoting_user_data(
    presentation, effort
):
    model = Models() if presentation else Model()
    service = (presenter_provider if presentation else provider)(model, reasoning_effort=effort)
    item = batch() if presentation else source()
    original = item.model_dump_json()
    await (service.present(item) if presentation else service.normalize(item))
    assert item.model_dump_json() == original
    assert len(model.calls) == 2
    for call in model.calls:
        wire = call["response_format"]["json_schema"]["schema"]
        compact = json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert call["messages"][0]["content"].endswith(compact)
        assert '"maxLength"' not in compact
        assert call["response_format"]["json_schema"]["strict"] is True
        assert call["temperature"] == 0 and call["max_tokens"] == 2048
        if effort is None:
            assert "reasoning_effort" not in call
        else:
            assert call["reasoning_effort"] == effort
        data = json.loads(call["messages"][1]["content"])
        units = data["units"] if presentation else [data]
        for unit in units:
            assert unit["source_text"] not in call["messages"][0]["content"]


@pytest.mark.parametrize("presentation", [False, True])
def test_reasoning_and_schema_prompt_policy_are_part_of_provider_identity(presentation):
    factory = presenter_provider if presentation else provider
    default = factory(lambda request: None)
    explicit = factory(lambda request: None, reasoning_effort="none")
    assert default.normalization_signature != explicit.normalization_signature
    if presentation:
        assert default.presentation_signature != explicit.presentation_signature
    assert normalization._POLICY["generation_prompt_policy"] == "system-json-schema-v1"
    assert normalization._POLICY["generation_schema_prompt"]
    assert normalization.POLICY_SHA256 == normalization._digest(normalization._POLICY)


@pytest.mark.asyncio
async def test_added_schema_counts_toward_utf8_request_budget_before_http():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return envelope(
            {
                "source_sha256": "a" * 64,
                "source_language": "fr",
                "target_language": "en",
                "text": "Keep the file.",
            }
        )

    service = provider(respond)
    async with httpx.AsyncClient(transport=service.transport) as client:
        await service._request(client, "translator", "", {}, normalization._Translation)
        without_prompt = copy.deepcopy(requests[0])
        without_prompt["messages"][0]["content"] = ""
        remaining = normalization.MAX_REQUEST_BYTES - len(
            normalization._json(without_prompt).encode("utf-8")
        )
        # This exact request would fit before adding the schema instruction.
        prompt = "é" * (remaining // 2) + "x" * (remaining % 2)
        with pytest.raises(MemoryNormalizationError, match="request_budget_exceeded"):
            await service._request(client, "translator", prompt, {}, normalization._Translation)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", [False, True])
@pytest.mark.parametrize("stage", [1, 2])
async def test_reasoning_only_truncation_is_rejected_without_retry(presentation, stage):
    model = Models() if presentation else Model()
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) != stage:
            return await model(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "", "reasoning": "private partial reasoning"},
                    }
                ],
                "usage": {"completion_tokens": 2048},
            },
        )

    service = (presenter_provider if presentation else provider)(respond, reasoning_effort="none")
    with pytest.raises(MemoryNormalizationError, match="invalid_provider_response"):
        await (service.present(batch()) if presentation else service.normalize(source()))
    assert len(requests) == stage
