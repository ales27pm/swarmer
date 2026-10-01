from __future__ import annotations

import copy
import json
from types import ModuleType
from typing import Any

import pytest
from test_text_worker import (
    FakeClient,
    draft,
    generator_for,
    payload,
    research_source,
    stream,
)
from test_text_worker import worker as worker  # noqa: PLC0414


def request() -> dict[str, Any]:
    return {
        **payload(),
        "requirements": {
            "min_words": 150,
            "max_words": 200,
            "min_citations": 2,
            "required_source_domains": ["docs.python.org", "sqlite.org"],
        },
        "research_sources": [
            {**research_source(), "url": "https://docs.python.org/3/library/json.html"},
            {**research_source(), "url": "https://www.sqlite.org/whentouse.html"},
        ],
    }


def diagnostic(words: int = 128) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "kind": "writing_requirement_diagnostics",
        "reason": "writing_requirements_unmet",
        "word_count": words,
        "min_words": 150,
        "max_words": 200,
        "citation_count": 2,
        "min_citations": 2,
        "required_source_domains": ["docs.python.org", "sqlite.org"],
        "cited_source_domains": ["docs.python.org", "www.sqlite.org"],
        "failures": ["min_words" if words < 150 else "max_words"],
    }


@pytest.mark.parametrize("words", [128, 238])
def test_rejected_draft_reports_only_measured_failure_after_final_lease_fence(
    worker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    words: int,
) -> None:
    client = FakeClient()
    client.job["payload"] = request()
    monkeypatch.setattr(worker.protocol, "ControlPlaneClient", lambda *args: client)
    generated = {
        **draft(),
        "text": " ".join(["confidentiel"] * words) + " [S1] [S2]",
        "summary": "private summary",
    }
    generator, connection = generator_for(worker, monkeypatch, stream(generated))
    assert worker.run_once("http://127.0.0.1", "agent", "secret-credential", generator)
    assert client.submitted == [
        {
            "status": "failed",
            "error": "writing_requirements_unmet",
            "result": diagnostic(words),
        }
    ]
    assert client.renewals == 2
    serialized = json.dumps(client.submitted)
    assert all(
        secret not in serialized + caplog.text
        for secret in (
            "confidentiel",
            "private summary",
            "secret-credential",
            "opaque-proof",
            "https://",
        )
    )
    assert len([call for call in connection.calls if call[0] == "POST"]) == 1


def test_measurements_count_only_prose_and_distinct_supplied_urls(
    worker: ModuleType,
) -> None:
    value = request()
    text = (
        " ".join(["client"] * 128)
        + " [S1] [S1]\n\n[S1] <https://docs.python.org/3/library/json.html>"
    )
    with pytest.raises(worker.GenerationError) as failure:
        worker.validate_writing_requirements(
            text, value["requirements"], {s["url"] for s in value["research_sources"]}
        )
    assert failure.value.diagnostics == {
        **diagnostic(),
        "citation_count": 1,
        "cited_source_domains": ["docs.python.org"],
        "failures": ["min_words", "min_citations", "required_source_domains"],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 1},
        {"kind": "private"},
        {"reason": "wall_timeout"},
        {"word_count": True},
        {"word_count": -1},
        {"word_count": 24001},
        {"word_count": "128"},
        {"citation_count": True},
        {"citation_count": -1},
        {"citation_count": 7},
        {"min_words": 149},
        {"max_words": None},
        {"min_citations": True},
        {"required_source_domains": ["sqlite.org", "docs.python.org"]},
        {"cited_source_domains": ["DOCS.PYTHON.ORG", "www.sqlite.org"]},
        {"cited_source_domains": ["https://docs.python.org", "www.sqlite.org"]},
        {"cited_source_domains": ["docs.python.org", "docs.python.org"]},
        {"cited_source_domains": ["www.sqlite.org", "docs.python.org"]},
        {"cited_source_domains": ["foreign.example", "www.sqlite.org"]},
        {"cited_source_domains": ["docs.python.org"]},
        {"cited_source_domains": []},
        {"failures": []},
        {"failures": ["min_words", "private model text"]},
        {"text": "private rejected draft"},
    ],
)
def test_malformed_or_unbound_measurements_are_rejected(
    worker: ModuleType, change: dict[str, Any]
) -> None:
    with pytest.raises(worker.GenerationError):
        worker.validate_failure_diagnostics({**diagnostic(), **change}, request())


def test_diagnostics_require_exact_fields_and_actual_failure(
    worker: ModuleType,
) -> None:
    missing = diagnostic()
    missing.pop("max_words")
    for value in (missing, None, [], {**diagnostic(175), "failures": []}):
        with pytest.raises(worker.GenerationError):
            worker.validate_failure_diagnostics(value, request())


def test_missing_requirements_have_explicit_null_bounds(worker: ModuleType) -> None:
    value = {**payload(), "requirements": {"min_words": 150}}
    with pytest.raises(worker.GenerationError) as failure:
        worker.validate_writing_requirements("client", value["requirements"], set())
    measured = failure.value.diagnostics
    assert measured["max_words"] is None and measured["min_citations"] is None
    assert measured["citation_count"] == 0 and measured["cited_source_domains"] == []
    assert worker.validate_failure_diagnostics(measured, value) == measured


def test_validated_diagnostic_collections_do_not_alias_caller_data(
    worker: ModuleType,
) -> None:
    value = diagnostic()
    validated = worker.validate_failure_diagnostics(value, request())
    value["failures"].append("private")
    assert validated == diagnostic()


def test_malformed_exception_diagnostics_never_leave_worker(
    worker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeClient()
    client.job["payload"] = request()
    monkeypatch.setattr(worker.protocol, "ControlPlaneClient", lambda *args: client)
    generator, _ = generator_for(worker, monkeypatch, stream())

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise worker.GenerationError(
            "private exception",
            reason="writing_requirements_unmet",
            diagnostics={**diagnostic(), "text": "private draft"},
        )

    monkeypatch.setattr(generator, "generate", fail)
    assert worker.run_once("http://127.0.0.1", "agent", "secret", generator)
    assert client.submitted == [{"status": "failed", "error": "writing_requirements_unmet"}]
    assert "private" not in caplog.text


def test_model_cannot_submit_its_own_failure_diagnostics(worker: ModuleType) -> None:
    value = {
        **draft(),
        "outcome": "delivered",
        "diagnostics": copy.deepcopy(diagnostic()),
    }
    with pytest.raises(worker.GenerationError):
        worker._decode_model_result(value, request())


def test_lost_final_lease_suppresses_measured_diagnostics(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    client.job["payload"] = request()
    client.failed_renewal = 2
    client.raise_lease = worker.protocol.LeaseLost("lost")
    monkeypatch.setattr(worker.protocol, "ControlPlaneClient", lambda *args: client)
    generator, connection = generator_for(
        worker, monkeypatch, stream({**draft(), "text": "client [S1]"})
    )
    assert worker.run_once("http://127.0.0.1", "agent", "secret", generator)
    assert client.submitted == []
    assert len([call for call in connection.calls if call[0] == "POST"]) == 1
