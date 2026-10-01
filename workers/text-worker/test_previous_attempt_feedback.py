from __future__ import annotations

import copy
import json
from types import ModuleType
from typing import Any

import pytest
from test_failure_diagnostics import diagnostic, request
from test_text_worker import draft, generator_for, stream
from test_text_worker import worker as worker  # noqa: PLC0414


def feedback() -> dict[str, Any]:
    return {"node_id": "node_failed", "worker_job_id": "job_failed", "diagnostics": diagnostic(225)}


def test_explicit_feedback_reaches_single_model_call_without_changing_acceptance(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = request()
    value = {**original, "previous_attempt_feedback": feedback()}
    generator, connection = generator_for(
        worker, monkeypatch, stream({**draft(), "text": " ".join(["données"] * 175) + " [S1] [S2]"})
    )
    generated = generator.generate(value, ensure_active=lambda: None)
    calls = [call for call in connection.calls if call[0] == "POST"]
    assert len(calls) == 1
    body = calls[0][2]
    projected = json.loads(body["messages"][1]["content"])
    assert projected["previous_attempt_feedback"] == feedback()
    assert "previous_attempt_feedback" in body["messages"][0]["content"]
    assert "not a target" in body["messages"][0]["content"]
    assert projected["requirements"] == original["requirements"]
    assert worker.output_token_budget(value) == worker.output_token_budget(original)
    assert worker.writing_word_count(generated["text"]) == 175
    with pytest.raises(worker.GenerationError, match="writing_requirements_unmet"):
        worker.validate_writing_requirements(
            " ".join(["données"] * 225),
            original["requirements"],
            {s["url"] for s in original["research_sources"]},
        )


@pytest.mark.parametrize(
    "change",
    [
        None,
        [],
        {},
        {"text": "private rejected text"},
        {"node_id": "foreign/path"},
        {"worker_job_id": "secret"},
        {"diagnostics": {**diagnostic(225), "word_count": True}},
        {"diagnostics": {**diagnostic(225), "max_words": 300}},
        {"diagnostics": {**diagnostic(225), "failures": ["min_words"]}},
        {"diagnostics": {**diagnostic(225), "text": "private rejected text"}},
        {"diagnostics": {**diagnostic(225), "cited_source_domains": ["evil.example"]}},
    ],
)
def test_malformed_feedback_rejected_before_model_call(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, change: object
) -> None:
    changed = {**feedback(), **change} if isinstance(change, dict) and change else change
    generator, connection = generator_for(worker, monkeypatch, stream())
    with pytest.raises(worker.GenerationError):
        generator.generate(
            {**request(), "previous_attempt_feedback": changed}, ensure_active=lambda: None
        )
    assert connection.calls == []


def test_initial_input_has_no_feedback_and_validated_feedback_is_copied(worker: ModuleType) -> None:
    assert (
        "previous_attempt_feedback"
        not in worker._model_input(worker.validate_payload(request()))[0]
    )
    value = {**request(), "previous_attempt_feedback": feedback()}
    checked = worker.validate_payload(copy.deepcopy(value))
    value["previous_attempt_feedback"]["diagnostics"]["failures"].clear()
    assert checked["previous_attempt_feedback"]["diagnostics"]["failures"] == ["max_words"]


def test_complete_durable_user_instruction_reaches_actual_model_request(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = {
        "version": 1,
        "fingerprint": "a" * 64,
        "base_revision_id": None,
        "requirements": [
            {
                "source_id": "msg_earlier",
                "text": "Use Canadian French and state accessibility limitations.",
            }
        ],
    }
    value = {**request(), "durable_context": capsule}
    generator, connection = generator_for(
        worker, monkeypatch, stream({**draft(), "text": " ".join(["données"] * 175) + " [S1] [S2]"})
    )
    generator.generate(value, ensure_active=lambda: None)
    calls = [call for call in connection.calls if call[0] == "POST"]
    assert len(calls) == 1
    projected = json.loads(calls[0][2]["messages"][1]["content"])
    assert projected["durable_context"] == capsule
    assert "historical observations" in calls[0][2]["messages"][0]["content"]
    assert worker.output_token_budget(value) == worker.output_token_budget(request())


@pytest.mark.parametrize(
    "capsule",
    [
        None,
        {
            "version": 1,
            "fingerprint": "a" * 64,
            "base_revision_id": None,
            "requirements": [{"source_id": "msg_required", "text": "é" * 16001}],
        },
    ],
)
def test_invalid_or_oversized_required_capsule_stops_before_model_call(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, capsule: object
) -> None:
    generator, connection = generator_for(worker, monkeypatch, stream())
    with pytest.raises(worker.GenerationError):
        generator.generate({**request(), "durable_context": capsule}, ensure_active=lambda: None)
    assert connection.calls == []
