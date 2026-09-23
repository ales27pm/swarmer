"""Describe accepted project snapshots without promoting model prose to evidence."""

from __future__ import annotations

import re

from app.services.project_contracts import (
    MAX_FILE_BYTES,
    ProjectCheck,
    ProjectPayload,
    ProjectResult,
    validate_project_path,
)
from app.services.project_validation import (
    NATIVE_VALIDATION_DIAGNOSTIC,
    native_validation_unavailable,
)

# Schema 1.0 workers use these exact runtime-owned diagnostics in conversation
# history, including for timeout recovery. Preserve only this closed vocabulary
# when the snapshot and check evidence are unchanged. It is a presentation
# compatibility exception, never a source of progress or retry accounting.
_WORKER_DIAGNOSTICS = frozenset(
    {
        (
            "The local model timed out before returning a complete response. No edits were accepted, "
            "and the previous files and check receipts are unchanged. In the next charged iteration, "
            "return one smaller complete module or a short repair patch, with concise metadata, "
            "within the 2000-token response limit. No retry occurred within this job."
        ),
        (
            "The local model timed out twice without producing an accepted edit, so the project is "
            "paused with its files and check receipts unchanged instead of consuming more model-call "
            "budget. Send a project message when you want to resume."
        ),
        (
            "The model response was incomplete. No edits were accepted. "
            "Return a smaller complete JSON file-edit batch in the next iteration."
        ),
        (
            "The Node build has no root package.json. This repair must create exactly "
            "package.json, or read the existing source first. No edits were accepted."
        ),
        (
            "The model batch exceeded one full-file edit. No edits were accepted. "
            "Return one small complete file or use short patches for repairs. "
            "Continue remaining work in later charged iterations."
        ),
        (
            "The model step failed strict validation. No edits were accepted. "
            "Return valid project-step JSON; updated files belong only in edits, "
            "and deletions contains only existing files being removed."
        ),
        (
            "The model requested a file absent from the current project manifest. "
            "No changes were accepted. Use only existing manifest paths in focus_paths."
        ),
        (
            "The model returned no application files. No changes or checks were accepted. "
            "Create one small complete source module in the next iteration."
        ),
        (
            "The test runner found no tests, but the model returned no test file. "
            "No changes were accepted. Create a small complete test file or read "
            "the application source first."
        ),
        (
            "The proposed Python source exceeds the parser complexity limit. "
            "No changes or checks were accepted. Return one smaller complete module "
            "or simplify the proposed patch."
        ),
        (
            "The model returned no effective project operation. No changes or checks were accepted. "
            "Return an effective file edit, patch or deletion, a focused read of an existing file, "
            "or an explicit check request."
        ),
        (
            "The documentation-only completion did not preserve the accepted project metadata. "
            "No edits were accepted. Return exactly one complete README.md with the existing "
            "plan, runtime, passing check receipts and concise run instructions."
        ),
    }
)
_CONTRACT_REASONS = frozenset(
    {
        "project text is empty, invalid, or exceeds its limit",
        "project text contains forbidden control characters",
        "project text is not valid UTF-8",
        "project path is not a permitted canonical relative path",
        "project file count exceeds its limit",
        "project file has invalid fields",
        "project file exceeds its UTF-8 byte limit",
        "project paths collide",
        "project snapshot exceeds its UTF-8 byte limit",
        "project plan exceeds its limit",
        "project check command is invalid",
        "model project step has invalid fields",
        "model project action or runtime is invalid",
        "model project deletion count exceeds its limit",
        "model requested check count exceeds its limit",
        "model project deletions collide",
        "model both edits and deletes the same path",
        "model patch conflicts with a replacement or deletion",
        "model batch exceeds three changed paths",
        "a clarification cannot modify or execute the project",
        "reading project files requires a continue step without edits or checks",
        "model patch count exceeds its limit",
        "model patch has invalid fields",
        "model patch is unchanged or exceeds its byte limit",
        "project read focus exceeds its limit",
        "project read focus contains duplicate paths",
        "model patch must match exactly once in the current base file",
        "model patches overlap in the current base file",
        "model deletes a file absent from the base snapshot",
        "compact repair fields are invalid",
        "compact repair requires one edit, patch or focused read",
        "compact repair exceeds its source limit",
        "compact repair metadata exceeds its limit",
        "model patches require path, span_id and new",
        "model selected an unknown, stale or differently targeted source span",
        "model patch replacement is identical to the selected source span",
        "model patch replacement must be below 8000 UTF-8 bytes",
    }
)
_REJECTION_WRAPPERS = (
    (
        "The model step was rejected: ",
        (
            ". No changes were accepted. "
            "Correct that exact contract violation in the next complete batch."
        ),
    ),
    (
        "The model batch was rejected: ",
        (
            ". No edits were accepted. "
            "For patches, copy the shortest unique old substring exactly; omit unchanged "
            "surrounding lines and leading/trailing whitespace when they are unnecessary. "
            "Never guess indentation or change unrelated behavior."
        ),
    ),
)
_PYTHON_SYNTAX_REJECTION = re.compile(
    r"The proposed Python file (?P<path>[A-Za-z0-9_.@/-]{1,240}) "
    r"has a SyntaxError at line (?P<line>[1-9][0-9]{0,4})\. "
    r"No changes or checks were accepted\. Preserve the current snapshot and correct "
    r"the proposed edit or patch before retrying\."
)


def _is_worker_diagnostic(message: str) -> bool:
    if message in _WORKER_DIAGNOSTICS:
        return True
    syntax = _PYTHON_SYNTAX_REJECTION.fullmatch(message)
    if syntax is not None:
        path = syntax.group("path")
        if not path.endswith(".py") or int(syntax.group("line")) > MAX_FILE_BYTES + 1:
            return False
        try:
            validate_project_path(path)
        except ValueError:
            return False
        return True
    return any(
        message == prefix + reason + suffix
        for prefix, suffix in _REJECTION_WRAPPERS
        for reason in _CONTRACT_REASONS
    )


def _check_signatures(checks: list[ProjectCheck]) -> set[tuple[tuple[str, ...], str, int | None]]:
    return {(tuple(check.command), check.status, check.exit_code) for check in checks}


def has_project_progress(payload: ProjectPayload, result: ProjectResult) -> bool:
    """Count source changes or new passing evidence, not log or metadata churn."""
    if {file.path: file.content for file in payload.files} != {
        file.path: file.content for file in result.files
    }:
        return True
    if native_validation_unavailable(result.files):
        return False
    previous = _check_signatures(payload.checks)
    return any(
        signature[1:] == ("passed", 0) and signature not in previous
        for signature in _check_signatures(result.checks)
    )


def project_progress_message(payload: ProjectPayload, result: ProjectResult) -> str:
    """Return evidence-owned status, retaining genuine questions and fixed errors."""
    if native_validation_unavailable([*payload.files, *result.files]):
        return NATIVE_VALIDATION_DIAGNOSTIC
    # The public schema permits a question alongside a snapshot. Do not turn
    # that accepted question into a progress statement marked as a question.
    if result.action == "clarify" and not _is_worker_diagnostic(result.message):
        return result.message
    before = {file.path: file.content for file in payload.files}
    after = {file.path: file.content for file in result.files}
    same_files = before == after
    previous_checks = _check_signatures(payload.checks)
    checks = _check_signatures(result.checks)
    if same_files and not result.focus_paths:
        worker_diagnostic = _is_worker_diagnostic(result.message)
        # Rejections preserve the actual old receipts, not merely their outcome
        # signatures. A different execution log or timing is not that rejection.
        if payload.checks == result.checks and worker_diagnostic:
            return result.message
    if same_files and result.focus_paths:
        return (
            "Files requested for reading: "
            + ", ".join(result.focus_paths)
            + ". No project files changed."
        )

    added = len(after.keys() - before.keys())
    removed = len(before.keys() - after.keys())
    modified = sum(before[path] != after[path] for path in before.keys() & after.keys())
    change_message = (
        "No project files changed."
        if same_files
        else f"Project draft changes: {added} added, {modified} modified, {removed} removed files."
    )
    if checks:
        passed = sum(status == "passed" for _, status, _ in checks)
        failed = sum(status == "failed" for _, status, _ in checks)
        skipped = sum(status == "skipped" for _, status, _ in checks)
        check_message = f"Check receipts: {passed} passed, {failed} failed, {skipped} skipped."
        if checks == previous_checks:
            check_message += " Check outcomes are unchanged from the previous iteration."
    else:
        check_message = "No check receipts are available."
    readiness = (
        "Draft ready for review; it has not been applied to the workspace."
        if result.action == "complete"
        else "Project completion has not been established."
    )
    return f"{change_message} {check_message} {readiness}"
