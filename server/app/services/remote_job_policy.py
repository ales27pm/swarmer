from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from app.models import CAPABILITY_ARGUMENT_MODELS
from app.services.agent_card import SUPPORTED_AGENT_SKILLS

MAX_QUERY_CHARACTERS = 2_000
MAX_REVIEW_PATHS = 50
MAX_PATH_CHARACTERS = 500
MAX_CONTEXT_LINES = 20
FULL_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_REVIEW_PROTECTED_NAMES = frozenset(
    {".git", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials"}
)


class RemoteJobPolicyError(ValueError):
    """A remote job is outside the bounded worker payload policy."""


def _exact_fields(payload: dict[str, Any], allowed: frozenset[str]) -> None:
    if set(payload) - allowed:
        raise RemoteJobPolicyError("remote job payload contains unsupported fields")


def _protected_review_name(name: str) -> bool:
    folded = name.casefold()
    return bool(
        folded in _REVIEW_PROTECTED_NAMES
        or folded.startswith(".env")
        or "secret" in folded
        or "token" in folded
        or "credential" in folded
        or "password" in folded
        or "passwd" in folded
        or folded.endswith((".pem", ".key"))
    )


def _protected_workspace_name(name: str) -> bool:
    folded = name.casefold()
    return bool(
        folded.startswith(".")
        or folded in {"id_rsa", "id_ed25519"}
        or "secret" in folded
        or "token" in folded
        or "credential" in folded
        or "password" in folded
        or "passwd" in folded
        or folded.endswith((".pem", ".key"))
    )


def _relative_path(value: object, *, workspace: bool, allow_root: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_PATH_CHARACTERS
        or "\0" in value
        or "\\" in value
        or "://" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise RemoteJobPolicyError("remote job path is invalid")
    if value == ".":
        if allow_root:
            return value
        raise RemoteJobPolicyError("remote job requires a file path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise RemoteJobPolicyError("remote job path escapes its configured root")
    if workspace:
        if any(_protected_workspace_name(part) for part in path.parts):
            raise RemoteJobPolicyError("remote job path is protected")
    elif any(_protected_review_name(part) for part in path.parts):
        raise RemoteJobPolicyError("remote job path is protected")
    return path.as_posix()


def _capability_request(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"capability_name", "arguments"}:
        raise RemoteJobPolicyError("capability metadata is invalid")
    capability_name = raw.get("capability_name")
    arguments = raw.get("arguments")
    if not isinstance(capability_name, str) or not isinstance(arguments, dict):
        raise RemoteJobPolicyError("capability metadata is invalid")
    model = CAPABILITY_ARGUMENT_MODELS.get(capability_name)
    if model is None:
        raise RemoteJobPolicyError("capability is not supported")
    try:
        normalized_arguments = model.model_validate(arguments).model_dump(
            mode="json", exclude_none=True
        )
    except ValidationError as exc:
        raise RemoteJobPolicyError("capability arguments are invalid") from exc
    return {"capability_name": capability_name, "arguments": normalized_arguments}


def _workspace_payload(skill: str, payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(payload, frozenset({"path", "capability_request"}))
    if skill == "workspace.read_text" and "path" not in payload:
        raise RemoteJobPolicyError("workspace.read_text requires path")
    path = _relative_path(
        payload.get("path", "."),
        workspace=True,
        allow_root=skill == "workspace.list_dir",
    )
    normalized: dict[str, Any] = {"path": path}
    if "capability_request" in payload:
        normalized["capability_request"] = _capability_request(payload["capability_request"])
    return normalized


def _research_payload(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(payload, frozenset({"query", "max_results"}))
    query = payload.get("query")
    if not isinstance(query, str):
        raise RemoteJobPolicyError("research query must be a string")
    query = query.strip()
    if not query or len(query) > MAX_QUERY_CHARACTERS:
        raise RemoteJobPolicyError("research query is empty or too long")
    max_results = payload.get("max_results", 5)
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or not 1 <= max_results <= 10
    ):
        raise RemoteJobPolicyError("max_results must be an integer between 1 and 10")
    return {"query": query, "max_results": max_results}


def _review_paths(payload: dict[str, Any], *, required: bool = False) -> list[str]:
    raw = payload.get("paths", [])
    if not isinstance(raw, list) or len(raw) > MAX_REVIEW_PATHS:
        raise RemoteJobPolicyError("review paths must be a bounded array")
    if required and not raw:
        raise RemoteJobPolicyError("static analysis requires at least one Python file")
    normalized: list[str] = []
    for value in raw:
        path = _relative_path(value, workspace=False, allow_root=True)
        if required and PurePosixPath(path).suffix.casefold() != ".py":
            raise RemoteJobPolicyError("static analysis accepts only Python files")
        if path not in normalized:
            normalized.append(path)
    return normalized


def _context_lines(payload: dict[str, Any]) -> int:
    value = payload.get("context_lines", 3)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_CONTEXT_LINES:
        raise RemoteJobPolicyError("context_lines must be an integer between 0 and 20")
    return value


def _review_payload(skill: str, payload: dict[str, Any]) -> dict[str, Any]:
    if skill == "code_review.git_status":
        _exact_fields(payload, frozenset())
        return {}
    if skill == "code_review.git_diff":
        _exact_fields(payload, frozenset({"paths", "staged", "context_lines"}))
        staged = payload.get("staged", False)
        if not isinstance(staged, bool):
            raise RemoteJobPolicyError("staged must be a boolean")
        return {
            "paths": _review_paths(payload),
            "staged": staged,
            "context_lines": _context_lines(payload),
        }
    if skill == "code_review.git_show":
        _exact_fields(payload, frozenset({"paths", "revision", "context_lines"}))
        revision = payload.get("revision")
        if not isinstance(revision, str) or not (
            revision == "HEAD" or FULL_OBJECT_ID_RE.fullmatch(revision)
        ):
            raise RemoteJobPolicyError("revision must be HEAD or a full object id")
        return {
            "revision": revision if revision == "HEAD" else revision.lower(),
            "paths": _review_paths(payload),
            "context_lines": _context_lines(payload),
        }
    _exact_fields(payload, frozenset({"paths"}))
    return {"paths": _review_paths(payload, required=True)}


def validate_remote_job(required_skill: str, payload: object) -> dict[str, Any]:
    """Return the canonical bounded payload accepted by one policy-bound worker."""

    if not isinstance(required_skill, str) or required_skill not in SUPPORTED_AGENT_SKILLS:
        raise RemoteJobPolicyError("remote job requires an unsupported or privileged skill")
    if not isinstance(payload, dict):
        raise RemoteJobPolicyError("remote job payload must be an object")
    if required_skill in {"workspace.list_dir", "workspace.read_text"}:
        return _workspace_payload(required_skill, payload)
    if required_skill == "research.query":
        return _research_payload(payload)
    return _review_payload(required_skill, payload)
