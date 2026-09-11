from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from typing import Any

from app.services.audit_log import audit_event_hash
from app.services.project_contracts import ProjectWriteArguments


class ApprovalBindingError(ValueError):
    pass


PUBLIC_PROCESS_ERROR = "sandboxed process failed; detailed error retained locally"
PUBLIC_TOOL_SUMMARIES = {
    "workspace.list_dir": "List a workspace directory",
    "workspace.read_text": "Read a workspace file",
    "workspace.write_text": "Write text to a workspace file",
    "workspace.write_project": "Save a reviewed project revision",
    "process.run": "Run a sandboxed process",
}


@dataclass(frozen=True)
class ConsentContext:
    requester: dict[str, str] | None
    policy: dict[str, str] | None
    affected_data_summary: str
    audit_id: int | None
    valid: bool


def public_tool_summary(tool_name: str) -> str:
    """Return a fixed label that cannot echo model or executor-controlled text."""

    return PUBLIC_TOOL_SUMMARIES.get(tool_name, "Use a supported tool")


def canonical_action_digest(*, tool_call_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
    """Bind an approval to one exact JSON tool invocation without exposing its values."""

    if not tool_call_id or not tool_name or not isinstance(arguments, dict):
        raise ApprovalBindingError("approval binding requires a call id, tool name, and arguments")
    try:
        canonical = json.dumps(
            {
                "arguments": arguments,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ApprovalBindingError("tool arguments are not canonical JSON") from exc
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def binding_matches(
    stored_digest: object,
    *,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    if not isinstance(stored_digest, str):
        return False
    try:
        expected = canonical_action_digest(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
        )
    except ApprovalBindingError:
        return False
    return hmac.compare_digest(stored_digest, expected)


def safe_action_preview(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return consent-relevant structure while omitting content and arbitrary values."""

    if tool_name == "workspace.write_project":
        files = arguments.get("files")
        count = len(files) if isinstance(files, list) else 0
        try:
            manifest = ProjectWriteArguments.model_validate(arguments)
            target = manifest.path
            digest_details = [f"SHA-256: {manifest.sha256}"]
        except ValueError:
            target = "new generated project revision"
            digest_details = []
        return {
            "operation": "Save reviewed project revision",
            "target": target,
            "details": [
                f"{count} files; source content hidden",
                "Existing project revisions are preserved",
            ]
            + digest_details,
            "arguments_redacted": True,
        }

    if tool_name == "workspace.write_text":
        raw_content = arguments.get("content", "")
        content_bytes = len(raw_content.encode("utf-8")) if isinstance(raw_content, str) else None
        extra_fields = set(arguments) - {"path", "content"}
        details = [
            f"{content_bytes} UTF-8 bytes; content hidden"
            if content_bytes is not None
            else "content length unavailable; content hidden"
        ]
        if extra_fields:
            details.append(f"{len(extra_fields)} additional field(s) hidden")
        raw_target = arguments.get("path", "")
        return {
            "operation": "Write workspace text",
            "target": raw_target if isinstance(raw_target, str) else "<redacted>",
            "details": details,
            "arguments_redacted": True,
        }

    if tool_name == "process.run":
        raw_argv = arguments.get("argv")
        argv = raw_argv if isinstance(raw_argv, list) else []
        visible, hidden_count = _safe_process_argv(argv)
        raw_working_directory = arguments.get("cwd", ".")
        working_directory = (
            raw_working_directory if isinstance(raw_working_directory, str) else "<hidden>"
        )
        raw_timeout = arguments.get("timeout_seconds", 15)
        finite_timeout = (
            isinstance(raw_timeout, int)
            and not isinstance(raw_timeout, bool)
            or isinstance(raw_timeout, float)
            and math.isfinite(raw_timeout)
        )
        timeout = raw_timeout if finite_timeout else "hidden"
        details = [
            f"timeout {timeout} seconds",
            "the entire configured workspace is mounted read-write except protected paths",
            "cwd selects only the working directory and does not restrict workspace access",
            "network denied by sandbox policy",
        ]
        if hidden_count:
            details.append(f"{hidden_count} argument(s) hidden")
        extra_fields = set(arguments) - {"argv", "cwd", "timeout_seconds"}
        if extra_fields:
            details.append(f"{len(extra_fields)} additional field(s) hidden")
        return {
            "operation": "Run sandboxed process",
            "target": "entire non-protected configured workspace (read-write)",
            "working_directory": working_directory,
            "command": visible,
            "details": details,
            "arguments_redacted": bool(hidden_count or extra_fields),
        }

    return {
        "operation": tool_name,
        "details": ["Arguments hidden"],
        "arguments_redacted": True,
    }


def safe_affected_data_summary(tool_name: str, arguments: dict[str, Any]) -> str:
    """Describe data exposure conservatively without rendering arbitrary values."""

    if tool_name == "workspace.write_project":
        return (
            "Saves the reviewed files in a new immutable project revision; source content hidden."
        )
    if tool_name == "workspace.write_text":
        raw_content = arguments.get("content", "")
        raw_target = arguments.get("path", "")
        byte_count = (
            str(len(raw_content.encode("utf-8")))
            if isinstance(raw_content, str)
            else "an unknown number of"
        )
        target = raw_target if isinstance(raw_target, str) else "<redacted>"
        return f"Writes {byte_count} UTF-8 bytes to one workspace file: {target}; content hidden."
    if tool_name == "process.run":
        return (
            "Sandboxed process may read or modify the entire configured workspace except "
            "protected paths; cwd selects only its working directory "
            "and does not restrict workspace access; network denied."
        )
    return "Arguments and affected data remain hidden."


def public_tool_arguments(tool_name: str, arguments: object) -> dict[str, Any]:
    """Project raw executor arguments into the sole safe public representation."""

    if tool_name == "workspace.write_project":
        files = arguments.get("files") if isinstance(arguments, dict) else None
        return {
            "file_count": len(files) if isinstance(files, list) else 0,
            "arguments_redacted": True,
        }
    if not isinstance(arguments, dict):
        if tool_name in {"workspace.list_dir", "workspace.read_text"}:
            return {"path": "<redacted>"}
        if tool_name == "workspace.write_text":
            return {
                "path": "<redacted>",
                "content": "<redacted: 0 UTF-8 bytes>",
                "arguments_redacted": True,
            }
        if tool_name == "process.run":
            return {"argv": [], "argument_count": 0, "arguments_redacted": True}
        return {"arguments_redacted": True}

    if tool_name in {"workspace.list_dir", "workspace.read_text"}:
        raw_path = arguments.get("path", ".")
        return {"path": raw_path if isinstance(raw_path, str) else "<redacted>"}

    if tool_name == "workspace.write_text":
        raw_content = arguments.get("content", "")
        content = (
            f"<redacted: {len(raw_content.encode('utf-8'))} UTF-8 bytes>"
            if isinstance(raw_content, str)
            else "<redacted: unknown UTF-8 bytes>"
        )
        raw_path = arguments.get("path", "")
        return {
            "path": raw_path if isinstance(raw_path, str) else "<redacted>",
            "content": content,
            "arguments_redacted": True,
        }

    if tool_name == "process.run":
        raw_argv = arguments.get("argv")
        argv = raw_argv if isinstance(raw_argv, list) else []
        return {
            "argv": ["<redacted>"] if argv else [],
            "argument_count": len(argv),
            "arguments_redacted": True,
        }

    if tool_name == "none":
        return {}
    return {"arguments_redacted": True}


def public_tool_error(tool_name: str, error: object) -> str | None:
    if error is None:
        return None
    if tool_name == "process.run":
        return PUBLIC_PROCESS_ERROR
    return str(error)


def public_tool_result(tool_name: str, result: object) -> dict[str, Any] | None:
    """Project executor evidence without echoing process-controlled output."""

    if result is None:
        return None
    if not isinstance(result, dict):
        return {"output_redacted": True} if tool_name == "process.run" else None
    if tool_name != "process.run":
        return dict(result)

    projected: dict[str, Any] = {"output_redacted": True}
    returncode = result.get("returncode")
    if isinstance(returncode, int) and not isinstance(returncode, bool):
        projected["returncode"] = returncode
    for stream in ("stdout", "stderr"):
        value = result.get(stream)
        if isinstance(value, str):
            projected[stream] = f"<redacted: {len(value.encode('utf-8'))} UTF-8 bytes>"
    for flag in ("stdout_truncated", "stderr_truncated"):
        value = result.get(flag)
        if isinstance(value, bool):
            projected[flag] = value
    if result.get("sandbox") == "bubblewrap":
        projected["sandbox"] = "bubblewrap"
    if result.get("network") == "denied":
        projected["network"] = "denied"
    return projected


def public_tool_call(record: dict[str, Any]) -> dict[str, Any]:
    """Return a detached ToolCall projection safe for APIs, sync, and events."""

    projected = dict(record)
    tool_name = str(projected.get("tool_name", "unknown"))
    projected["summary"] = public_tool_summary(tool_name)
    projected["arguments"] = public_tool_arguments(tool_name, projected.get("arguments"))
    projected["result"] = public_tool_result(tool_name, projected.get("result"))
    projected["error"] = public_tool_error(tool_name, projected.get("error"))
    return projected


def consent_context_from_audit(
    *,
    approval_id: str,
    task_id: str,
    tool_call_id: str,
    action_digest: object,
    tool_name: str,
    arguments: dict[str, Any],
    request_audit_id: object,
    audit: dict[str, Any],
) -> ConsentContext:
    """Decode and validate server-authored consent context from its request audit event."""

    current_affected_summary = safe_affected_data_summary(tool_name, arguments)
    raw_payload_json = audit.get("payload_json")
    payload: dict[str, Any] | None = None
    if isinstance(raw_payload_json, str):
        try:
            decoded = json.loads(raw_payload_json)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            payload = decoded

    audit_id_value = audit.get("id")
    audit_id = (
        audit_id_value
        if isinstance(audit_id_value, int) and not isinstance(audit_id_value, bool)
        else None
    )
    actor_type = audit.get("actor_type")
    actor_id = audit.get("actor_id")
    requester_name = payload.get("requester_name") if payload else None
    requester = None
    if all(isinstance(value, str) and value for value in (actor_type, actor_id, requester_name)):
        requester = {
            "type": str(actor_type),
            "id": str(actor_id),
            "name": str(requester_name),
        }

    raw_policy = payload.get("policy") if payload else None
    policy = None
    if isinstance(raw_policy, dict):
        rule_id = raw_policy.get("rule_id")
        decision = raw_policy.get("decision")
        reason = raw_policy.get("reason")
        if all(isinstance(value, str) and value for value in (rule_id, decision, reason)):
            policy = {
                "rule_id": str(rule_id),
                "decision": str(decision),
                "reason": str(reason),
            }

    audited_affected_summary = payload.get("affected_data_summary") if payload else None
    affected_data_summary = current_affected_summary

    hash_matches = False
    try:
        if all(
            isinstance(audit.get(field), str)
            for field in ("trace_id", "event_type", "actor_type", "actor_id", "created_at", "hash")
        ) and (audit.get("prev_hash") is None or isinstance(audit.get("prev_hash"), str)):
            expected_hash = audit_event_hash(
                prev_hash=audit.get("prev_hash"),
                trace_id=str(audit["trace_id"]),
                event_type=str(audit["event_type"]),
                actor_type=str(audit["actor_type"]),
                actor_id=str(audit["actor_id"]),
                task_id=str(audit["task_id"]) if audit.get("task_id") is not None else None,
                payload_json=str(raw_payload_json),
                created_at=str(audit["created_at"]),
            )
            hash_matches = hmac.compare_digest(str(audit["hash"]), expected_hash)
    except (KeyError, TypeError, ValueError):
        hash_matches = False

    valid = bool(
        audit_id is not None
        and isinstance(request_audit_id, int)
        and not isinstance(request_audit_id, bool)
        and audit_id == request_audit_id
        and audit.get("event_type") == "approval.requested"
        and audit.get("actor_type") == "device"
        and audit.get("task_id") == task_id
        and audit.get("trace_id") == task_id
        and requester is not None
        and policy is not None
        and policy["decision"] == "ask"
        and payload is not None
        and payload.get("approval_id") == approval_id
        and payload.get("task_id") == task_id
        and payload.get("tool_call_id") == tool_call_id
        and payload.get("tool_name") == tool_name
        and payload.get("action_digest") == action_digest
        and audited_affected_summary == current_affected_summary
        and hash_matches
    )
    return ConsentContext(
        requester=requester,
        policy=policy,
        affected_data_summary=affected_data_summary,
        audit_id=audit_id,
        valid=valid,
    )


def _safe_process_argv(argv: list[Any]) -> tuple[list[str], int]:
    values = [value for value in argv if isinstance(value, str)]
    if not values:
        return [], 0
    command = values[0]
    if command == "git":
        # ProcessSandbox accepts only fixed read-only Git verbs and options.
        visible_count = len(values)
    elif command == "npm" and len(values) >= 2:
        visible_count = min(len(values), 3 if values[1] == "run" else 2)
    elif command == "npx":
        visible_count = min(len(values), 3)
    elif command in {"node", "python3"}:
        visible_count = 1
    else:
        visible_count = 1
    return values[:visible_count], max(0, len(values) - visible_count)
