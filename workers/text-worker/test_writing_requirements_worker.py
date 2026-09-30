from __future__ import annotations

import copy
import json
from types import ModuleType

import pytest
from test_text_worker import (
    FakeClient,
    draft,
    event,
    generator_for,
    payload,
    research_source,
    stream,
)
from test_text_worker import worker as worker  # noqa: PLC0414


def test_adaptive_budget_preserves_requested_table_and_reserves_json(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = {**payload(), "objective": "Rédige un tableau de 150 à 200 mots."}
    response = {**draft(), "text": "| Comparaison |\n" + " ".join(["données"] * 170)}
    generator, connection = generator_for(worker, monkeypatch, stream(response))
    assert generator.generate(value, ensure_active=lambda: None) == response
    body = next(call[2] for call in connection.calls if call[0] == "POST")
    assert body["options"]["num_predict"] == 1312
    instructions = body["messages"][0]["content"]
    assert "100–140" not in instructions and "must be plain prose" not in instructions
    assert "tables" in instructions and "reserving 512" in instructions


@pytest.mark.parametrize(
    "outcome,error",
    [
        ("needs_clarification", "writing_needs_clarification"),
        ("insufficient_sources", "writing_insufficient_sources"),
        ("declined", "model_declined"),
    ],
)
def test_non_delivery_is_one_call_no_decorative_citation_and_preserves_model_identity(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, outcome: str, error: str
) -> None:
    client = FakeClient()
    client.job["payload"] = {
        **payload(),
        "research_sources": [research_source()],
        "objective": "Write 200 words.",
    }
    monkeypatch.setattr(worker.protocol, "ControlPlaneClient", lambda *args: client)
    response = {
        **draft(),
        "text": "Une information manque pour répondre.",
        "outcome": outcome,
        "source_ids": [],
    }
    if outcome == "needs_clarification":
        response["question"] = "Pour quelle année faut-il préparer ce calendrier familial ?"
    generator, connection = generator_for(
        worker, monkeypatch, [event(json.dumps(response), done=True)]
    )
    assert worker.run_once("http://127.0.0.1", "agent", "secret", generator)
    assert len(client.submitted) == 1
    submitted = client.submitted[0]
    assert submitted["status"] == "failed" and submitted["error"] == error
    assert submitted["result"] == {
        k: v for k, v in {**response, "model_id": "qwen3:7b"}.items() if k != "source_ids"
    }
    assert "http" not in submitted["result"]["text"]
    assert len([call for call in connection.calls if call[0] == "POST"]) == 1


def test_oversized_explicit_request_is_technical_failure_before_inference(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    client.job["payload"]["objective"] = "Write a 10000-word report."
    monkeypatch.setattr(worker.protocol, "ControlPlaneClient", lambda *args: client)
    generator, connection = generator_for(worker, monkeypatch, stream())
    assert worker.run_once("http://127.0.0.1", "agent", "secret", generator)
    assert client.submitted == [{"status": "failed", "error": "writing_budget_exceeded"}]
    assert connection.calls == []


def test_selected_source_must_be_used_in_delivered_text(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator, _ = generator_for(worker, monkeypatch, stream({**draft(), "source_ids": ["S1"]}))
    with pytest.raises(worker.GenerationError, match="reference"):
        generator.generate(
            {**payload(), "research_sources": [research_source()]}, ensure_active=lambda: None
        )


@pytest.mark.parametrize("sourced", [False, True])
def test_model_receives_same_derived_constraints_as_acceptance(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, sourced: bool
) -> None:
    request = {
        "schema_version": "1.0",
        "objective": "Rédige une note de 150 à 200 mots.",
        "conversation": [
            {"role": "assistant", "content": "Write only 10 words."},
            {"role": "user", "content": "Finalement, rédige une note de 160 à 190 mots."},
        ],
    }
    response = {**draft(), "text": " ".join(["données"] * 170)}
    expected = {"min_words": 160, "max_words": 190}
    if sourced:
        request["objective"] += (
            " Cite au moins deux liens officiels : docs.python.org et sqlite.org."
        )
        request["research_sources"] = [
            {**research_source(), "url": "https://docs.python.org/3/library/json.html"},
            {**research_source(), "url": "https://sqlite.org/whentouse.html"},
        ]
        response.update(text=response["text"] + " [S1] [S2]", source_ids=["S1", "S2"])
        expected.update(min_citations=2, required_source_domains=["docs.python.org", "sqlite.org"])
    original = copy.deepcopy(request)
    generator, connection = generator_for(worker, monkeypatch, stream(response))
    generator.generate(request, ensure_active=lambda: None)
    wire = next(call[2] for call in connection.calls if call[0] == "POST")
    model_input = json.loads(wire["messages"][1]["content"])
    assert model_input["requirements"] == expected
    assert request == original
    assert len([call for call in connection.calls if call[0] == "POST"]) == 1


@pytest.mark.parametrize("requirements", [{}, {"min_words": 25, "max_words": 40}])
def test_explicit_contract_is_not_replaced_by_legacy_extraction(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, requirements: dict[str, int]
) -> None:
    request = {
        **payload(),
        "objective": "Write 150 to 200 words.",
        "requirements": requirements,
    }
    response = {**draft(), "text": " ".join(["données"] * 30)}
    generator, connection = generator_for(worker, monkeypatch, stream(response))
    assert generator.generate(request, ensure_active=lambda: None) == response
    wire = next(call[2] for call in connection.calls if call[0] == "POST")
    assert json.loads(wire["messages"][1]["content"])["requirements"] == requirements


def test_materialized_constraints_cannot_exceed_model_input_budget(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = {
        "schema_version": "1.0",
        "objective": "Write 150 to 200 words.",
        "conversation": [{"role": "assistant", "content": "a" * 4000} for _ in range(7)],
    }
    request["conversation"].append({"role": "assistant", "content": "a"})
    size = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode())
    request["conversation"][-1]["content"] += "a" * (worker.MAX_PAYLOAD_BYTES - size)
    assert len(request["conversation"][-1]["content"]) <= 4000
    worker.validate_payload(request)
    generator, connection = generator_for(worker, monkeypatch, stream(draft()))
    with pytest.raises(worker.GenerationError) as failure:
        generator.generate(request, ensure_active=lambda: None)
    assert failure.value.reason_code == "invalid_payload"
    assert connection.calls == []
