"""Arguments and execution evidence for the separately approved Mac tools lane."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SWIFT_SKILLS = frozenset({"code.swift.build", "code.swift.test"})
_DIGEST = re.compile(r"[a-f0-9]{64}")


def swift_argument_schema() -> dict[str, Any]:
    def branch(kind: str, extra: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "kind": {"type": "string", "const": kind},
            "source_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            **extra,
        }
        return {
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        }

    text = {"type": "string", "minLength": 1, "maxLength": 100}
    return {
        "anyOf": [
            branch("swiftpm", {}),
            branch("xcode", {"project": text, "scheme": text, "destination": text}),
        ]
    }


def validate_swift_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Swift operation arguments must be an object")  # noqa: TRY004 - contract validation
    kind = payload.get("kind")
    fields = {"kind", "source_sha256"}
    if kind == "xcode":
        fields |= {"project", "scheme", "destination"}
    if not isinstance(kind, str) or kind not in {"swiftpm", "xcode"} or set(payload) != fields:
        raise ValueError("Swift operation fields invalid")
    digest = payload["source_sha256"]
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("Swift source digest required")
    if kind == "xcode":
        patterns = {
            "project": r"[A-Za-z0-9_ .-]+\.(?:xcodeproj|xcworkspace)",
            "scheme": r"[A-Za-z0-9_][A-Za-z0-9_ .-]{0,99}",
            "destination": r"[A-Za-z0-9_-]{1,64}",
        }
        for key, pattern in patterns.items():
            if not isinstance(payload[key], str) or not re.fullmatch(pattern, payload[key]):
                raise ValueError("invalid Swift target")
    return dict(payload)


def valid_swift_receipt(skill: str, receipt: object, payload: object | None = None) -> bool:
    """Require successful bounded evidence; a hash is identity, not source approval."""
    fields = {
        "operation",
        "kind",
        "status",
        "exit_code",
        "source_sha256",
        "request_sha256",
        "source_unchanged",
        "tests_executed",
        "test_evidence_format",
        "test_failures",
        "duration_ms",
        "artifact_directory",
        "report_error",
    }
    if isinstance(payload, dict) and "project_revision" in payload:
        fields.add("project_revision")
    if skill not in SWIFT_SKILLS or not isinstance(receipt, dict) or set(receipt) != fields:
        return False
    operation = skill.rsplit(".", 1)[1]
    kind = receipt["kind"]
    if (
        receipt["operation"] != operation
        or not isinstance(kind, str)
        or kind not in {"swiftpm", "xcode"}
        or receipt["status"] != "passed"
        or receipt["source_unchanged"] is not True
        or type(receipt["exit_code"]) is not int
        or receipt["exit_code"] != 0
        or receipt["report_error"] is not None
        or not isinstance(receipt["source_sha256"], str)
        or not _DIGEST.fullmatch(receipt["source_sha256"])
        or not isinstance(receipt["request_sha256"], str)
        or not _DIGEST.fullmatch(receipt["request_sha256"])
        or not isinstance(receipt["artifact_directory"], str)
        or not re.fullmatch(r"\.swarmer-swift-runs/[a-f0-9]{32}", receipt["artifact_directory"])
    ):
        return False
    for field in ("tests_executed", "test_failures", "duration_ms"):
        if type(receipt[field]) is not int or not 0 <= receipt[field] <= 120_000:
            return False
    if receipt["test_failures"] or (operation == "test" and receipt["tests_executed"] == 0):
        return False
    if operation == "build" and receipt["tests_executed"] != 0:
        return False
    if receipt["test_evidence_format"] != (
        "swiftpm_xunit" if kind == "swiftpm" else "xcresult_summary"
    ):
        return False
    if payload is not None:
        try:
            expected = validate_swift_project_payload(payload)
        except ValueError:
            return False
        if receipt["source_sha256"] != expected["source_sha256"] or kind != expected["kind"]:
            return False
        if receipt.get("project_revision") != expected.get("project_revision"):
            return False
        request_digest = hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if receipt["request_sha256"] != request_digest:
            return False
    return True


def validate_swift_project_payload(payload: object) -> dict[str, Any]:
    """Reserved controller payload; never exposed in model argument schemas."""
    if not isinstance(payload, dict):
        raise ValueError("Swift operation arguments must be an object")  # noqa: TRY004
    if "project_revision" not in payload:
        return validate_swift_payload(payload)
    reference = payload["project_revision"]
    if not isinstance(reference, dict) or set(reference) != {
        "validation_id",
        "project_id",
        "revision_id",
        "sha256",
    }:
        raise ValueError("invalid Swift project reference")
    for key in ("validation_id", "project_id", "revision_id"):
        if not isinstance(reference[key], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", reference[key]
        ):
            raise ValueError("invalid Swift project identifier")
    if not isinstance(reference["sha256"], str) or not _DIGEST.fullmatch(reference["sha256"]):
        raise ValueError("invalid Swift project digest")
    target = validate_swift_payload({k: v for k, v in payload.items() if k != "project_revision"})
    return {**target, "project_revision": dict(reference)}
