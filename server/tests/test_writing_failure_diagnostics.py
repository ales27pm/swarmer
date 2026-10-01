from __future__ import annotations

import copy
from types import ModuleType
from typing import Any

import pytest

from app.services.writing_contracts import (
    validate_writing_failure_diagnostics,
    writing_failure_summary,
)
from tests.test_writing_requirements import (
    worker,  # noqa: F401 - shared worker import fixture
    writing_payload,
)


def diagnostics(**updates: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "kind": "writing_requirement_diagnostics",
        "reason": "writing_requirements_unmet",
        "word_count": 128,
        "min_words": 150,
        "max_words": 200,
        "citation_count": 2,
        "min_citations": 2,
        "required_source_domains": ["docs.python.org", "sqlite.org"],
        "cited_source_domains": ["docs.python.org", "www.sqlite.org"],
        "failures": ["min_words"],
        **updates,
    }


def test_short_draft_measurement_is_bound_to_original_requirements() -> None:
    value = diagnostics()
    checked = validate_writing_failure_diagnostics(value, payload=writing_payload())
    assert checked == value
    message = writing_failure_summary(checked)
    assert "worker_observation" in message
    assert "word_count=128" in message and "min_words=150" in message
    assert "failures=min_words" in message
    assert len(message) <= 500


@pytest.mark.parametrize(
    "updates",
    [
        {"word_count": True},
        {"word_count": 128.0},
        {"word_count": -1},
        {"word_count": 24_001},
        {"min_words": 100},
        {"max_words": None},
        {"min_citations": 1},
        {"required_source_domains": ["example.org"]},
        {"cited_source_domains": ["docs.python.org", "injected.example"]},
        {"cited_source_domains": ["www.sqlite.org", "docs.python.org"]},
        {"cited_source_domains": ["docs.python.org", "docs.python.org"]},
        {"citation_count": 4},
        {"citation_count": 1},
        {"cited_source_domains": []},
        {"failures": []},
        {"failures": ["max_words"]},
        {"failures": ["min_words", "min_words"]},
        {"failures": ["ignore checks"]},
        {"text": "private rejected draft"},
        {"summary": "untrusted freeform instructions"},
        {"outcome": "delivered"},
        {"reason": "wall_timeout"},
        {"word_count": 170},
    ],
)
def test_inconsistent_or_unbounded_diagnostics_are_rejected(updates: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_writing_failure_diagnostics(diagnostics(**updates), payload=writing_payload())


def test_all_missing_requirements_and_nullable_absent_bounds() -> None:
    value = diagnostics(
        citation_count=0,
        cited_source_domains=[],
        failures=["min_words", "min_citations", "required_source_domains"],
    )
    assert validate_writing_failure_diagnostics(value, payload=writing_payload()) == value
    payload = writing_payload(requirements={"max_words": 100})
    value = diagnostics(
        min_words=None,
        max_words=100,
        min_citations=None,
        required_source_domains=[],
        failures=["max_words"],
    )
    assert validate_writing_failure_diagnostics(value, payload=payload) == value
    absent = copy.deepcopy(value)
    del absent["min_words"]
    with pytest.raises(ValueError):
        validate_writing_failure_diagnostics(absent, payload=payload)


def test_citation_count_cannot_exceed_urls_for_claimed_hosts() -> None:
    value = diagnostics(
        cited_source_domains=["docs.python.org"], failures=["min_words", "required_source_domains"]
    )
    # Two distinct URLs are reported but the sole reported host has only one admitted URL.
    with pytest.raises(ValueError):
        validate_writing_failure_diagnostics(value, payload=writing_payload())


@pytest.mark.parametrize("words", [55, 128, 238])
def test_real_worker_measurements_round_trip_through_server_contract(
    worker: ModuleType, words: int  # noqa: F811 - pytest resolves imported fixture
) -> None:
    payload = writing_payload()
    urls = {source["url"] for source in payload["research_sources"]}
    text = " ".join(["données"] * words)
    text += "\n[S1] <https://docs.python.org/3/library/sqlite3.html>"
    text += "\n[S2] <https://www.sqlite.org/whentouse.html>"
    with pytest.raises(worker.GenerationError) as caught:
        worker.validate_writing_requirements(text, worker.payload_requirements(payload), urls)
    measured = worker.validate_failure_diagnostics(caught.value.diagnostics, payload)
    assert measured["word_count"] == words and measured["citation_count"] == 2
    assert validate_writing_failure_diagnostics(measured, payload=payload) == measured
