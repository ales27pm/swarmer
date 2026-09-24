from __future__ import annotations

import copy
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from test_text_worker import (
    FakeClient,
    draft,
    event,
    generator_for,
    payload,
    research_source,
    stream,
)
from test_text_worker import (
    worker as worker,  # noqa: PLC0414 - re-export the shared pytest fixture
)


def sourced_payload(*sources: dict[str, str]) -> dict[str, Any]:
    return {**payload(), "research_sources": list(sources) or [research_source()]}


def test_native_source_ids_project_evidence_and_resolve_exact_urls(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = {
        **research_source(),
        "title": "Événements à Québec — https://example.org/title-link",
        "url": "https://bibliothèque.example/activités/été?q=é#été",
        "snippet": "Atelier 🧑‍🎨. Détail https://example.org/snippet-link",
    }
    value = sourced_payload(source)
    urls = " ".join(source["url"] + suffix for suffix in ("", "-2", "?page=2", "#section"))
    value["objective"] += " " + urls + " https://example.net/user-link"
    value["conversation"][0]["content"] += " " + urls
    original = copy.deepcopy(value)
    private = {
        **draft(),
        "outcome": "delivered",
        "text": "Un atelier est annoncé [S1].",
        "source_ids": ["S1"],
    }
    generator, connection = generator_for(worker, monkeypatch, stream(private))
    result = generator.generate(value, ensure_active=lambda: None)
    assert result["text"] == "Un atelier est annoncé [S1].\n\n[S1] <" + source["url"] + ">"
    assert set(result) == set(draft())
    assert value == original
    requests = [call for call in connection.calls if call[0] == "POST"]
    assert len(requests) == 1
    body = requests[0][2]
    projected = json.loads(body["messages"][1]["content"])
    assert source["url"] not in json.dumps(projected["research_sources"], ensure_ascii=False)
    assert projected["objective"] == value["objective"]
    assert projected["conversation"] == value["conversation"]
    assert projected["research_sources"] == [
        {
            "content_trust": "untrusted",
            "source_id": "S1",
            "hostname": "bibliothèque.example",
            "title": "Événements à Québec — [URL omitted]",
            "snippet": "Atelier 🧑‍🎨. Détail [URL omitted]",
        }
    ]
    assert body["options"] == {"temperature": 0, "num_predict": 512, "num_gpu": 0}
    assert body["think"] is False
    schema = Draft202012Validator(body["format"])
    schema.validate(private)
    schema.validate({**private, "source_ids": []})
    for invalid in (["S2"], ["S1", "S1"], [1], None):
        with pytest.raises(ValidationError):
            schema.validate({**private, "source_ids": invalid})
    assert "source_ids" not in worker.RESPONSE_SCHEMA["properties"]


@pytest.mark.parametrize(
    "ids", [None, {}, "S1", [1], [True], [[]], ["S0"], ["S99"], ["s1"], ["S1", "S1"]]
)
def test_native_decoder_rejects_invalid_selected_ids_even_if_model_ignores_schema(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, ids: object
) -> None:
    generator, _ = generator_for(worker, monkeypatch, stream({**draft(), "source_ids": ids}))
    with pytest.raises(worker.GenerationError, match="source IDs"):
        generator.generate(sourced_payload(), ensure_active=lambda: None)


@pytest.mark.parametrize("key", ["text", "summary"])
@pytest.mark.parametrize(
    "reference,ids", [("S99", ["S1"]), ("S1", []), ("S01", ["S1"]), ("Sx", ["S1"])]
)
def test_unknown_or_unselected_markers_are_rejected_in_both_text_fields(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, key: str, reference: str, ids: list[str]
) -> None:
    private = {**draft(), key: f"Information [{reference}].", "source_ids": ids}
    generator, _ = generator_for(worker, monkeypatch, stream(private))
    with pytest.raises(worker.GenerationError, match="reference"):
        generator.generate(sourced_payload(), ensure_active=lambda: None)


@pytest.mark.parametrize(
    "change", [{}, {"citations": []}, {"source_ids": [], "url": "https://example.org"}]
)
def test_sourced_native_response_cannot_mix_or_omit_private_fields(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    generator, _ = generator_for(worker, monkeypatch, stream({**draft(), **change}))
    with pytest.raises(worker.GenerationError, match="fields"):
        generator.generate(sourced_payload(), ensure_active=lambda: None)


def test_duplicate_json_source_ids_field_is_rejected_before_decoding(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.dumps(draft())[:-1] + ',"source_ids":["S1"],"source_ids":[]}'
    generator, _ = generator_for(worker, monkeypatch, [event(raw, done=True)])
    with pytest.raises(worker.GenerationError) as error:
        generator.generate(sourced_payload(), ensure_active=lambda: None)
    assert worker.failure_reason(error.value) == "invalid_json"


@pytest.mark.parametrize("key", ["text", "summary"])
@pytest.mark.parametrize("url", ["https://invented.example/path", "HTTPS://example.org/activites"])
def test_any_model_url_is_rejected_in_sourced_private_fields(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, key: str, url: str
) -> None:
    generator, _ = generator_for(
        worker, monkeypatch, stream({**draft(), key: url, "source_ids": ["S1"]})
    )
    with pytest.raises(worker.GenerationError) as error:
        generator.generate(sourced_payload(), ensure_active=lambda: None)
    assert worker.failure_reason(error.value) == "unsupported_citation"


def test_captured_facebook_rewrite_cannot_survive_but_id_preserves_original(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = json.loads(Path(__file__).with_name("citation-facebook.json").read_text())
    value = sourced_payload({**research_source(), "url": fixture["original_url"]})
    corrupt = {**draft(), "text": fixture["corrupt_text"], "source_ids": ["S1"]}
    generator, _ = generator_for(worker, monkeypatch, stream(corrupt))
    with pytest.raises(worker.GenerationError) as error:
        generator.generate(value, ensure_active=lambda: None)
    assert worker.failure_reason(error.value) == "unsupported_citation"
    generator, connection = generator_for(
        worker, monkeypatch, stream({**draft(), "source_ids": ["S1"]})
    )
    result = generator.generate(value, ensure_active=lambda: None)
    assert result["text"].endswith("[S1] <" + fixture["original_url"] + ">")
    assert fixture["original_url"] not in json.dumps(connection.calls)
    assert worker.validate_result(result, value) == result


def test_exact_final_utf8_boundary_counts_attached_urls(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = sourced_payload({**research_source(), "url": "https://é.example/été"})
    suffix = "\n\n[S1] <https://é.example/été>"
    text = "é" + "a" * (worker.MAX_TEXT_BYTES - len(suffix.encode()) - 2)
    private = {**draft(), "text": text, "source_ids": ["S1"]}
    generator, _ = generator_for(worker, monkeypatch, stream(private))
    result = generator.generate(value, ensure_active=lambda: None)
    assert len(result["text"].encode()) == worker.MAX_TEXT_BYTES
    generator, _ = generator_for(worker, monkeypatch, stream({**private, "text": text + "a"}))
    with pytest.raises(worker.GenerationError, match="byte limit"):
        generator.generate(value, ensure_active=lambda: None)


def test_all_five_sources_and_empty_selection_are_bounded(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = [{**research_source(), "url": f"https://example.org/{index}"} for index in range(5)]
    private = {**draft(), "source_ids": ["S5", "S1"]}
    generator, connection = generator_for(worker, monkeypatch, stream(private))
    result = generator.generate(sourced_payload(*sources), ensure_active=lambda: None)
    assert result["text"].endswith("[S5] <https://example.org/4>\n[S1] <https://example.org/0>")
    body = next(call[2] for call in connection.calls if call[0] == "POST")
    assert body["format"]["properties"]["source_ids"]["maxItems"] == 5
    private = {**draft(), "text": "Les extraits sont insuffisants pour répondre.", "source_ids": []}
    generator, _ = generator_for(worker, monkeypatch, stream(private))
    result = generator.generate(sourced_payload(), ensure_active=lambda: None)
    assert result["text"] == private["text"] and "source_ids" not in result


@pytest.mark.parametrize(
    "sources",
    [
        [research_source()] * 6,
        [{**research_source(), "title": "😀" * 240, "snippet": "😀" * 700}] * 5,
    ],
)
def test_excess_source_count_or_utf8_bytes_never_reaches_transport(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, sources: list[dict[str, str]]
) -> None:
    generator, connection = generator_for(
        worker, monkeypatch, stream({**draft(), "source_ids": []})
    )
    with pytest.raises(worker.GenerationError):
        generator.generate(sourced_payload(*sources), ensure_active=lambda: None)
    assert connection.calls == []


@pytest.mark.parametrize("sources", [None, []])
def test_unsourced_transport_and_historical_canonical_results_are_unchanged(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, sources: object
) -> None:
    value = payload() if sources is None else {**payload(), "research_sources": sources}
    legacy = {**draft(), "text": "Voir https://example.org/example."}
    generator, connection = generator_for(worker, monkeypatch, stream(legacy))
    assert generator.generate(value, ensure_active=lambda: None) == legacy
    body = next(call[2] for call in connection.calls if call[0] == "POST")
    assert body["format"] == worker.MODEL_RESPONSE_SCHEMA
    assert body["messages"][0]["content"] == worker.SYSTEM_PROMPT
    assert json.loads(body["messages"][1]["content"]) == value
    with pytest.raises(worker.GenerationError):
        worker.validate_result({**legacy, "source_ids": []}, value)
    historical = {**draft(), "text": "Voir <https://example.org/activites>."}
    assert worker.validate_result(historical, sourced_payload()) == historical


def test_sourced_run_once_submits_only_canonical_result_accepted_by_server(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "server"))
    from app.services.writing_contracts import validate_writing_result

    client = FakeClient()
    client.job["payload"] = sourced_payload()
    monkeypatch.setattr(worker.protocol, "ControlPlaneClient", lambda *args: client)
    generator, _ = generator_for(worker, monkeypatch, stream({**draft(), "source_ids": ["S1"]}))
    assert worker.run_once("http://127.0.0.1", "agent", "secret", generator)
    assert len(client.submitted) == 1 and client.renewals == 2
    submitted = client.submitted[0]
    assert submitted["status"] == "completed"
    result = submitted["result"]
    assert set(result) == set(draft())
    assert validate_writing_result(result, payload=client.job["payload"]) == result
