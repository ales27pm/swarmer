from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from model_transport import (
    build_model_transport_receipt,
    new_model_attempt_id,
    utc_timestamp,
)

_GOAL = "goal_6fc92c9fa71d42b7ab478417486467ae"
_JOB = "job_8fb6db328150431283da3d3ffb526c7d"
_ATTEMPT = "model_attempt_0123456789abcdef0123456789abcdef"


def receipt(**changes: Any) -> dict[str, Any]:
    return build_model_transport_receipt(
        **{
            "goal_id": _GOAL,
            "job_id": _JOB,
            "attempt_id": _ATTEMPT,
            "started_at": "2026-10-01T16:44:40+00:00",
            "finished_at": "2026-10-01T16:48:40+00:00",
            "outcome": "success",
            "context_tokens": 64_000,
            "prompt_max_bytes": 50_000,
            "wall_timeout_seconds": 240,
            "read_timeout_seconds": 240.0,
            **changes,
        }
    )


def test_success_retains_only_actual_terminal_counters_with_units() -> None:
    result = receipt(
        transport_metrics={
            "prompt_bytes": 44_628,
            "schema_bytes": 13_537,
            "output_token_limit": 2_000,
            "compact_repair": 0,
            "chunks": 50,
            "response_bytes": 8_000,
            "content_bytes": 800,
            "terminal_received": 1,
            "headers_ms": 1_200,
            "first_chunk_ms": 1_203,
            "first_content_ms": 1_400,
            "elapsed_ms": 1_600,
        },
        terminal_envelope={
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 12_000,
            "eval_count": 199,
            "load_duration": 100_000_000,
            "prompt_eval_duration": 200_000_000,
            "eval_duration": 1_000_000_000,
            "total_duration": 1_500_000_000,
            "message": {"content": "secret model output", "thinking": "secret reasoning"},
        },
    )
    assert result["goal_id"] == _GOAL and result["job_id"] == _JOB
    assert result["attempt_id"] == _ATTEMPT and result["scope"] == "model_transport"
    assert result["request"]["prompt_bytes"] == 44_628
    assert result["request"]["schema_bytes"] == 13_537
    assert result["request"]["output_token_limit"] == 2_000
    assert result["ollama"]["eval_count"] == 199
    assert result["ollama"]["duration_unit"] == "ns"
    assert result["ollama"]["total_duration"] == 1_500_000_000
    assert result["client_timing_ms"]["first_content_ms"] == 1_400
    assert "secret" not in json.dumps(result, allow_nan=False)


def test_incomplete_length_preserves_evidence_without_claiming_success() -> None:
    result = receipt(
        outcome="incomplete",
        failure_category="incomplete_response",
        transport_metrics={"terminal_received": 1, "output_token_limit": 2_000},
        terminal_envelope={"done": True, "done_reason": "length", "eval_count": 2_000},
    )
    assert result["outcome"] == "incomplete"
    assert result["ollama"]["done_reason"] == "length"
    assert result["ollama"]["eval_count"] == 2_000
    assert result["ollama"]["prompt_eval_count"] is None


@pytest.mark.parametrize("after_content", [False, True])
def test_timeout_does_not_invent_terminal_tokens_or_phase_durations(after_content: bool) -> None:
    metrics = {
        "prompt_bytes": 44_628,
        "schema_bytes": 13_537,
        "output_token_limit": 2_000,
        "chunks": 43 if after_content else 0,
        "response_bytes": 7_312 if after_content else 0,
        "content_bytes": 112 if after_content else 0,
        "terminal_received": 0,
        "elapsed_ms": 240_001,
    }
    if after_content:
        metrics.update(headers_ms=228_853, first_chunk_ms=228_855, first_content_ms=228_855)
    result = receipt(outcome="timeout", failure_category="timeout", transport_metrics=metrics)
    assert result["transport"]["content_bytes"] == (112 if after_content else 0)
    assert result["client_timing_ms"]["headers_ms"] == (228_853 if after_content else None)
    assert result["transport"]["terminal_received"] is False
    assert result["ollama"] == {
        "duration_unit": "ns",
        "done": None,
        "done_reason": None,
        "prompt_eval_count": None,
        "eval_count": None,
        "load_duration": None,
        "prompt_eval_duration": None,
        "eval_duration": None,
        "total_duration": None,
    }


def test_partial_event_is_not_terminal_proof() -> None:
    result = receipt(
        outcome="incomplete",
        terminal_envelope={"done": False, "done_reason": "length", "eval_count": 2_000},
    )
    assert result["ollama"]["done_reason"] is None
    assert result["ollama"]["eval_count"] is None


@pytest.mark.parametrize("value", [True, -1, 1.2, "2000", float("nan"), float("inf"), 1 << 64])
def test_invalid_counters_stay_unknown_and_json_safe(value: object) -> None:
    result = receipt(
        context_tokens=value,
        transport_metrics={"content_bytes": value, "elapsed_ms": value},
        terminal_envelope={"done": True, "eval_count": value},
    )
    assert result["request"]["context_tokens"] is None
    assert result["transport"]["content_bytes"] is None
    assert result["client_timing_ms"]["elapsed_ms"] is None
    assert result["ollama"]["eval_count"] is None
    json.dumps(result, allow_nan=False)


def test_unknown_fields_strings_and_identifiers_cannot_leak_content() -> None:
    secret = "https://private.example/?token=DO_NOT_LOG"
    result = receipt(
        goal_id=secret,
        job_id="job_1\n" + secret,
        attempt_id=secret,
        started_at=secret,
        finished_at=secret,
        outcome=secret,
        failure_category=secret,
        transport_metrics={
            "headers": {"Authorization": secret},
            "url": secret,
            "compact_completion": secret,
            "compact_authoring": secret,
        },
        terminal_envelope={
            "done": True,
            "done_reason": secret,
            "message": {"content": secret, "thinking": secret},
            "error": secret,
            "context": [secret],
            "model": secret,
        },
    )
    serialized = json.dumps(result, allow_nan=False)
    assert "DO_NOT_LOG" not in serialized and "private.example" not in serialized
    assert all(result[key] is None for key in ("goal_id", "job_id", "attempt_id", "outcome"))
    assert result["started_at"] is None and result["finished_at"] is None
    assert result["ollama"]["done_reason"] is None
    assert result["failure_category"] is None
    assert result["request"]["compact_completion"] is None
    assert result["request"]["compact_authoring"] is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), (1, True), (0, False), (None, None),
     ("true", None), (2, None), (-1, None), (1.0, None), ({"prompt": "private"}, None)],
)
@pytest.mark.parametrize("flag", ["compact_completion", "compact_authoring"])
def test_compact_completion_flag_is_bounded_and_does_not_imply_success(
    value: object, expected: bool | None, flag: str,
) -> None:
    result = receipt(
        outcome="timeout",
        transport_metrics={flag: value, "compact_repair": 0},
    )
    assert result["request"][flag] is expected
    assert result["request"]["compact_repair"] is False
    assert result["outcome"] == "timeout"
    assert result["ollama"]["done"] is None
    assert "private" not in json.dumps(result, allow_nan=False)


def test_independent_attempts_do_not_reuse_success_metrics_after_timeout() -> None:
    first_id, second_id = new_model_attempt_id(), new_model_attempt_id()
    assert first_id != second_id
    first = receipt(
        attempt_id=first_id,
        terminal_envelope={"done": True, "done_reason": "stop", "eval_count": 99},
    )
    second = receipt(attempt_id=second_id, outcome="timeout")
    assert first["ollama"]["eval_count"] == 99
    assert second["ollama"]["eval_count"] is None
    assert first["job_id"] == second["job_id"] == _JOB


def test_receipt_does_not_mutate_transport_or_terminal_inputs() -> None:
    metrics = {"chunks": 2, "output_token_limit": 512, "compact_repair": 1}
    envelope = {"done": True, "done_reason": "stop", "eval_count": 10, "message": {"a": 1}}
    before = copy.deepcopy((metrics, envelope))
    result = receipt(transport_metrics=metrics, terminal_envelope=envelope)
    assert (metrics, envelope) == before
    assert result["request"]["compact_repair"] is True
    assert result["request"]["output_token_limit"] == 512


def test_timestamps_are_utc_and_unknown_naive_dates_are_rejected() -> None:
    result = receipt(started_at="2026-10-01T12:44:40-04:00", finished_at=None)
    assert result["started_at"] == "2026-10-01T16:44:40+00:00"
    assert datetime.fromisoformat(result["finished_at"]).tzinfo == UTC
    assert receipt(started_at="2026-10-01T16:44:40")["started_at"] is None
    assert datetime.fromisoformat(utc_timestamp()).tzinfo == UTC
    assert re.fullmatch(r"model_attempt_[0-9a-f]{32}", new_model_attempt_id())


@pytest.mark.parametrize("value", [True, -1, "240", float("nan"), float("inf")])
def test_invalid_timeout_settings_are_not_serialized(value: object) -> None:
    result = receipt(wall_timeout_seconds=value, read_timeout_seconds=value)
    assert result["request"]["wall_timeout_seconds"] is None
    assert result["request"]["read_timeout_seconds"] is None
    json.dumps(result, allow_nan=False)
