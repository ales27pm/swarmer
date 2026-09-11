"""Strict project artifacts; these helpers never read or execute project code."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

SKILL = "code.build_project"
MAX_FILES = 80
MAX_FILE_BYTES = 64_000
MAX_PROJECT_BYTES = 1_000_000
MAX_CONTROL_BYTES = 4_000_000
MAX_PATCH_BYTES = 8_000
RUNTIMES = frozenset({"python", "node", "python_node"})
PAYLOAD_FIELDS = frozenset(
    {
        "objective",
        "conversation",
        "files",
        "plan",
        "checks",
        "iteration",
        "base_revision_id",
        "base_sha256",
    }
)
STEP_FIELDS = frozenset(
    {
        "action",
        "message",
        "plan",
        "edits",
        "deletions",
        "requested_checks",
        "run_instructions",
        "runtime",
    }
)
PROTECTED_PARTS = frozenset(
    {
        ".git",
        ".env",
        ".ssh",
        ".aws",
        ".azure",
        ".config",
        ".npmrc",
        ".pypirc",
        "id_rsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "auth.json",
        ".codex",
        ".venv",
        "node_modules",
    }
)


class ProjectError(ValueError):
    """A project operation cannot be accepted within the worker contract."""


def text_value(value: object, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ProjectError("project text is empty, invalid, or exceeds its limit")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ProjectError("project text contains forbidden control characters")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ProjectError("project text is not valid UTF-8") from exc
    return value


def path_value(value: object) -> str:
    value = text_value(value, 240)
    parts = value.split("/")
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.@\-]+", part)
        or part in {".", ".."}
        or part.casefold() in PROTECTED_PARTS
        or (
            part.casefold().startswith(".env")
            and part.casefold() not in {".env.example", ".env.sample", ".env.template"}
        )
        or part.casefold().endswith((".pem", ".key", ".p12", ".pfx"))
        for part in parts
    ):
        raise ProjectError("project path is not a permitted canonical relative path")
    return value


def files_value(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise ProjectError("project file count exceeds its limit")
    files: list[dict[str, str]] = []
    seen: set[str] = set()
    size = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise ProjectError("project file has invalid fields")
        path = path_value(item["path"])
        content = text_value(item["content"], MAX_FILE_BYTES, empty=True)
        content_size = len(content.encode("utf-8"))
        if content_size > MAX_FILE_BYTES:
            raise ProjectError("project file exceeds its UTF-8 byte limit")
        folded = path.casefold()
        if folded in seen or any(
            folded.startswith(other + "/") or other.startswith(folded + "/") for other in seen
        ):
            raise ProjectError("project paths collide")
        seen.add(folded)
        size += content_size
        files.append({"path": path, "content": content})
    if size > MAX_PROJECT_BYTES:
        raise ProjectError("project snapshot exceeds its UTF-8 byte limit")
    return sorted(files, key=lambda item: item["path"])


def plan_value(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise ProjectError("project plan exceeds its limit")
    return [text_value(item, 500) for item in value]


def command_value(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 24:
        raise ProjectError("project check command is invalid")
    return [text_value(item, 500) for item in value]


def checks_value(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 12:
        raise ProjectError("project check count exceeds its limit")
    checks = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "command",
            "status",
            "exit_code",
            "output",
            "duration_ms",
        }:
            raise ProjectError("project check receipt has invalid fields")
        status, code, duration = item["status"], item["exit_code"], item["duration_ms"]
        if status not in {"passed", "failed", "skipped"}:
            raise ProjectError("project check status is invalid")
        if code is not None and (type(code) is not int or not -255 <= code <= 255):
            raise ProjectError("project check exit code is invalid")
        if (status == "passed" and code != 0) or (status == "failed" and code == 0):
            raise ProjectError("project check status and exit code disagree")
        if type(duration) is not int or not 0 <= duration <= 900_000:
            raise ProjectError("project check duration is invalid")
        checks.append(
            {
                "command": command_value(item["command"]),
                "status": status,
                "exit_code": code,
                "output": text_value(item["output"], 8_000, empty=True),
                "duration_ms": duration,
            }
        )
    return checks


def parse_payload(job: dict[str, Any]) -> dict[str, Any]:
    if job.get("required_skill") != SKILL:
        raise ProjectError("unsupported project worker skill")
    value = job.get("payload")
    if not isinstance(value, dict) or set(value) - {"focus_paths", "memory"} != PAYLOAD_FIELDS:
        raise ProjectError("project job payload has invalid fields")
    objective = text_value(value["objective"], 4_000)
    conversation = value["conversation"]
    if not isinstance(conversation, list) or len(conversation) > 40:
        raise ProjectError("project conversation exceeds its limit")
    messages = []
    for message in conversation:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in {"user", "assistant"}
        ):
            raise ProjectError("project message is invalid")
        messages.append({"role": message["role"], "content": text_value(message["content"], 4_000)})
    iteration = value["iteration"]
    if type(iteration) is not int or not 1 <= iteration <= 100:
        raise ProjectError("project iteration is invalid")
    revision, sha = value["base_revision_id"], value["base_sha256"]
    if (revision is None) != (sha is None):
        raise ProjectError("project base identity is incomplete")
    if revision is not None and (
        not isinstance(revision, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", revision)
        or not isinstance(sha, str)
        or not re.fullmatch(r"[a-f0-9]{64}", sha)
    ):
        raise ProjectError("project base identity is invalid")
    files = files_value(value["files"])
    if sha is not None and sha != snapshot_sha(files):
        raise ProjectError("project base digest does not match the supplied snapshot")
    focus = focus_value(value.get("focus_paths", []))
    if any(path not in {item["path"] for item in files} for path in focus):
        raise ProjectError("project read focus references an absent file")
    return {
        "objective": objective,
        "conversation": messages,
        "files": files,
        "plan": plan_value(value["plan"]),
        "checks": checks_value(value["checks"]),
        "iteration": iteration,
        "base_revision_id": revision,
        "base_sha256": sha,
        "focus_paths": focus,
        "memory": memory_value(value.get("memory")),
    }


def memory_value(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"mode", "reason", "items"}:
        raise ProjectError("project memory has invalid fields")
    if value["mode"] not in {"semantic", "lexical"}:
        raise ProjectError("project memory mode is invalid")
    reason = text_value(value["reason"], 100)
    if not isinstance(value["items"], list) or len(value["items"]) > 4:
        raise ProjectError("project memory item count exceeds its limit")
    items = []
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {"id", "summary", "score", "source_id"}:
            raise ProjectError("project memory item has invalid fields")
        score = item["score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (float, int))
            or not math.isfinite(score)
            or not -1 <= score <= 1
        ):
            raise ProjectError("project memory score is not finite")
        if any(
            not isinstance(item[key], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item[key])
            for key in ("id", "source_id")
        ):
            raise ProjectError("project memory identity is invalid")
        items.append(
            {
                "id": text_value(item["id"], 200),
                "summary": text_value(item["summary"], 1_200),
                "score": float(score),
                "source_id": text_value(item["source_id"], 200),
            }
        )
    return {"mode": value["mode"], "reason": reason, "items": items}


def parse_step(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"focus_paths", "patches"} != STEP_FIELDS:
        raise ProjectError("model project step has invalid fields")
    if (
        value["action"] not in {"clarify", "continue", "complete"}
        or value["runtime"] not in RUNTIMES
    ):
        raise ProjectError("model project action or runtime is invalid")
    deletions, requested = value["deletions"], value["requested_checks"]
    if not isinstance(deletions, list) or len(deletions) > MAX_FILES:
        raise ProjectError("model project deletion count exceeds its limit")
    if not isinstance(requested, list) or len(requested) > 8:
        raise ProjectError("model requested check count exceeds its limit")
    edits = files_value(value["edits"])
    patches = patches_value(value.get("patches", []))
    deleted = [path_value(item) for item in deletions]
    if len({item.casefold() for item in deleted}) != len(deleted):
        raise ProjectError("model project deletions collide")
    if {item["path"].casefold() for item in edits} & {item.casefold() for item in deleted}:
        raise ProjectError("model both edits and deletes the same path")
    changed = {item["path"].casefold() for item in edits} | {item.casefold() for item in deleted}
    patched = {item["path"].casefold() for item in patches}
    if changed & patched:
        raise ProjectError("model patch conflicts with a replacement or deletion")
    if len(changed | patched) > 3:
        raise ProjectError("model batch exceeds three changed paths")
    if value["action"] == "clarify" and (edits or patches or deleted or requested):
        raise ProjectError("a clarification cannot modify or execute the project")
    focus = focus_value(value.get("focus_paths", []))
    if focus and (value["action"] != "continue" or edits or patches or deleted or requested):
        raise ProjectError("reading project files requires a continue step without edits or checks")
    return {
        "action": value["action"],
        "message": text_value(value["message"], 4_000),
        "plan": plan_value(value["plan"]),
        "edits": edits,
        "patches": patches,
        "deletions": deleted,
        "requested_checks": [command_value(item) for item in requested],
        "run_instructions": text_value(value["run_instructions"], 4_000, empty=True),
        "runtime": value["runtime"],
        "focus_paths": focus,
    }


def patches_value(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 8:
        raise ProjectError("model patch count exceeds its limit")
    patches = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "old", "new"}:
            raise ProjectError("model patch has invalid fields")
        old = text_value(item["old"], MAX_PATCH_BYTES)
        new = text_value(item["new"], MAX_PATCH_BYTES, empty=True)
        if old == new or any(len(text.encode("utf-8")) > MAX_PATCH_BYTES for text in (old, new)):
            raise ProjectError("model patch is unchanged or exceeds its byte limit")
        patches.append({"path": path_value(item["path"]), "old": old, "new": new})
    return patches


def focus_value(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 8:
        raise ProjectError("project read focus exceeds its limit")
    paths = [path_value(item) for item in value]
    if len(set(paths)) != len(paths):
        raise ProjectError("project read focus contains duplicate paths")
    return paths


def merge_files(previous: list[dict[str, str]], step: dict[str, Any]) -> list[dict[str, str]]:
    step = parse_step(step)
    merged = {item["path"]: item["content"] for item in previous}
    changes: dict[str, list[tuple[int, int, str]]] = {}
    for patch in step["patches"]:
        path, old = patch["path"], patch["old"]
        content = merged.get(path)
        if content is None or content.count(old) != 1:
            raise ProjectError("model patch must match exactly once in the current base file")
        start = content.index(old)
        end = start + len(old)
        spans = changes.setdefault(path, [])
        if any(start < other_end and other_start < end for other_start, other_end, _ in spans):
            raise ProjectError("model patches overlap in the current base file")
        spans.append((start, end, patch["new"]))
    for path, spans in changes.items():
        for start, end, replacement in sorted(spans, reverse=True):
            merged[path] = merged[path][:start] + replacement + merged[path][end:]
    for path in step["deletions"]:
        if path not in merged:
            raise ProjectError("model deletes a file absent from the base snapshot")
        del merged[path]
    for edit in step["edits"]:
        merged[edit["path"]] = edit["content"]
    return files_value([{"path": path, "content": content} for path, content in merged.items()])


def snapshot_sha(files: list[dict[str, str]]) -> str:
    raw = json.dumps(
        files_value(files), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
