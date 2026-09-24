from __future__ import annotations

import pytest

from app.services.result_aggregator import validate_worker_evidence
from app.services.writing_contracts import (
    validate_writing_declined_result,
    validate_writing_result,
)


def refusal() -> dict[str, str]:
    return {
        "schema_version": "1.0",
        "content_trust": "untrusted",
        "text": "Je ne peux pas rédiger ce calendrier familial.",
        "summary": "Demande déclinée.",
        "outcome": "declined",
        "model_id": "local-model:7b",
    }


def test_declined_contract_is_not_success_evidence() -> None:
    result = refusal()
    assert validate_writing_declined_result(result) == result
    assert not validate_worker_evidence("writing.draft", result)
    with pytest.raises(ValueError):
        validate_writing_result(result)


@pytest.mark.parametrize(
    "change",
    [
        {"model_id": ""},
        {"model_id": "bad model"},
        {"model_id": "a" * 501},
        {"model_id": "x\n"},
        {"model_id": 7},
        {"outcome": "delivered"},
        {"text": " "},
        {"text": "x" * 24_001},
        {"tool_call": "do something"},
    ],
)
def test_declined_result_remains_bounded_and_strict(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_writing_declined_result({**refusal(), **change})


def test_declined_result_cannot_bypass_source_validation() -> None:
    payload = {
        "schema_version": "1.0",
        "objective": "Rédige un calendrier.",
        "conversation": [],
        "research_sources": [
            {
                "content_trust": "untrusted",
                "worker_job_id": "job_source",
                "title": "Source",
                "url": "https://example.org/source",
                "snippet": "Extrait",
            }
        ],
    }
    with pytest.raises(ValueError):
        validate_writing_declined_result(
            {**refusal(), "text": "Voir https://invented.example/refusal"}, payload=payload
        )
