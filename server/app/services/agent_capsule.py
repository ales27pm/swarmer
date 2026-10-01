"""Strict, side-effect-free validation of the shared source-backed prompt capsule.

Project-guide hashes identify original accepted bytes; their displayed content may
be redacted. Historical observations never establish current correctness or authority.
The standalone worker mirror must remain byte-identical to this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any

MAX_CAPSULE_BYTES = 32_000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_SHA = re.compile(r"[0-9a-f]{64}")


def _require(ok: object) -> None:
    if not ok:
        raise ValueError("invalid agent instruction capsule")


def _object(value: object, keys: set[str]) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == keys)
    assert isinstance(value, dict)
    return value


def _text(value: object, *, nonempty: bool = True) -> str:
    _require(isinstance(value, str))
    assert isinstance(value, str)
    _require((bool(value.strip()) or not nonempty) and "\0" not in value)
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("invalid agent instruction capsule") from exc
    return value


def _identifier(value: object, *, nullable: bool = False) -> None:
    _require((value is None and nullable) or (isinstance(value, str) and _ID.fullmatch(value)))


def _sha(value: object, *, nullable: bool = False) -> None:
    _require((value is None and nullable) or (isinstance(value, str) and _SHA.fullmatch(value)))


def _integer(value: object, minimum: int = 0) -> None:
    _require(type(value) is int and minimum <= value <= 1_000_000_000)


def _list(value: object, maximum: int) -> list[Any]:
    _require(isinstance(value, list) and len(value) <= maximum)
    assert isinstance(value, list)
    return value


def validate_agent_capsule(value: object) -> dict[str, Any]:
    """Return an exact deep copy; reject oversize/unknown data without truncation."""
    base = {"version", "fingerprint", "requirements", "base_revision_id"}
    optional = {"operating_guidance", "project_guidance", "experiences"}
    _require(isinstance(value, dict) and base <= set(value) <= base | optional)
    assert isinstance(value, dict)
    _integer(value["version"], 1)
    _sha(value["fingerprint"])
    _identifier(value["base_revision_id"], nullable=True)
    source_ids: set[str] = set()
    for raw in _list(value["requirements"], 10_000):
        item = _object(raw, {"text", "source_id"})
        _text(item["text"])
        _identifier(item["source_id"])
        _require(item["source_id"] not in source_ids)
        source_ids.add(item["source_id"])
    if "operating_guidance" in value:
        guide = _object(value["operating_guidance"], {"path", "sha256", "content"})
        _require(guide["path"] == "AGENTS.md")
        content = _text(guide["content"])
        _require(len(content.encode()) <= 4_000)
        _sha(guide["sha256"])
        _require(hashlib.sha256(content.encode()).hexdigest() == guide["sha256"])
    paths: set[str] = set()
    for raw in _list(value.get("project_guidance", []), 80):
        guide = _object(raw, {"path", "scope", "source_revision_id", "sha256", "content"})
        path = _text(guide["path"])
        parts = path.split("/")
        _require(
            len(path) <= 1_000
            and "\\" not in path
            and all(ord(character) >= 32 and ord(character) != 127 for character in path)
            and all(p not in {"", ".", ".."} for p in parts)
        )
        _require(not PurePosixPath(path).is_absolute() and parts[-1].casefold() == "agents.md")
        scope = "/".join(parts[:-1]) + ("/" if len(parts) > 1 else "")
        _require(guide["scope"] == scope and path.casefold() not in paths)
        paths.add(path.casefold())
        _identifier(guide["source_revision_id"])
        _sha(guide["sha256"])
        _text(guide["content"], nonempty=False)
    if "experiences" in value:
        experiences = _object(value["experiences"], {"items", "omitted_count"})
        _integer(experiences["omitted_count"])
        seen: set[str] = set()
        for raw in _list(experiences["items"], 6):
            item = _object(
                raw,
                {
                    "source_id",
                    "worker_job_id",
                    "required_skill",
                    "outcome",
                    "observation_kind",
                    "source_revision_id",
                    "source_sha256",
                    "summary",
                    "content_trust",
                    "applicability",
                },
            )
            for key in ("source_id", "worker_job_id", "required_skill"):
                _identifier(item[key])
            _require(item["source_id"] not in seen)
            seen.add(item["source_id"])
            _require(
                isinstance(item["outcome"], str) and item["outcome"] in {"completed", "failed"}
            )
            _require(
                isinstance(item["observation_kind"], str)
                and item["observation_kind"]
                in {"accepted_result", "measured_failure", "reported_failure"}
            )
            _require(
                (item["outcome"] == "completed") == (item["observation_kind"] == "accepted_result")
            )
            _identifier(item["source_revision_id"], nullable=True)
            _sha(item["source_sha256"], nullable=True)
            _require((item["source_revision_id"] is None) == (item["source_sha256"] is None))
            _require(len(_text(item["summary"])) <= 600)
            _require(item["content_trust"] == "untrusted" and item["applicability"] == "historical")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        _require(len(encoded.encode()) <= MAX_CAPSULE_BYTES)
        result: dict[str, Any] = json.loads(encoded)
    except (TypeError, UnicodeError) as exc:
        raise ValueError("invalid agent instruction capsule") from exc
    return result


def required_capsule_identity(value: object) -> dict[str, Any] | None:
    """Identity for a repair: required instructions, excluding evolving observations."""
    if value is None:
        return None
    capsule = validate_agent_capsule(value)
    return {
        key: capsule[key]
        for key in ("requirements", "base_revision_id", "operating_guidance", "project_guidance")
        if key in capsule
    }
