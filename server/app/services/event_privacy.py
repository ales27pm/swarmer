from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

_FORBIDDEN_KEY_PARTS = (
    "access_key",
    "api_key",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "password",
    "private_key",
    "session_id",
    "secret",
    "token",
    "grant_id",
    "grant_digest",
    "lease_token",
    "token_hash",
    "native_result",
    "result_json",
    "arguments_json",
)
_FORBIDDEN_KEY_SEGMENTS = frozenset({"auth", "grant"})
_BOARD_BOOLEAN_MARKERS = frozenset({"arguments_redacted", "result_redacted"})
_BOARD_FORBIDDEN_KEY_PARTS = (
    "argument",
    "body",
    "contact",
    "coordinate",
    "email",
    "latitude",
    "longitude",
    "message_body",
    "native_payload",
    "output",
    "path",
    "phone",
    "result",
    "sms",
)
_BOARD_FORBIDDEN_KEYS = frozenset(
    {
        "address",
        "addresses",
        "bcc",
        "cc",
        "lat",
        "lng",
        "recipient",
        "recipients",
        "text",
        "to",
    }
)
_BOARD_NATURAL_LANGUAGE_KEYS = frozenset(
    {
        "content",
        "description",
        "input",
        "message",
        "note",
        "notes",
        "prompt",
        "summary",
        "title",
    }
)
_TASK_NOTIFICATION_FIELDS = (
    "id",
    "task_id",
    "conversation_id",
    "status",
    "created_at",
    "updated_at",
    "completed_at",
)
_MESSAGE_NOTIFICATION_FIELDS = (
    "id",
    "message_id",
    "conversation_id",
    "task_id",
    "role",
    "agent_id",
    "status",
    "created_at",
    "updated_at",
)
_ORCHESTRATOR_NOTIFICATION_FIELDS = (
    "task_id",
    "planner_source",
    "status",
    "created_at",
    "updated_at",
)
_APPROVAL_NOTIFICATION_FIELDS = (
    "id",
    "approval_id",
    "task_id",
    "tool_call_id",
    "status",
    "created_at",
    "expires_at",
    "decided_at",
)
_GOAL_NOTIFICATION_FIELDS = (
    "id",
    "root_task_id",
    "status",
    "current_phase",
    "updated_at",
    "completed_at",
)
_PLAN_NODE_NOTIFICATION_FIELDS = (
    "id",
    "goal_run_id",
    "status",
    "updated_at",
    "completed_at",
)
_GOAL_RESULT_NOTIFICATION_FIELDS = (
    "goal_run_id",
    "root_task_id",
    "status",
    "completed_at",
)
_CONFUSABLES = str.maketrans(
    {
        # Common Cyrillic and Greek homoglyphs used to evade ASCII key checks.
        "\u0430": "a",
        "\u0435": "e",
        "\u0456": "i",
        "\u0458": "j",
        "\u043a": "k",
        "\u043c": "m",
        "\u043e": "o",
        "\u0440": "p",
        "\u0441": "c",
        "\u0455": "s",
        "\u0442": "t",
        "\u0445": "x",
        "\u0443": "y",
        "\u0391": "a",
        "\u0392": "b",
        "\u0395": "e",
        "\u0397": "h",
        "\u0399": "i",
        "\u039a": "k",
        "\u039c": "m",
        "\u039d": "n",
        "\u039f": "o",
        "\u03a1": "p",
        "\u03a4": "t",
        "\u03a7": "x",
        "\u03b1": "a",
        "\u03b2": "b",
        "\u03b5": "e",
        "\u03b9": "i",
        "\u03ba": "k",
        "\u03bc": "m",
        "\u03bd": "v",
        "\u03bf": "o",
        "\u03c1": "p",
        "\u03c4": "t",
        "\u03c5": "y",
        "\u03c7": "x",
    }
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SECRET_TEXT = re.compile(
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"\b(?:redis|rediss|https?)://[^/@\s:]+:[^/@\s]+@|"
    r"\b(?:authorization|auth|api[\W_]*key|private[\W_]*key|password|secret|"
    r"token|grant|claim[\W_]*token|lease[\W_]*token)\s*[:=]\s*"
    r"(?:(?:bearer|basic)\s+)?[^\s,;]{8,}|"
    r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b|"
    r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----"
)


class EventPrivacyError(ValueError):
    pass


def _fold_confusables(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _CAMEL_BOUNDARY.sub("_", normalized)
    normalized = normalized.casefold().translate(_CONFUSABLES)
    normalized = unicodedata.normalize("NFKD", normalized)
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cf", "Mn", "Me"}
    )


def _normalized_key(key: str) -> tuple[str, str]:
    folded = _fold_confusables(key)
    normalized = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")
    return normalized, normalized.replace("_", "")


def _forbidden_key(
    key: str,
    *,
    board_payload: bool = False,
    natural_language_payload: bool = False,
) -> bool:
    # Durable/shared envelopes use an ASCII metadata vocabulary. Rejecting
    # non-ASCII keys fail-closes the broad Unicode-confusable class instead of
    # relying only on a necessarily incomplete homoglyph table.
    if not key.isascii():
        return True
    normalized, compact = _normalized_key(key)
    segments = frozenset(normalized.split("_"))

    def includes(part: str) -> bool:
        return part in normalized or part.replace("_", "") in compact

    return (
        bool(segments & _FORBIDDEN_KEY_SEGMENTS)
        or any(includes(part) for part in _FORBIDDEN_KEY_PARTS)
        or (
            board_payload
            and (
                normalized in _BOARD_FORBIDDEN_KEYS
                or any(includes(part) for part in _BOARD_FORBIDDEN_KEY_PARTS)
            )
        )
        or (
            natural_language_payload
            and bool(segments & _BOARD_NATURAL_LANGUAGE_KEYS)
            and not normalized.endswith("_id")
        )
    )


def _contains_secret_text(value: str) -> bool:
    return _SECRET_TEXT.search(_fold_confusables(value)) is not None


def _walk_shared(
    value: Any,
    *,
    path: str = "payload",
    board_payload: bool = False,
) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise EventPrivacyError("shared event keys must be strings")
            if board_payload and key.casefold() in _BOARD_BOOLEAN_MARKERS:
                if nested is not True:
                    raise EventPrivacyError(f"shared event redaction marker is invalid at {path}")
                continue
            if _forbidden_key(
                key,
                board_payload=board_payload,
                natural_language_payload=board_payload,
            ):
                raise EventPrivacyError(f"shared event field is forbidden at {path}")
            _walk_shared(
                nested,
                path=f"{path}.{key}",
                board_payload=board_payload,
            )
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _walk_shared(
                nested,
                path=f"{path}[{index}]",
                board_payload=board_payload,
            )
        return
    if isinstance(value, str) and _contains_secret_text(value):
        raise EventPrivacyError(f"shared event contains credential-like text at {path}")


def assert_safe_shared_payload(payload: dict[str, Any]) -> None:
    """Reject sensitive values before they enter outbox/shared board state."""

    if not isinstance(payload, dict):
        raise TypeError("shared event payload must be an object")
    _walk_shared(payload, board_payload=True)


def _sanitize(value: Any, *, shared_transport: bool = False) -> Any:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise EventPrivacyError("WebSocket event payload keys must be strings")
        sanitized: dict[str, Any] = {}
        for key, nested in value.items():
            if key in _BOARD_BOOLEAN_MARKERS:
                if type(nested) is not bool:
                    raise EventPrivacyError("WebSocket event redaction marker must be boolean")
                sanitized[key] = nested
                continue
            if key == "result" and nested == {"result_redacted": True}:
                sanitized[key] = {"result_redacted": True}
                continue
            if _forbidden_key(key, board_payload=shared_transport):
                continue
            sanitized[key] = _sanitize(nested, shared_transport=shared_transport)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item, shared_transport=shared_transport) for item in value]
    if isinstance(value, str) and _contains_secret_text(value):
        return "<redacted sensitive text>"
    return copy.deepcopy(value)


def _refetch_notification(
    payload: dict[str, Any],
    allowed_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Project a content-bearing resource into an invalidation notification."""

    projected = {key: payload[key] for key in allowed_fields if key in payload}
    projected["refetch_required"] = True
    return projected


def safe_websocket_event(event: dict[str, Any]) -> dict[str, Any]:
    """Central fail-closed projection for the paired-device WebSocket channel."""

    if set(event) != {"type", "payload"}:
        raise EventPrivacyError("WebSocket event must contain only type and payload")
    event_type = event.get("type")
    payload = event.get("payload")
    if not isinstance(event_type, str) or not event_type or not isinstance(payload, dict):
        raise EventPrivacyError("WebSocket event shape is invalid")

    if event_type.startswith("iphone.capability."):
        allowed = {"request_id", "capability_name", "expires_at", "status", "preview"}
        projected = {key: payload[key] for key in allowed if key in payload}
    elif event_type.startswith("tool."):
        projected = dict(payload)
        if projected.get("result") is not None:
            projected["result"] = {"result_redacted": True}
        projected.pop("error_json", None)
    elif event_type == "memory.updated":
        allowed = {"id", "pinned", "updated_at", "scope", "kind"}
        projected = {key: payload[key] for key in allowed if key in payload}
    elif event_type.startswith("task."):
        projected = _refetch_notification(payload, _TASK_NOTIFICATION_FIELDS)
    elif event_type.startswith("message."):
        projected = _refetch_notification(payload, _MESSAGE_NOTIFICATION_FIELDS)
    elif event_type == "orchestrator.proposed":
        projected = _refetch_notification(payload, _ORCHESTRATOR_NOTIFICATION_FIELDS)
    elif event_type.startswith("approval."):
        projected = _refetch_notification(payload, _APPROVAL_NOTIFICATION_FIELDS)
    elif event_type == "goal.updated":
        projected = _refetch_notification(payload, _GOAL_NOTIFICATION_FIELDS)
    elif event_type == "plan.node.updated":
        projected = _refetch_notification(payload, _PLAN_NODE_NOTIFICATION_FIELDS)
    elif event_type == "goal.result.updated":
        projected = _refetch_notification(payload, _GOAL_RESULT_NOTIFICATION_FIELDS)
    else:
        projected = dict(payload)

    sanitized = _sanitize(projected, shared_transport=True)
    if not isinstance(sanitized, dict):  # pragma: no cover - dict input is preserved
        raise EventPrivacyError("WebSocket event projection failed")
    _walk_shared(sanitized)
    return {"type": event_type, "payload": sanitized}
