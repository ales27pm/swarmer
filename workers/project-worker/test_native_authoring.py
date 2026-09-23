"""Native source is drafted incrementally, never executed by the Python/Node lane."""

from __future__ import annotations

import copy
import io
import json
from typing import Any

import project_worker as worker
import pytest
from jsonschema import Draft202012Validator
from project_contract import ProjectError, parse_payload, parse_step, snapshot_sha
from test_project_worker import Generator, Runner, payload, step

SWIFT_FILES = [
    {
        "path": "Package.swift",
        "content": '// swift-tools-version: 5.9\nimport PackageDescription\nlet package = Package(name: "Addition", products: [.library(name: "Addition", targets: ["Addition"])], targets: [.target(name: "Addition"), .testTarget(name: "AdditionTests", dependencies: ["Addition"])])\n',
    },
    {
        "path": "Sources/Addition/Addition.swift",
        "content": "public func add(_ a: Int, _ b: Int) -> Int { a + b }\n",
    },
    {
        "path": "Tests/AdditionTests/AdditionTests.swift",
        "content": "import XCTest\n@testable import Addition\nfinal class AdditionTests: XCTestCase { func testAdd() { XCTAssertEqual(add(2, 3), 5) } }\n",
    },
    {
        "path": "README.md",
        "content": "Addition example. Build and test the reviewed revision with swift test.\n",
    },
]


def test_four_native_files_can_be_authored_before_validation_is_required() -> None:
    current = payload()
    runner = Runner()
    for index, file in enumerate(SWIFT_FILES):
        generator = Generator(step(action="continue" if index < 3 else "complete", edits=[file]))
        result = worker.run_iteration(current, generator, runner, lambda: None)
        assert generator.calls == 1
        assert runner.calls == 0 and result["checks"] == []
        assert result["action"] == "continue"
        assert result["native_validation"] == ("authoring" if index < 3 else "required")
        assert len(result["files"]) == index + 1
        current = {
            **current,
            "files": result["files"],
            "plan": result["plan"],
            "checks": result["checks"],
            "iteration": index + 2,
            "base_revision_id": f"revision_{index}",
            "base_sha256": snapshot_sha(result["files"]),
        }
    assert result["run_instructions"] == "python -m pytest"  # Instructions are inert model prose.


def test_native_metadata_cannot_be_supplied_by_the_model() -> None:
    with pytest.raises(ProjectError):
        parse_step(step(native_validation="authoring"))


@pytest.mark.parametrize("operation", ["edit", "read", "reject", "checks", "delete"])
def test_native_authoring_never_runs_python_even_if_swift_is_removed(
    operation: str,
) -> None:
    files = copy.deepcopy(SWIFT_FILES[:2])
    data = {**payload(), "files": files, "plan": ["Finish the native example"]}
    response = step(action="continue", edits=[])
    if operation == "edit":
        response["edits"] = [{"path": "helper.py", "content": "VALUE = 1\n"}]
    elif operation == "read":
        response["focus_paths"] = [files[1]["path"]]
    elif operation == "reject":
        response["patches"] = [
            {"path": files[1]["path"], "old": "absent text", "new": "replacement"}
        ]
    elif operation == "checks":
        response["requested_checks"] = [["python", "-m", "pytest", "-q"]]
    else:
        response["deletions"] = [file["path"] for file in files]
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert runner.calls == 0
    assert result["native_validation"] == ("required" if operation == "checks" else "authoring")
    assert result["action"] != "complete"


def test_native_rejected_model_response_retains_snapshot_and_is_retryable() -> None:
    class Broken:
        def generate(self, data: object, ensure_active: object) -> object:
            raise worker.ModelStepError(worker.REDUNDANT_READ_DIAGNOSTIC)

    data = {**payload(), "files": copy.deepcopy(SWIFT_FILES[:1])}
    result = worker.run_iteration(data, Broken(), Runner(), lambda: None)
    assert result["files"] == data["files"]
    assert result["native_validation"] == "authoring"
    assert result["message"] == worker.REDUNDANT_READ_DIAGNOSTIC


def test_identical_native_replacement_reports_no_effective_operation() -> None:
    current = {
        **payload(),
        "files": copy.deepcopy(SWIFT_FILES[:1]),
        "plan": ["Create package, source, native tests and README"],
        "checks": Runner().run(SWIFT_FILES[:1])["checks"],
        "base_revision_id": "revision_existing",
        "base_sha256": snapshot_sha(SWIFT_FILES[:1]),
    }
    before = copy.deepcopy(current)
    runner = Runner()
    result = worker.run_iteration(
        current,
        Generator(
            step(
                action="continue",
                edits=copy.deepcopy(SWIFT_FILES[:1]),
                message="Implemented the package",
            )
        ),
        runner,
        lambda: None,
    )
    assert result["message"] == worker.NO_EFFECTIVE_OPERATION_DIAGNOSTIC
    assert result["native_validation"] == "authoring" and result["action"] == "continue"
    assert runner.calls == 0 and current == before
    for field in ("files", "checks", "plan", "base_revision_id", "base_sha256"):
        assert result[field] == before[field]


def test_native_marker_survives_empty_snapshot_into_next_iteration() -> None:
    current = {**payload(), "files": copy.deepcopy(SWIFT_FILES[:1])}
    runner = Runner()
    removed = worker.run_iteration(
        current,
        Generator(step(action="continue", edits=[], deletions=["Package.swift"])),
        runner,
        lambda: None,
    )
    data = {
        **payload(),
        "files": removed["files"],
        "native_validation": removed["native_validation"],
    }
    parsed = parse_payload({"required_skill": "code.build_project", "payload": data})
    result = worker.run_iteration(parsed, Generator(step()), runner, lambda: None)
    assert result["native_validation"] == "required"
    assert runner.calls == 0 and result["checks"] == []


@pytest.mark.parametrize("marker", [True, "validated", 1, []])
def test_payload_marker_rejects_unrecognized_states(marker: object) -> None:
    with pytest.raises(ProjectError):
        parse_payload(
            {
                "required_skill": "code.build_project",
                "payload": {**payload(), "native_validation": marker},
            }
        )


@pytest.mark.parametrize("answered", [False, True])
def test_native_ready_without_another_edit_is_accepted_by_real_generator_schema(
    monkeypatch: pytest.MonkeyPatch,
    answered: bool,
) -> None:
    current = {
        **payload(),
        "files": copy.deepcopy(SWIFT_FILES),
        "native_validation": "authoring",
        "checks": [
            {
                "command": ["npm", "run", "build"],
                "status": "failed",
                "exit_code": 1,
                "output": "package.json missing",
                "duration_ms": 1,
            }
        ],
    }
    if answered:
        current["conversation"] = [
            {"role": "user", "content": "Fix the call labels, then request native validation."},
            {"role": "assistant", "content": "The previous revision had incorrect call labels."},
        ]
    response = step(edits=[], patches=[], focus_paths=[])
    requests = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            requests.append(json.loads(request.data))
            return Response(
                json.dumps(
                    {
                        "message": {"content": json.dumps(response)},
                        "done": True,
                        "done_reason": "stop",
                    }
                ).encode()
            )

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "example")
    runner = Runner()
    result = worker.run_iteration(current, generator, runner, lambda: None)
    assert result["native_validation"] == "required" and result["action"] == "continue"
    assert runner.calls == 0
    Draft202012Validator(requests[0]["format"]).validate(response)
    prompt = str(requests[0]["messages"])
    assert "Swift/iOS source is authored" in prompt
    assert "create package.json in this iteration" not in prompt
    assert worker.IMPLEMENTATION_INSTRUCTION not in requests[0]["messages"][0]["content"]
    assert "Historical diagnostics may already be resolved in the current SOURCE" in prompt
    assert "all operation arrays empty" in prompt


def test_native_unread_existing_source_cannot_be_replaced() -> None:
    current = {**payload(), "files": copy.deepcopy(SWIFT_FILES[:2])}
    generator = Generator(
        step(
            action="continue",
            edits=[{"path": SWIFT_FILES[1]["path"], "content": "replacement"}],
        )
    )
    generator.last_visible_paths = {"Package.swift"}
    result = worker.run_iteration(current, generator, Runner(), lambda: None)
    assert snapshot_sha(result["files"]) == snapshot_sha(current["files"])
    assert result["focus_paths"] == [SWIFT_FILES[1]["path"]]
    assert result["native_validation"] == "authoring"


def test_native_continuation_prompt_advances_missing_plan_file_and_preserves_latest_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = "Preserve addition and add overflow handling before validation."
    current = {
        **payload(),
        "objective": "Create a Swift library with arithmetic and real tests.",
        "files": copy.deepcopy(SWIFT_FILES[:1]),
        "plan": [
            "Create Package.swift",
            "Implement Sources/Addition/Addition.swift",
            "Write Tests/AdditionTests/AdditionTests.swift",
        ],
        "conversation": [
            {"role": "assistant", "content": "Package draft recorded."},
            {"role": "user", "content": latest},
        ],
    }
    requests = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            requests.append(json.loads(request.data))
            return Response(
                json.dumps(
                    {
                        "message": {
                            "content": json.dumps(step(action="continue", edits=[SWIFT_FILES[1]]))
                        },
                        "done": True,
                        "done_reason": "stop",
                    }
                ).encode()
            )

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    result = worker.run_iteration(
        current,
        worker.ProjectGenerator("http://127.0.0.1:11434/v1", "example"),
        Runner(),
        lambda: None,
    )
    task = requests[0]["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:\n", 1)[1]
    assert "Continue the existing native project" in task
    assert '"Sources/Addition/Addition.swift"' in task
    assert "advisory" in task and "latest user" in task
    assert latest in task
    assert "Implement this latest user request now:" not in task
    assert current["objective"] in requests[0]["messages"][-1]["content"]
    assert SWIFT_FILES[1] in result["files"] and result["native_validation"] == "authoring"


@pytest.mark.parametrize(
    "plan",
    [
        ["See https://example.org/Sources/Next.swift"],
        [r"Create C:\Windows\Next.swift", r"Create Sources\Next.swift"],
        ["Create /tmp/Next.swift", "Create ../Next.swift", "Create .git/Next.swift"],
        ["Use value.swift()", "let value.swift = 1;", "```Next.swift```"],
        ["Create PACKAGE.SWIFT"],
    ],
)
def test_missing_plan_file_ignores_urls_code_unsafe_and_present_paths(plan: list[str]) -> None:
    assert (
        worker.next_native_plan_file({**payload(), "files": SWIFT_FILES[:1], "plan": plan}) is None
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("edits", "patches"),
        ("edits", "deletions"),
        ("patches", "deletions"),
        ("edits", "requested_checks"),
        ("patches", "requested_checks"),
        ("deletions", "requested_checks"),
    ],
)
def test_native_schema_disjoins_mutation_families(first: str, second: str) -> None:
    current = {**payload(), "files": copy.deepcopy(SWIFT_FILES), "native_validation": "authoring"}
    context = worker.model_context(current)
    addresses = worker.addressed_patch_spans(context, current)
    identifier, address = next(iter(addresses.items()))
    file = next(item for item in current["files"] if item["path"] == address["path"])
    operations = {
        "edits": [{"path": file["path"], "content": file["content"] + "\n// update\n"}],
        "patches": [
            {
                "path": address["path"],
                "span_id": identifier,
                "new": address["old"] + "\n// update\n",
            }
        ],
        "deletions": [file["path"]],
        "requested_checks": [["python", "-m", "pytest", "-q"]],
    }
    schema = worker.constrained_step_schema(
        copy.deepcopy(worker.STEP_SCHEMA), context, current, addresses
    )
    validator = Draft202012Validator(schema)
    response = step(action="continue", edits=[], patches=[], focus_paths=[])
    for field in (first, second):
        single = {**response, field: operations[field]}
        assert validator.is_valid(single), f"single {field} must remain possible"
    assert not validator.is_valid(
        {**response, first: operations[first], second: operations[second]}
    )
    ready = {**response, "action": "complete"}
    assert validator.is_valid(ready)


def test_python_schema_keeps_mixed_operations_on_distinct_paths() -> None:
    current = {**payload(), "files": [{"path": "app.py", "content": "VALUE = 1\n"}]}
    context = worker.model_context(current)
    addresses = worker.addressed_patch_spans(context, current)
    identifier, address = next(iter(addresses.items()))
    response = step(
        action="continue",
        edits=[{"path": "README.md", "content": "Run the Python example.\n"}],
        patches=[{"path": "app.py", "span_id": identifier, "new": "VALUE = 2\n"}],
        focus_paths=[],
    )
    assert address["path"] == "app.py"
    schema = worker.constrained_step_schema(
        copy.deepcopy(worker.STEP_SCHEMA), context, current, addresses
    )
    Draft202012Validator(schema).validate(response)
    parsed = parse_step(worker.resolve_model_patches(response, addresses))
    assert parsed["edits"] and parsed["patches"]
