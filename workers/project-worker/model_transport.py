"""Content-free receipts for the existing project model transport.

This module does not send requests, consume streams, retry or decide whether a
project change is accepted. Call the builder once when an attempt finishes,
using the counters already collected by the transport. Ollama terminal counters
are evidence only when a terminal envelope was actually received.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

_MAX_COUNTER = (1 << 63) - 1
_OUTCOMES = frozenset({"success", "incomplete", "timeout", "cancelled", "error"})
_FAILURE_CATEGORIES = frozenset(
    {
        "wall_timeout",
        "read_timeout",
        "timeout",
        "connection_error",
        "configuration_error",
        "http_error",
        "unavailable",
        "invalid_response",
        "incomplete_response",
        "response_limit",
        "cancelled",
        "unknown",
    }
)
_DONE_REASONS = frozenset({"stop", "length", "load", "unload"})
_TERMINAL_COUNTERS = (
    "prompt_eval_count",
    "eval_count",
    "load_duration",
    "prompt_eval_duration",
    "eval_duration",
    "total_duration",
)


def new_model_attempt_id() -> str:
    """Create an identifier independent of prompt contents and credentials."""
    return "model_attempt_" + uuid4().hex


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: object, prefix: str) -> str | None:
    if isinstance(value, str) and re.fullmatch(prefix + r"[A-Za-z0-9_-]{1,160}", value):
        return value
    return None


def _counter(value: object) -> int | None:
    # bool is an int subclass; it is not a measured token/byte/time counter.
    return value if type(value) is int and 0 <= value <= _MAX_COUNTER else None


def _seconds(value: object) -> int | float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not 0 <= value <= _MAX_COUNTER or not math.isfinite(value):
        return None
    return value


def _flag(value: object) -> bool | None:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat()
    except (ValueError, OverflowError):
        return None


def _known_string(value: object, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def build_model_transport_receipt(
    *,
    attempt_id: object,
    started_at: object,
    outcome: str,
    goal_id: object = None,
    job_id: object = None,
    finished_at: object = None,
    context_tokens: object = None,
    prompt_max_bytes: object = None,
    wall_timeout_seconds: object = None,
    read_timeout_seconds: object = None,
    transport_metrics: Mapping[str, object] | None = None,
    terminal_envelope: Mapping[str, object] | None = None,
    failure_category: object = None,
) -> dict[str, Any]:
    """Build a bounded JSON-safe receipt without copying any model content.

    Missing or invalid measurements stay None, including all terminal counters
    after a timeout without a terminal envelope. ``outcome`` describes transport,
    not project success. The caller must not label semantic validation success
    solely from this receipt. Timing offsets come from the caller's monotonic
    clock; time before headers does not identify queue/load/prefill separately.

    ``finished_at=None`` records the current UTC time. Supplied timestamps must
    include an offset. This function never mutates inputs or inspects arbitrary
    nested event fields such as message, thinking, error, model or URL.
    """
    metrics = transport_metrics if isinstance(transport_metrics, Mapping) else {}
    envelope = terminal_envelope if isinstance(terminal_envelope, Mapping) else {}
    terminal = envelope if envelope.get("done") is True else {}
    return {
        "schema_version": 1,
        "scope": "model_transport",
        "goal_id": _identifier(goal_id, "goal_"),
        "job_id": _identifier(job_id, "job_"),
        "attempt_id": _identifier(attempt_id, "model_attempt_"),
        "outcome": _known_string(outcome, _OUTCOMES),
        "failure_category": _known_string(failure_category, _FAILURE_CATEGORIES),
        "started_at": _timestamp(started_at),
        "finished_at": _timestamp(utc_timestamp() if finished_at is None else finished_at),
        "request": {
            "context_tokens": _counter(context_tokens),
            "prompt_max_bytes": _counter(prompt_max_bytes),
            "prompt_bytes": _counter(metrics.get("prompt_bytes")),
            "schema_bytes": _counter(metrics.get("schema_bytes")),
            "output_token_limit": _counter(metrics.get("output_token_limit")),
            "compact_repair": _flag(metrics.get("compact_repair")),
            "compact_completion": _flag(metrics.get("compact_completion")),
            "wall_timeout_seconds": _seconds(wall_timeout_seconds),
            "read_timeout_seconds": _seconds(read_timeout_seconds),
        },
        "transport": {
            "chunks": _counter(metrics.get("chunks")),
            "response_bytes": _counter(metrics.get("response_bytes")),
            "content_bytes": _counter(metrics.get("content_bytes")),
            "terminal_received": _flag(metrics.get("terminal_received")),
        },
        "client_timing_ms": {
            key: _counter(metrics.get(key))
            for key in (
                "headers_ms",
                "first_chunk_ms",
                "first_content_ms",
                "elapsed_ms",
            )
        },
        "ollama": {
            "duration_unit": "ns",
            "done": True if terminal else None,
            "done_reason": _known_string(terminal.get("done_reason"), _DONE_REASONS),
            **{key: _counter(terminal.get(key)) for key in _TERMINAL_COUNTERS},
        },
    }
