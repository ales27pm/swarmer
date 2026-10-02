"""An already validated snapshot still needs an explicit readiness decision."""

from __future__ import annotations

import copy
import io
import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import project_worker as worker
from project_contract import snapshot_sha
from runtime import CHECK_COMMANDS, RECEIPT_PREFIX, profiles_for
from test_project_worker import Generator, Runner, capture_project_request, step


@pytest.mark.parametrize("operation", ["edits", "patches", "deletions"])
@pytest.mark.parametrize("action", ["continue", "complete"])
def test_completion_decision_cannot_mutate_even_when_grammar_is_ignored(
    monkeypatch: pytest.MonkeyPatch, operation: str, action: str,
) -> None:
    data = validated_payload()
    before = copy.deepcopy(data)
    source = data["files"][0]
    response = {**check_step(action=action), "requested_checks": []}
    response[operation] = {
        "edits": [{"path": source["path"], "content": source["content"] + "# artificial\n"}],
        "patches": [{"path": source["path"], "old": source["content"], "new": "VALUE = 1\n"}],
        "deletions": [source["path"]],
    }[operation]
    schema = worker.constrained_step_schema(worker.STEP_SCHEMA, worker.model_context(data), data)
    assert not Draft202012Validator(schema).is_valid(response)
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert runner.calls == 0 and result["action"] == "continue"
    assert result["message"] == worker.COMPLETION_DECISION_DIAGNOSTIC
    assert result["files"] == data["files"] and result["checks"] == data["checks"]
    assert data == before
    if operation == "patches":
        response[operation] = [{"path": source["path"], "span_id": "invented", "new": "VALUE = 1\n"}]
    with pytest.raises(worker.ModelStepError, match="explicit completion decision"):
        capture_compact_decision(monkeypatch, data, response)


def test_completion_decision_can_request_visible_source_for_a_real_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = validated_payload()
    read = {
        **check_step(action="continue"), "requested_checks": [],
        "focus_paths": ["calculator.py"], "message": "Division by zero still needs an explicit ValueError.",
    }
    body, generator, parsed = capture_compact_decision(monkeypatch, data, read)
    assert "calculator.py" in generator.last_visible_paths
    assert Draft202012Validator(body["format"]).is_valid(read)
    runner = Runner()
    result = worker.run_iteration(data, Generator(parsed), runner, lambda: None)
    assert runner.calls == 0 and result["files"] == data["files"]
    focused = {
        **data, "focus_paths": result["focus_paths"], "iteration": data["iteration"] + 1,
        # The server replaces the model's read message with a generic notice.
        # The transition relies on focus + source/requirements, not that wording.
        "conversation": [{"role": "assistant", "content": "Files requested for the next iteration: calculator.py"}],
    }
    assert not worker.completion_decision_evidence(focused)
    replacement = {
        "path": "calculator.py", "content": "def divide(a, b):\n    if b == 0:\n        raise ValueError('zero')\n    return a / b\n",
    }
    edit = {**check_step(action="continue"), "requested_checks": [], "edits": [replacement]}
    body = capture_project_request(monkeypatch, focused, edit)
    assert Draft202012Validator(body["format"]).is_valid(edit)
    repaired = worker.run_iteration(focused, Generator(edit), runner, lambda: None)
    assert runner.calls == 1 and replacement in repaired["files"]
    assert repaired["checks"] != data["checks"] and repaired["action"] == "continue"
    with pytest.raises(worker.ModelStepError, match="already fully visible"):
        capture_project_request(monkeypatch, focused, read)


def test_tiny_complete_source_patch_uses_full_extent_and_cannot_duplicate_body() -> None:
    from project_contract import merge_files

    data = validated_payload()
    source = 'def divide(a, b):\n    if b == 0:\n        raise ValueError("Division by zero")\n    return a / b\n'
    data["files"][0]["content"] = source
    data["base_sha256"] = snapshot_sha(data["files"])
    # A real authoring iteration still needs safe patch coordinates.
    data["focus_paths"] = ["calculator.py"]
    context = worker.model_context(data)
    addresses = worker.addressed_patch_spans(context, data, max_bytes=500)
    identifier, address = next((key, value) for key, value in addresses.items() if value["path"] == "calculator.py")
    assert address["old"] == source
    assert (address["start_line"], address["end_line"]) == (1, 4)
    response = {
        **check_step(action="continue"), "requested_checks": [],
        "patches": [{"path": "calculator.py", "span_id": identifier, "new": source}],
    }
    with pytest.raises(worker.ProjectError, match="identical"):
        worker.resolve_model_patches(response, addresses)
    replacement = source.replace("Division by zero", "Zero denominator")
    response["patches"][0]["new"] = replacement
    merged = merge_files(data["files"], worker.resolve_model_patches(response, addresses))
    assert next(item["content"] for item in merged if item["path"] == "calculator.py") == replacement


@pytest.mark.parametrize("case", ["continue_without_operation", "complete_without_checks", "absent_focus", "multiple_focus"])
def test_decision_requires_one_supported_transition(case: str) -> None:
    data = validated_payload()
    response = {**check_step(action="continue"), "requested_checks": []}
    if case == "complete_without_checks":
        response["action"] = "complete"
    elif case == "absent_focus":
        response["focus_paths"] = ["absent.py"]
    elif case == "multiple_focus":
        response["focus_paths"] = [item["path"] for item in data["files"]]
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert runner.calls == 0 and result["message"] == worker.COMPLETION_DECISION_DIAGNOSTIC
    assert result["files"] == data["files"] and result["checks"] == data["checks"]


def test_larger_complete_source_keeps_fine_patch_spans() -> None:
    data = validated_payload()
    data["files"][0]["content"] += "# Additional source context.\n" * 40
    data["base_sha256"] = snapshot_sha(data["files"])
    data["focus_paths"] = ["calculator.py"]
    context = worker.model_context(data)
    spans = worker.visible_patch_spans(context, data)
    assert spans["calculator.py"][0] == "def divide(a, b):\n"
    assert data["files"][0]["content"] not in spans["calculator.py"]


def test_focused_repair_does_not_revive_unrelated_historical_runtime_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = validated_payload()
    data["checks"] += unrelated_failures("node")
    data["focus_paths"] = ["calculator.py"]
    replacement = {
        "path": "calculator.py", "content": "def divide(a, b):\n    if not b:\n        raise ValueError('zero')\n    return a / b\n",
    }
    response = {**check_step(action="continue"), "requested_checks": [], "edits": [replacement]}
    body = capture_project_request(monkeypatch, data, response)
    assert Draft202012Validator(body["format"]).is_valid(response)
    task = body["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:", 1)[1]
    assert "package.json" not in task and "collected NO TESTS" not in task
    assert not worker.completion_decision_evidence(data)
    evidence = worker.material_runtime_evidence(data)
    assert evidence["python"]["ignored_profiles"] == profiles_for("node", [])
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert runner.calls == 1 and replacement in result["files"]
    assert result["checks"] != data["checks"] and result["action"] == "continue"
    for case in ("real_failure", "changed_source"):
        invalid = copy.deepcopy(data)
        if case == "real_failure":
            invalid["checks"][:2] = unrelated_failures("python")
        else:
            invalid["files"][0]["content"] += "# newer source\n"
        assert worker.material_runtime_evidence(invalid) == {}


def passing_check(profile: str, *, count: int | None = None) -> dict[str, Any]:
    return {
        "command": CHECK_COMMANDS[profile][:],
        "status": "passed",
        "exit_code": 0,
        "output": RECEIPT_PREFIX
        + json.dumps(
            {
                "exit_code": 0,
                "tests_executed": (2 if profile.endswith("_test") else 0)
                if count is None else count,
                "test_failures": 0,
            }
        ),
        "duration_ms": 500,
    }


def validated_payload(runtime: str = "python") -> dict[str, Any]:
    files = [
        {"path": "calculator.py", "content": "def divide(a, b):\n    return a / b\n"},
        {
            "path": "tests/test_calculator.py",
            "content": "from calculator import divide\ndef test_divide():\n    assert divide(10, 2) == 5\n",
        },
    ]
    if runtime in {"node", "python_node"}:
        files.append({"path": "package.json", "content": '{"scripts":{"build":"tsc"}}'})
    if runtime == "node":
        files = [item for item in files if not item["path"].endswith(".py")]
        files.append({"path": "calculator.js", "content": "export const divide = (a, b) => a / b;\n"})
    files.sort(key=lambda item: item["path"])
    return {
        "objective": "Provide division and validate the current implementation.",
        "conversation": [],
        "files": files,
        "plan": ["Implement division", "Validate division"],
        "checks": [passing_check(profile) for profile in profiles_for(runtime, [])],
        "iteration": 3,
        "base_revision_id": "revision_validated",
        "base_sha256": snapshot_sha(files),
        "focus_paths": [],
    }


def check_step(runtime: str = "python", action: str = "complete") -> dict[str, Any]:
    return step(
        action=action,
        runtime=runtime,
        edits=[],
        patches=[],
        deletions=[],
        focus_paths=[],
        plan=[],
        requested_checks=[CHECK_COMMANDS[profiles_for(runtime, [])[-1]]],
    )


@pytest.mark.parametrize("runtime", ["python", "node", "python_node"])
def test_validated_check_only_branch_requires_explicit_complete(runtime: str) -> None:
    data = validated_payload(runtime)
    schema = worker.constrained_step_schema(worker.STEP_SCHEMA, worker.model_context(data), data)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(check_step(runtime, "complete"))
    assert not validator.is_valid(check_step(runtime, "continue"))


def test_ignored_grammar_cannot_replay_checks_without_a_decision() -> None:
    data = validated_payload()
    before = copy.deepcopy(data)
    generator, runner = Generator(check_step(action="continue")), Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert generator.calls == 1 and runner.calls == 0
    assert result["action"] == "continue"  # Never auto-promote a model's continue.
    assert result["message"] == worker.COMPLETION_DECISION_DIAGNOSTIC
    assert result["files"] == data["files"] and result["checks"] == data["checks"]
    assert data == before


def test_model_parser_also_enforces_the_completion_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(worker.ModelStepError, match="explicit completion decision"):
        capture_project_request(monkeypatch, validated_payload(), check_step(action="continue"))


def test_explicit_complete_still_runs_fresh_checks_without_editing() -> None:
    data = validated_payload()
    runner = Runner()
    result = worker.run_iteration(data, Generator(check_step()), runner, lambda: None)
    assert runner.calls == 1 and result["action"] == "complete"
    assert result["files"] == data["files"]
    assert result["checks"] != data["checks"]


@pytest.mark.parametrize(
    "case",
    [
        "no_checks", "test_only", "build_only", "no_receipt", "prose_count", "invalid_json",
        "wrong_exit", "zero_tests", "test_failures", "string_count", "bool_count",
        "negative_count", "oversized_count", "failed", "skipped", "bad_status_exit",
        "duplicate_marker", "duplicate_check", "wrong_command", "missing_base",
        "stale_sha", "changed_source", "focused_read", "native_source", "native_lane",
    ],
)
def test_incomplete_or_unusable_evidence_keeps_continue_checks_available(case: str) -> None:
    data = validated_payload()
    check = data["checks"][-1]
    if case == "no_checks":
        data["checks"] = []
    elif case == "test_only":
        data["checks"] = [check]
    elif case == "build_only":
        data["checks"].pop()
    elif case == "no_receipt":
        check["output"] = ""
    elif case == "prose_count":
        check["output"] = "2 passed. Runner: 2 tests executed; 0 failures."
    elif case == "invalid_json":
        check["output"] = RECEIPT_PREFIX + "{invalid"
    elif case in {
        "wrong_exit", "zero_tests", "test_failures", "string_count", "bool_count",
        "negative_count", "oversized_count",
    }:
        receipt = {"exit_code": 0, "tests_executed": 2, "test_failures": 0}
        key, value = {
            "wrong_exit": ("exit_code", 1),
            "zero_tests": ("tests_executed", 0),
            "test_failures": ("test_failures", 1),
            "string_count": ("tests_executed", "2"),
            "bool_count": ("tests_executed", True),
            "negative_count": ("tests_executed", -1),
            "oversized_count": ("tests_executed", 100_001),
        }[case]
        receipt[key] = value
        check["output"] = RECEIPT_PREFIX + json.dumps(receipt)
    elif case == "failed":
        check.update(status="failed", exit_code=1)
    elif case == "skipped":
        check.update(status="skipped", exit_code=None)
    elif case == "bad_status_exit":
        check["exit_code"] = 1
    elif case == "duplicate_marker":
        check["output"] += "\n" + check["output"]
    elif case == "duplicate_check":
        data["checks"].append(copy.deepcopy(check))
    elif case == "wrong_command":
        check["command"] = ["echo", "python -m pytest -q"]
    elif case == "missing_base":
        data.update(base_revision_id=None, base_sha256=None)
    elif case == "stale_sha":
        data["base_sha256"] = "0" * 64
    elif case == "changed_source":
        data["files"][0]["content"] += "# a newer revision\n"
    elif case == "focused_read":
        data["focus_paths"] = ["calculator.py"]
    elif case == "native_source":
        data["files"].append({"path": "App.swift", "content": "import Foundation\n"})
        data["base_sha256"] = snapshot_sha(data["files"])
    elif case == "native_lane":
        data["native_validation"] = "required"
    assert worker.completion_decision_evidence(data) == {}
    schema = worker.constrained_step_schema(worker.STEP_SCHEMA, worker.model_context(data), data)
    assert Draft202012Validator(schema).is_valid(check_step(action="continue"))


@pytest.mark.parametrize("covered", ["python", "node", "python_node"])
@pytest.mark.parametrize("requested", ["python", "node", "python_node"])
def test_decision_is_scoped_to_all_exact_runtime_profiles(covered: str, requested: str) -> None:
    data = validated_payload(requested)
    data["checks"] = [passing_check(profile) for profile in profiles_for(covered, [])]
    expected = set(profiles_for(requested, [])) <= set(profiles_for(covered, []))
    schema = worker.constrained_step_schema(worker.STEP_SCHEMA, worker.model_context(data), data)
    assert Draft202012Validator(schema).is_valid(check_step(requested, "continue")) is not expected
    runner = Runner()
    result = worker.run_iteration(data, Generator(check_step(requested, "continue")), runner, lambda: None)
    assert runner.calls == (0 if expected else 1)
    assert result["action"] == "continue"


@pytest.mark.parametrize("actual,changed", [
    ("python", "python_node"), ("python", "node"), ("node", "python"),
    ("node", "python_node"), ("python_node", "python"), ("python_node", "node"),
])
@pytest.mark.parametrize("action", ["continue", "complete"])
def test_validation_cannot_change_runtime_without_changing_files(
    actual: str, changed: str, action: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = validated_payload(actual)
    response = check_step(changed, action)
    schema = worker.constrained_step_schema(worker.STEP_SCHEMA, worker.model_context(data), data)
    assert not Draft202012Validator(schema).is_valid(response)
    with pytest.raises(worker.ModelStepError, match="runtime of the unchanged files"):
        capture_project_request(monkeypatch, data, response)
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert runner.calls == 0 and result["action"] == "continue"
    assert result["message"] == worker.VALIDATION_RUNTIME_DIAGNOSTIC
    assert result["files"] == data["files"] and result["checks"] == data["checks"]


def test_python_static_web_assets_do_not_add_a_node_runtime() -> None:
    data = validated_payload()
    data["files"] += [
        {"path": "index.html", "content": '<script src="app.js"></script>'},
        {"path": "app.js", "content": "document.title = 'Calculator';"},
        {"path": "style.css", "content": "body { color: black; }"},
    ]
    data["base_sha256"] = snapshot_sha(data["files"])
    schema = worker.constrained_step_schema(worker.STEP_SCHEMA, worker.model_context(data), data)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(check_step("python"))
    assert not validator.is_valid(check_step("python_node"))


def unrelated_failures(runtime: str) -> list[dict[str, Any]]:
    result = []
    for mode in profiles_for(runtime, []):
        code = 1 if mode.endswith("_build") else 5
        check = passing_check(mode, count=0)
        check.update(status="failed", exit_code=code, output=RECEIPT_PREFIX + json.dumps({
            "exit_code": code, "tests_executed": 0, "test_failures": 1 if code == 1 else 0,
        }))
        result.append(check)
    return result


@pytest.mark.parametrize("actual,unrelated", [("python", "node"), ("node", "python")])
def test_known_failures_outside_material_runtime_do_not_hide_existing_evidence(
    actual: str, unrelated: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = validated_payload(actual)
    data["checks"] += unrelated_failures(unrelated)
    if unrelated == "python":
        data["checks"][-1]["output"] = "no tests ran in 0.01s\n" + data["checks"][-1]["output"]
    else:
        data["checks"][-1]["output"] = "TAP version 13\n" + "".join(
            f"# {field} 0\n" for field in ("tests", "pass", "fail", "cancelled", "skipped", "todo")
        ) + data["checks"][-1]["output"]
    evidence = worker.completion_decision_evidence(data)
    assert list(evidence) == [actual]
    assert evidence[actual]["tests_executed"] == 2
    assert evidence[actual]["ignored_profiles"] == profiles_for(unrelated, [])
    schema = worker.constrained_step_schema(worker.STEP_SCHEMA, worker.model_context(data), data)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(check_step(actual, "complete"))
    assert not validator.is_valid(check_step(actual, "continue"))
    request = capture_project_request(monkeypatch, data, check_step(actual))
    assert Draft202012Validator(request["format"]).is_valid(check_step(actual))
    assert "collected NO TESTS" not in request["messages"][-1]["content"]
    runner = Runner()
    result = worker.run_iteration(data, Generator(check_step(actual)), runner, lambda: None)
    assert runner.calls == 1 and result["action"] == "complete"
    assert result["checks"] != data["checks"] and result["files"] == data["files"]


@pytest.mark.parametrize("case", ["actual_failure", "unknown_passed", "unknown_failed", "install_failed", "ambiguous_unrelated"])
def test_ignoring_unrelated_profiles_does_not_hide_real_or_unknown_failures(case: str) -> None:
    data = validated_payload()
    data["checks"] += unrelated_failures("node")
    if case == "actual_failure":
        data["checks"][:2] = unrelated_failures("python")
    elif case.startswith("unknown_"):
        check = passing_check("python_test")
        check["command"] = ["custom", "check"]
        if case == "unknown_failed":
            check.update(status="failed", exit_code=1)
        data["checks"].append(check)
    elif case == "install_failed":
        data["checks"].append({
            **passing_check("python_build"), "command": ["python", "-m", "pip", "install", "-r", "requirements.txt"],
            "status": "failed", "exit_code": 1, "output": "install failed",
        })
    elif case == "ambiguous_unrelated":
        data["checks"][-1]["output"] += "\n" + data["checks"][-1]["output"]
    assert worker.completion_decision_evidence(data) == {}


def test_material_runtime_can_change_through_real_authoring() -> None:
    data = validated_payload()
    # After the decision selects a source to prepare the actual runtime change.
    data["focus_paths"] = ["calculator.py"]
    response = {
        **check_step("python_node", "continue"), "requested_checks": [],
        "edits": [{"path": "package.json", "content": '{"scripts":{"build":"tsc"}}'}],
    }
    schema = worker.constrained_step_schema(worker.STEP_SCHEMA, worker.model_context(data), data)
    assert Draft202012Validator(schema).is_valid(response)
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert runner.calls == 1 and result["action"] == "continue"
    assert response["edits"][0] in result["files"]


def test_new_user_requirement_can_prepare_real_edits_or_clarify() -> None:
    data = validated_payload()
    data["conversation"] = [
        {"role": "assistant", "content": "Prior checks passed."},
        {"role": "user", "content": "Now raise ValueError when the denominator is zero."},
    ]
    replacement = {
        "path": "calculator.py",
        "content": "def divide(a, b):\n    if b == 0:\n        raise ValueError('zero')\n    return a / b\n",
    }
    schema = worker.constrained_step_schema(worker.STEP_SCHEMA, worker.model_context(data), data)
    validator = Draft202012Validator(schema)
    response = {**check_step(action="continue"), "requested_checks": [], "edits": [replacement]}
    assert not validator.is_valid(response)
    assert validator.is_valid({**check_step(action="clarify"), "requested_checks": []})
    read = {**check_step(action="continue"), "requested_checks": [], "focus_paths": ["calculator.py"]}
    assert validator.is_valid(read)
    runner = Runner()
    prepared = worker.run_iteration(data, Generator(read), runner, lambda: None)
    assert runner.calls == 0 and prepared["files"] == data["files"]
    focused = {**data, "focus_paths": prepared["focus_paths"]}
    result = worker.run_iteration(focused, Generator(response), runner, lambda: None)
    assert runner.calls == 1 and result["action"] == "continue"
    assert replacement in result["files"]


def test_omitted_source_can_still_be_read_after_passing_checks() -> None:
    data = validated_payload()
    context = worker.model_context(data)
    context["selected_complete_files"] = [data["files"][1]]
    schema = worker.constrained_step_schema(worker.STEP_SCHEMA, context, data)
    response = {
        **check_step(action="continue"), "requested_checks": [], "focus_paths": ["calculator.py"],
    }
    assert Draft202012Validator(schema).is_valid(response)
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert runner.calls == 0 and result["focus_paths"] == ["calculator.py"]
    assert result["action"] == "continue" and result["files"] == data["files"]


@pytest.mark.parametrize("failure", ["zero_tests", "failed_test", "failed_build", "skipped", "runtime_error"])
def test_historical_evidence_never_bypasses_fresh_failure(failure: str) -> None:
    data = validated_payload()

    class FailingRunner(Runner):
        def run(self, files: list[dict[str, str]], *args: Any) -> dict[str, Any]:
            evidence = super().run(files, *args)
            if failure == "zero_tests":
                evidence["tests_executed"] = 0
            elif failure == "failed_test":
                evidence["test_failures"] = 1
            elif failure == "failed_build":
                evidence["build_passed"] = False
            elif failure == "skipped":
                evidence["checks"][0].update(status="skipped", exit_code=None)
            elif failure == "runtime_error":
                raise worker.ProjectRuntimeError("runner unavailable")
            return evidence

    runner = FailingRunner()
    result = worker.run_iteration(data, Generator(check_step()), runner, lambda: None)
    assert runner.calls == 1 and result["action"] == "continue"
    assert result["files"] == data["files"] and result["checks"] != data["checks"]


def test_prompt_exposes_bounded_evidence_without_claiming_completeness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = validated_payload()
    body = capture_project_request(monkeypatch, data, check_step())
    context = worker.model_context(data)
    summary = context["completion_decision"]
    assert summary == {
        "base_revision_id": data["base_revision_id"],
        "base_sha256": data["base_sha256"],
        "prior_runtime_evidence": {
            "python": {"profiles": ["python_build", "python_test"], "tests_executed": 2},
        },
        "requirements_completeness": "not_inferred",
        "fresh_validation_required_for_complete": True,
    }
    assert all(check["output"] == "" for check in context["checks"])
    instruction = body["messages"][0]["content"]
    assert "passing checks alone do not establish completeness" in instruction
    assert "Do not return continue just to repeat these checks" in instruction
    assert Draft202012Validator(body["format"]).is_valid(check_step())


def capture_compact_decision(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, Any], response: dict[str, Any],
) -> tuple[dict[str, Any], worker.ProjectGenerator, dict[str, Any]]:
    captured = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, **kwargs: Any) -> Response:
            captured.append(json.loads(request.data))
            return Response(json.dumps({
                "message": {"content": json.dumps(response)}, "done": True, "done_reason": "stop",
            }).encode())

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        context_tokens=64_000, prompt_max_bytes=48_000,
    )
    result = generator.generate(data)
    assert len(captured) == 1
    return captured[0], generator, result


@pytest.mark.parametrize("prior_timeout", [False, True])
def test_validated_snapshot_uses_compact_decision_even_without_a_failed_current_check(
    monkeypatch: pytest.MonkeyPatch, prior_timeout: bool,
) -> None:
    from test_project_agent_capsule import capsule

    data = validated_payload()
    data["checks"] += unrelated_failures("node")
    data["durable_context"] = capsule()
    data["durable_context"]["requirements"] = [
        {"source_id": f"message_requirement_{i}", "text": text}
        for i, text in enumerate([
            "Preserve division's existing behavior.", "Reject division by zero.",
            "Keep negative operands supported.", "Execute the existing tests without changing files.",
        ])
    ]
    data["memory"] = {"items": [{"summary": "OPTIONAL_HISTORICAL_HINT " * 140}]}
    data["conversation"] = [{"role": "user", "content": "Validate the current calculator without artificial edits."}]
    if prior_timeout:
        data["conversation"].append({"role": "assistant", "content": worker.MODEL_TIMEOUT_DIAGNOSTIC})
    original = copy.deepcopy(data)
    body, generator, result = capture_compact_decision(monkeypatch, data, check_step())
    messages = "\n".join(item["content"] for item in body["messages"])
    assert generator.last_transport_metrics.get("compact_completion") == 1
    assert generator.last_transport_metrics["compact_repair"] == 0
    assert len(messages.encode()) <= 16_000
    assert len(json.dumps(body["format"], separators=(",", ":")).encode()) <= 6_000
    assert body["options"]["num_predict"] == 768
    assert "OPTIONAL_HISTORICAL_HINT" not in messages
    for requirement in data["durable_context"]["requirements"]:
        assert requirement["text"] in messages and requirement["source_id"] in messages
    assert data["durable_context"]["operating_guidance"]["content"] in messages
    assert data["durable_context"]["fingerprint"] in messages
    assert data["base_sha256"] in messages and data["base_revision_id"] in messages
    assert data["files"][0]["content"] in messages and data["files"][1]["content"] in messages
    assert "collected NO TESTS" not in messages
    assert "return one smaller complete module" not in messages
    assert result["plan"] == data["plan"] and data == original
    validator = Draft202012Validator(body["format"])
    assert validator.is_valid(check_step())
    assert validator.is_valid({**check_step(action="clarify"), "requested_checks": []})
    assert not validator.is_valid({
        **check_step(action="continue"), "requested_checks": [],
        "edits": [{"path": "calculator.py", "content": "def divide(a, b):\n    return a / b\n"}],
    })
    assert not validator.is_valid(check_step(action="continue"))


@pytest.mark.parametrize("repeated", [False, True])
def test_validation_timeout_never_demands_an_artificial_edit(
    monkeypatch: pytest.MonkeyPatch, repeated: bool,
) -> None:
    data = validated_payload()

    class Opener:
        def open(self, request: Any, **kwargs: Any) -> None:
            raise TimeoutError("private endpoint")

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b")
    runner = Runner()
    first = worker.run_iteration(data, generator, runner, lambda: None)
    if repeated:
        data["conversation"] = [{"role": "assistant", "content": first["message"]}]
    result = worker.run_iteration(data, generator, runner, lambda: None) if repeated else first
    assert runner.calls == 0
    assert result["action"] == ("clarify" if repeated else "continue")
    assert "validation decision" in result["message"]
    assert "module" not in result["message"] and "patch" not in result["message"]
    assert "accepted edit" not in result["message"] and "private" not in result["message"]
    assert result["files"] == data["files"] and result["checks"] == data["checks"]


def test_compact_completion_can_read_omitted_source_before_real_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = validated_payload()
    data["files"].append({"path": "larger.py", "content": "# Omitted source.\n" * 3_000})
    data["base_sha256"] = snapshot_sha(data["files"])
    read = {**check_step(action="continue"), "requested_checks": [], "focus_paths": ["larger.py"]}
    body, _, result = capture_compact_decision(monkeypatch, data, read)
    assert result["action"] == "continue" and result["focus_paths"] == ["larger.py"]
    assert result["plan"] == data["plan"]
    assert Draft202012Validator(body["format"]).is_valid(read)
    edit = {
        **check_step(action="continue"), "requested_checks": [],
        "edits": [{"path": "calculator.py", "content": "def divide(a, b):\n    return a / b if b else 0\n"}],
    }
    focused = {**data, "focus_paths": result["focus_paths"]}
    body, _, result = capture_compact_decision(monkeypatch, focused, edit)
    assert result["action"] == "continue" and result["edits"] == edit["edits"]
    assert Draft202012Validator(body["format"]).is_valid(edit)


@pytest.mark.parametrize("invalid", ["plan", "oversized_edit", "oversized_message", "mixed_operations", "continue_checks"])
def test_compact_decision_ignored_grammar_remains_rejected_before_execution(
    monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    response = check_step()
    if invalid == "plan":
        response["plan"] = ["Replace accepted plan"]
    elif invalid == "oversized_edit":
        response.update(action="continue", requested_checks=[], edits=[{"path": "calculator.py", "content": "x" * 801}])
    elif invalid == "oversized_message":
        response["message"] = "x" * 161
    elif invalid == "mixed_operations":
        response["edits"] = [{"path": "calculator.py", "content": "changed = True\n"}]
    else:
        response["action"] = "continue"
    with pytest.raises(worker.ModelStepError):
        capture_compact_decision(monkeypatch, validated_payload(), response)


def test_compact_decision_parsed_complete_still_executes_fresh_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = validated_payload()
    _, _, parsed = capture_compact_decision(monkeypatch, data, check_step())
    runner = Runner()
    result = worker.run_iteration(data, Generator(parsed), runner, lambda: None)
    assert runner.calls == 1 and result["action"] == "complete"
    assert result["files"] == data["files"] and result["plan"] == data["plan"]
    assert result["checks"] != data["checks"]


def test_large_mandatory_requirements_fail_without_truncation_or_a_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_project_agent_capsule import capsule

    data = validated_payload()
    data["durable_context"] = capsule()
    data["durable_context"]["requirements"] = [
        {"text": "Nonnegotiable requirement " * 1_100, "source_id": "message_large"},
    ]
    calls = []

    class Opener:
        def open(self, request: Any, **kwargs: Any) -> None:
            calls.append(request)
            raise AssertionError("oversized decision reached model")

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b", context_tokens=64_000, prompt_max_bytes=48_000,
    )
    original = copy.deepcopy(data)
    with pytest.raises(worker.ProjectError, match="context budget"):
        generator.generate(data)
    assert calls == [] and data == original


@pytest.mark.parametrize("new_timeouts", [0, 2])
def test_soft_decision_budget_preserves_small_source_and_new_timeout_history(
    monkeypatch: pytest.MonkeyPatch, new_timeouts: int,
) -> None:
    from test_project_agent_capsule import capsule

    data = validated_payload()
    data["durable_context"] = capsule()
    data["durable_context"]["requirements"] = [
        {"text": f"Requirement {i}: " + "Preserve every specified behavior. " * 100, "source_id": f"message_{i}"}
        for i in range(4)
    ]
    for index in range(new_timeouts):
        experience = copy.deepcopy(data["durable_context"]["experiences"]["items"][0])
        experience.update(
            source_id=f"node_timeout_{index}", worker_job_id=f"job_timeout_{index}",
            outcome="failed", observation_kind="measured_failure",
            source_revision_id=data["base_revision_id"], source_sha256=data["base_sha256"],
            summary="Model timed out after 240 seconds. No fresh checks or changed files." * 5,
        )
        data["durable_context"]["experiences"]["items"].append(experience)
    latest = {"role": "user", "content": "Resume validation only; keep all four original requirements."}
    data["conversation"] = [latest, {"role": "assistant", "content": worker.MODEL_REPEATED_TIMEOUT_DIAGNOSTIC}]
    body, generator, result = capture_compact_decision(monkeypatch, data, check_step())
    size = sum(len(item["content"].encode()) for item in body["messages"])
    assert 16_000 < size <= 24_000
    assert latest in body["messages"]
    assert generator.last_visible_paths == {item["path"] for item in data["files"]}
    metadata = json.loads(body["messages"][-1]["content"].split("Current workspace data:\n", 1)[1].split("\n\n", 1)[0])
    assert metadata["durable_project_requirements"] == data["durable_context"]
    assert result["plan"] == data["plan"] and result["action"] == "complete"
