from __future__ import annotations

import copy
import re
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
_SECRET_TEXT = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}|"
    r"\b(?:redis|rediss|https?)://[^/@\s:]+:[^/@\s]+@"
)


class EventPrivacyError(ValueError):
    pass


def _forbidden_key(key: str, *, board_payload: bool = False) -> bool:
    folded = key.casefold()
    normalized = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")
    compact = normalized.replace("_", "")

    def includes(part: str) -> bool:
        return part in normalized or part.replace("_", "") in compact

    return any(includes(part) for part in _FORBIDDEN_KEY_PARTS) or (
        board_payload
        and (
            normalized in _BOARD_FORBIDDEN_KEYS
            or any(includes(part) for part in _BOARD_FORBIDDEN_KEY_PARTS)
        )
    )


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
            if _forbidden_key(key, board_payload=board_payload):
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
    if isinstance(value, str) and _SECRET_TEXT.search(value):
        raise EventPrivacyError(f"shared event contains credential-like text at {path}")


def assert_safe_shared_payload(payload: dict[str, Any]) -> None:
    """Reject sensitive values before they enter outbox/shared board state."""

    if not isinstance(payload, dict):
        raise TypeError("shared event payload must be an object")
    _walk_shared(payload, board_payload=True)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize(nested)
            for key, nested in value.items()
            if isinstance(key, str) and not _forbidden_key(key)
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub("<redacted credential>", value)
    return copy.deepcopy(value)


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
    elif event_type == "task.updated":
        projected = dict(payload)
        projected.pop("error_json", None)
    else:
        projected = dict(payload)

    sanitized = _sanitize(projected)
    if not isinstance(sanitized, dict):  # pragma: no cover - dict input is preserved
        raise EventPrivacyError("WebSocket event projection failed")
    _walk_shared(sanitized)
    return {"type": event_type, "payload": sanitized}
