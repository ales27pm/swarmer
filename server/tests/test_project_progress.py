from __future__ import annotations

from typing import Any

import pytest

from app.services.project_contracts import ProjectPayload, ProjectResult, project_digest
from app.services.project_progress import has_project_progress, project_progress_message

FILES = [
    {"path": "README.md", "content": "CRM contact directory"},
    {"path": "contacts.txt", "content": "Name, email"},
]
CLAIM = "I implemented every CRM feature and all tests passed."
TIMEOUT = (
    "The local model timed out before returning a complete response. No edits were accepted, "
    "and the previous files and check receipts are unchanged. In the next charged iteration, "
    "return one smaller complete module or a short repair patch, with concise metadata, "
    "within the 2000-token response limit. No retry occurred within this job."
)
INCOMPLETE = (
    "The model response was incomplete. No edits were accepted. "
    "Return a smaller complete JSON file-edit batch in the next iteration."
)
IDENTICAL = (
    "The model step was rejected: model patch replacement is identical to the selected source span. "
    "No changes were accepted. Correct that exact contract violation in the next complete batch."
)
NO_APPLICATION = (
    "The model returned no application files. No changes or checks were accepted. "
    "Create one small complete source module in the next iteration."
)
NO_TEST_FILE = (
    "The test runner found no tests, but the model returned no test file. "
    "No changes were accepted. Create a small complete test file or read "
    "the application source first."
)
COMPLEX_SOURCE = (
    "The proposed Python source exceeds the parser complexity limit. "
    "No changes or checks were accepted. Return one smaller complete module "
    "or simplify the proposed patch."
)
NO_EFFECTIVE_OPERATION = (
    "The model returned no effective project operation. No changes or checks were accepted. "
    "Return an effective file edit, patch or deletion, a focused read of an existing file, "
    "or an explicit check request."
)


def syntax_rejection(path: str = "tests/test_store.py", line: str = "18") -> str:
    return (
        f"The proposed Python file {path} has a SyntaxError at line {line}. "
        "No changes or checks were accepted. Preserve the current snapshot and correct "
        "the proposed edit or patch before retrying."
    )


def check(status: str = "passed", **updates: Any) -> dict[str, Any]:
    return {
        "command": ["python", "-m", "pytest"],
        "status": status,
        "exit_code": 0 if status == "passed" else 1 if status == "failed" else None,
        "output": "CRM checks",
        "duration_ms": 10,
        **updates,
    }


def payload(**updates: Any) -> ProjectPayload:
    return ProjectPayload.model_validate(
        {
            "objective": "Create a CRM contact directory",
            "conversation": [],
            "files": FILES,
            "plan": ["Contacts", "Tests"],
            "checks": [],
            "iteration": 2,
            "base_revision_id": "revision_crm",
            "base_sha256": project_digest(FILES),
            **updates,
        }
    )


def result(**updates: Any) -> ProjectResult:
    return ProjectResult.model_validate(
        {
            "schema_version": "1.0",
            "action": "continue",
            "message": CLAIM,
            "plan": ["Contacts", "Tests"],
            "files": FILES,
            "checks": [],
            "run_instructions": "Read README.md",
            "runtime": "python",
            "base_revision_id": "revision_crm",
            "base_sha256": project_digest(FILES),
            **updates,
        }
    )


@pytest.mark.parametrize(
    "files",
    [
        [*FILES, {"path": "calendar.txt", "content": "Appointments"}],
        [FILES[0], {"path": "contacts.txt", "content": "Name, email, phone"}],
        [FILES[0]],
        [FILES[0], {"path": "clients.txt", "content": FILES[1]["content"]}],
    ],
)
def test_file_add_modify_delete_or_rename_is_progress(files: list[dict[str, str]]) -> None:
    assert has_project_progress(payload(), result(files=files))


@pytest.mark.parametrize(
    "updates",
    [
        {"files": list(reversed(FILES))},
        {"message": "Everything is implemented"},
        {"plan": ["All done"]},
        {"run_instructions": "Start the CRM"},
        {"runtime": "node"},
        {"focus_paths": ["contacts.txt"]},
    ],
)
def test_metadata_and_read_requests_are_not_progress(updates: dict[str, Any]) -> None:
    assert not has_project_progress(payload(), result(**updates))


@pytest.mark.parametrize("old_status", [None, "failed", "skipped"])
def test_new_or_improved_passing_command_is_progress(old_status: str | None) -> None:
    before = payload(checks=[] if old_status is None else [check(old_status)])
    assert has_project_progress(before, result(checks=[check()]))


def test_distinct_command_receipt_is_progress_even_with_an_existing_pass() -> None:
    assert has_project_progress(
        payload(checks=[check()]),
        result(checks=[check(), check(command=["python", "-m", "pytest", "tests/contacts"])]),
    )


@pytest.mark.parametrize(
    "checks",
    [
        [],
        [check(output="20 passed", duration_ms=900_000)],
        [check(), check()],
        [check("failed")],
        [check("skipped")],
        [check(), check("failed", command=["crm", "build"])],
    ],
)
def test_check_noise_duplicates_removed_or_failed_receipts_are_not_progress(
    checks: list[dict[str, Any]],
) -> None:
    assert not has_project_progress(payload(checks=[check()]), result(checks=checks))


def test_failed_exit_change_is_not_improvement() -> None:
    assert not has_project_progress(
        payload(checks=[check("failed", exit_code=2)]),
        result(checks=[check("failed", exit_code=1)]),
    )


def test_check_order_and_log_noise_do_not_create_progress_or_fake_new_checks() -> None:
    old_checks = [check(), check("failed", command=["crm", "build"])]
    new_checks = [
        {**receipt, "output": "Different log", "duration_ms": 500} for receipt in old_checks
    ]
    before, after = payload(checks=old_checks), result(checks=list(reversed(new_checks)))
    assert not has_project_progress(before, after)
    assert "outcomes are unchanged" in project_progress_message(before, after)


def test_read_message_suppresses_claim_and_names_only_requested_files() -> None:
    message = project_progress_message(payload(), result(focus_paths=["contacts.txt"]))
    assert message == "Files requested for reading: contacts.txt. No project files changed."
    assert CLAIM not in message


def test_factual_counts_and_failed_receipts_replace_model_success_claim() -> None:
    after = result(
        files=[
            {"path": "README.md", "content": "Updated CRM requirements"},
            {"path": "calendar.txt", "content": "Appointments"},
        ],
        checks=[check("failed"), check("skipped", command=["crm", "lint"])],
    )
    message = project_progress_message(payload(), after)
    assert "1 added, 1 modified, 1 removed files" in message
    assert "0 passed, 1 failed, 1 skipped" in message
    assert "completion has not been established" in message
    assert CLAIM not in message


def test_unchanged_complete_snapshot_reports_review_without_new_implementation_claim() -> None:
    message = project_progress_message(
        payload(checks=[check()]), result(action="complete", checks=[check()])
    )
    assert "No project files changed" in message
    assert "outcomes are unchanged" in message
    assert "Draft ready for review" in message
    assert "not been applied" in message
    assert CLAIM not in message


def test_genuine_clarification_is_preserved() -> None:
    question = "Should CRM contacts be shared with the team?"
    assert (
        project_progress_message(payload(), result(action="clarify", message=question)) == question
    )


def test_public_clarification_with_snapshot_preserves_its_question() -> None:
    question = "Which contact fields should the next module include?"
    after = result(
        action="clarify",
        message=question,
        files=[{"path": "README.md", "content": "CRM project outline"}],
        checks=[check()],
    )
    assert project_progress_message(payload(), after) == question


def test_repeated_timeout_pause_preserves_exact_runtime_message() -> None:
    diagnostic = (
        "The local model timed out twice without producing an accepted edit, so the project is "
        "paused with its files and check receipts unchanged instead of consuming more model-call "
        "budget. Send a project message when you want to resume."
    )
    before, after = payload(), result(action="clarify", message=diagnostic)
    assert project_progress_message(before, after) == diagnostic
    assert not has_project_progress(before, after)


def test_clarification_cannot_preserve_false_timeout_when_new_checks_exist() -> None:
    after = result(action="clarify", message=TIMEOUT, checks=[check()])
    assert project_progress_message(payload(), after) != TIMEOUT


@pytest.mark.parametrize("diagnostic", [TIMEOUT, INCOMPLETE, IDENTICAL])
def test_exact_runtime_rejection_is_preserved_without_changing_progress(diagnostic: str) -> None:
    before, after = (
        payload(checks=[check("failed")]),
        result(message=diagnostic, checks=[check("failed")]),
    )
    assert not has_project_progress(before, after)
    assert project_progress_message(before, after) == diagnostic


@pytest.mark.parametrize(
    "message",
    [
        INCOMPLETE + " I implemented all CRM features.",
        " " + TIMEOUT,
        IDENTICAL.replace(
            "model patch replacement is identical to the selected source span",
            "the CRM is implemented and tested",
        ),
        CLAIM,
    ],
)
def test_diagnostic_prefix_or_arbitrary_note_cannot_bypass_factual_message(message: str) -> None:
    factual = project_progress_message(payload(), result(message=message))
    assert factual != message
    assert factual.startswith("No project files changed.")
    assert "implemented" not in factual


@pytest.mark.parametrize(
    "updates",
    [
        {"files": [FILES[0]]},
        {"checks": [check()]},
        {"checks": [check("failed")]},
    ],
)
def test_unchanged_diagnostic_not_retained_when_actual_evidence_changes(
    updates: dict[str, Any],
) -> None:
    assert project_progress_message(payload(), result(message=TIMEOUT, **updates)) != TIMEOUT


def test_pure_helpers_leave_payload_and_result_untouched_and_message_bounded() -> None:
    files = [{"path": str(index) + "a" * 239, "content": "CRM"} for index in range(8)]
    before = payload(files=files, base_sha256=project_digest(files))
    after = result(files=files, focus_paths=[file["path"] for file in files])
    originals = before.model_dump_json(), after.model_dump_json()
    assert not has_project_progress(before, after)
    assert len(project_progress_message(before, after)) <= 4_000
    assert originals == (before.model_dump_json(), after.model_dump_json())


@pytest.mark.parametrize(
    "diagnostic",
    [NO_APPLICATION, NO_TEST_FILE, syntax_rejection(), COMPLEX_SOURCE, NO_EFFECTIVE_OPERATION],
)
def test_new_runtime_rejections_preserve_their_cause_without_progress(diagnostic: str) -> None:
    files = [] if diagnostic == NO_APPLICATION else FILES
    identity = {"base_revision_id": None, "base_sha256": None} if not files else {}
    before = payload(files=files, checks=[check("failed")], **identity)
    after = result(files=files, message=diagnostic, checks=[check("failed")], **identity)
    assert project_progress_message(before, after) == diagnostic
    assert not has_project_progress(before, after)


@pytest.mark.parametrize(
    "path,line",
    [
        ("crm.py", "1"),
        ("crm/new_module.py", "64001"),
        ("tests/test_@store-v2.py", "42"),
        ("a" * 237 + ".py", "2"),
    ],
)
def test_syntax_rejection_accepts_bounded_canonical_python_locations(path: str, line: str) -> None:
    message = syntax_rejection(path, line)
    assert project_progress_message(payload(), result(message=message)) == message


@pytest.mark.parametrize(
    "path,line",
    [
        ("/tmp/crm.py", "1"),
        ("../crm.py", "1"),
        ("./crm.py", "1"),
        ("crm//store.py", "1"),
        ("crm/../store.py", "1"),
        ("crm\\store.py", "1"),
        ("crm/.git/store.py", "1"),
        (".env/store.py", "1"),
        ("crm.py; all tests passed", "1"),
        ("a" * 238 + ".py", "1"),
        ("README.md", "1"),
        ("crm.py", "0"),
        ("crm.py", "-1"),
        ("crm.py", "01"),
        ("crm.py", "64002"),
        ("crm.py", "9999999999999999999999999"),
        ("crm.py", "18; all checks passed"),
    ],
)
def test_syntax_rejection_rejects_arbitrary_or_invalid_locations(path: str, line: str) -> None:
    message = syntax_rejection(path, line)
    factual = project_progress_message(payload(), result(message=message))
    assert factual != message
    assert factual.startswith("No project files changed.")
    assert "completion has not been established" in factual


@pytest.mark.parametrize(
    "diagnostic",
    [NO_APPLICATION, NO_TEST_FILE, syntax_rejection(), COMPLEX_SOURCE, NO_EFFECTIVE_OPERATION],
)
@pytest.mark.parametrize("alteration", ["prefix", "suffix", "newline", "changed_word"])
def test_runtime_rejection_format_cannot_carry_extra_model_prose(
    diagnostic: str, alteration: str
) -> None:
    messages = {
        "prefix": "All features implemented. " + diagnostic,
        "suffix": diagnostic + " All tests passed.",
        "newline": diagnostic + "\n",
        "changed_word": diagnostic.replace("No changes", "All changes"),
    }
    message = messages[alteration]
    assert project_progress_message(payload(), result(message=message)) != message


@pytest.mark.parametrize(
    "diagnostic",
    [NO_APPLICATION, NO_TEST_FILE, syntax_rejection(), COMPLEX_SOURCE, NO_EFFECTIVE_OPERATION],
)
@pytest.mark.parametrize("change", ["files", "checks", "output", "duration", "focus"])
def test_runtime_rejection_is_not_retained_when_snapshot_or_receipts_change(
    diagnostic: str, change: str
) -> None:
    before = payload(checks=[check("failed")])
    updates: dict[str, Any] = {"message": diagnostic, "checks": [check("failed")]}
    if change == "files":
        updates["files"] = [FILES[0]]
    elif change == "checks":
        updates["checks"] = [check()]
    elif change == "output":
        updates["checks"] = [check("failed", output="Different execution receipt")]
    elif change == "duration":
        updates["checks"] = [check("failed", duration_ms=11)]
    else:
        updates["focus_paths"] = ["contacts.txt"]
    assert project_progress_message(before, result(**updates)) != diagnostic
