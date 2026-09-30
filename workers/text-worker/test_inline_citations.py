from __future__ import annotations

from types import ModuleType

import pytest
from jsonschema import Draft202012Validator, ValidationError
from test_text_worker import draft, payload, research_source
from test_text_worker import worker as worker  # noqa: PLC0414


def request() -> dict:
    return {
        **payload(),
        "research_sources": [
            {**research_source(), "url": "https://docs.python.org/3/library/json.html"},
            {**research_source(), "url": "https://sqlite.org/whentouse.html"},
        ],
        "requirements": {"min_citations": 2, "required_source_domains": ["sqlite.org"]},
    }


def response() -> dict:
    return {
        **draft(),
        "outcome": "delivered",
        "text": "Un fichier local [S2]. La sérialisation [S1]. Encore la même source [S2].",
    }


def test_generation_uses_only_claim_markers(worker: ModuleType) -> None:
    _, schema = worker._model_input(worker.validate_payload(request()))
    validator = Draft202012Validator(schema)
    validator.validate(response())
    with pytest.raises(ValidationError):
        validator.validate({**response(), "source_ids": ["S1", "S2"]})


def test_markers_resolve_once_in_first_citation_order(worker: ModuleType) -> None:
    result = worker._decode_model_result(response(), request())
    assert result["text"] == response()["text"] + (
        "\n\n[S2] <https://sqlite.org/whentouse.html>"
        "\n[S1] <https://docs.python.org/3/library/json.html>"
    )
    assert set(result) == set(worker.RESPONSE_SCHEMA["required"])


def test_unused_sources_are_not_attached_when_no_citations_are_required(worker: ModuleType) -> None:
    value = request()
    value["requirements"] = {}
    result = worker._decode_model_result({**draft(), "outcome": "delivered"}, value)
    assert result == draft()


@pytest.mark.parametrize("outcome", ["declined", "needs_clarification", "insufficient_sources"])
def test_non_delivery_needs_no_redundant_source_selection(worker: ModuleType, outcome: str) -> None:
    value = {**draft(), "outcome": outcome}
    if outcome == "needs_clarification":
        value["question"] = "Quelle année faut-il utiliser pour ce calendrier ?"
    result = worker._decode_model_result(value, request(), model_id="configured-writer")
    assert result["outcome"] == outcome
    assert result["model_id"] == "configured-writer"
    assert "source_ids" not in result


@pytest.mark.parametrize(
    "change",
    [
        {"text": "Une référence inconnue [S99]."},
        {"text": "Une référence malformée [Snot-a-source]."},
        {"text": "https://invented.example/ [S1] [S2]"},
        {"text": "Aucune citation."},
        {"text": "Une seule source [S1]."},
        {"summary": "Source uniquement dans le résumé [S99]."},
        {"model_id": "invented-provenance"},
        {"question": "Quel est le sujet exact à traiter ici ?"},
    ],
)
def test_marker_format_preserves_delivery_guards(worker: ModuleType, change: dict) -> None:
    with pytest.raises(worker.GenerationError):
        worker._decode_model_result({**response(), **change}, request())


@pytest.mark.parametrize("outcome", ["declined", "needs_clarification", "insufficient_sources"])
def test_non_delivery_cannot_use_markers_as_evidence(worker: ModuleType, outcome: str) -> None:
    value = {**response(), "outcome": outcome}
    if outcome == "needs_clarification":
        value["question"] = "Quelle année faut-il utiliser pour ce calendrier ?"
    with pytest.raises(worker.GenerationError):
        worker._decode_model_result(value, request(), model_id="configured-writer")
