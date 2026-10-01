from __future__ import annotations

import copy
import json
from typing import Any

import project_worker as worker
import pytest
from jsonschema import Draft202012Validator
from test_project_read_progress import model_transport
from test_project_worker import NODE_EMPTY_TAP, Runner, node_check, payload, step


def missing_tests(runtime: str) -> dict:
    files = [{"path": "app.py", "content": "VALUE = 7\n"}]
    if runtime == "node":
        files = [
            {"path": "app.js", "content": "export const value = 7;\n"},
            {"path": "package.json", "content": '{"type":"module"}'},
        ]
    return {
        **payload(),
        "objective": "Create real tests for the existing application; preserve its behavior.",
        "files": files,
        "checks": [
            node_check(
                ["python", "-m", "pytest", "-q"] if runtime == "python" else ["node", "--test"],
                "no tests ran in 0.00s" if runtime == "python" else NODE_EMPTY_TAP,
                code=5,
            )
        ],
    }


@pytest.mark.parametrize("runtime", ["python", "node"])
@pytest.mark.parametrize(
    "path",
    ["app.py", "README.md", "requirements.txt", "package.json", "tests/helper.py"],
)
def test_non_test_edit_cannot_satisfy_measured_missing_tests(monkeypatch, runtime, path):
    data = missing_tests(runtime)
    original = copy.deepcopy(data)
    response = step(
        action="continue",
        runtime=runtime,
        edits=[{"path": path, "content": "# unrelated edit\n"}],
        patches=[],
        focus_paths=[],
    )
    generator, requests = model_transport(monkeypatch, response)
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert len(requests) == 1 and runner.calls == 0
    assert not Draft202012Validator(requests[0]["format"]).is_valid(response)
    assert result["files"] == data["files"] and result["checks"] == data["checks"]
    assert "test file" in result["message"]
    assert data == original


@pytest.mark.parametrize(
    "runtime,path,content",
    [
        (
            "python",
            "tests/test_value.py",
            "from app import VALUE\n\ndef test_value():\n    assert VALUE == 7\n",
        ),
        (
            "python",
            "value_test.py",
            "from app import VALUE\n\ndef test_value():\n    assert VALUE == 7\n",
        ),
        (
            "node",
            "tests/app.test.mjs",
            "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { value } from '../app.js';\ntest('value', () => assert.equal(value, 7));\n",
        ),
    ],
)
def test_missing_tests_accepts_discoverable_test_file(monkeypatch, runtime, path, content):
    data = missing_tests(runtime)
    response = step(
        action="continue",
        runtime=runtime,
        edits=[{"path": path, "content": content}],
        patches=[],
        focus_paths=[],
    )
    generator, requests = model_transport(monkeypatch, response)
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert runner.calls == 1 and len(requests) == 1
    assert Draft202012Validator(requests[0]["format"]).is_valid(response)
    assert any(item["path"] == path and item["content"] == content for item in result["files"])


def test_test_creation_retains_source_without_redundant_patch_previews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = missing_tests("python")
    source = "VALUE = 7\n" + "# Existing application implementation\n" * 80
    data["files"][0]["content"] = source
    response = step(
        action="continue",
        runtime="python",
        edits=[
            {
                "path": "tests/test_value.py",
                "content": "from app import VALUE\ndef test_value(): assert VALUE == 7\n",
            }
        ],
        patches=[],
        focus_paths=[],
    )
    generator, requests = model_transport(monkeypatch, response)
    generator.generate(data)
    message = requests[0]["messages"][-1]["content"]
    # The actual source remains available once; patch previews and their IDs
    # cannot be used by this creation-only operation and need not be duplicated.
    context: dict[str, Any] = json.loads(
        message.split("Current workspace data:\n", 1)[1].split("\n\n", 1)[0]
    )
    assert message.count(source) == 1
    assert 'SOURCE {"path":"app.py","complete":true' in message
    assert context["editable_spans"] == []
    assert "PATCH_TARGET " not in message
    assert data["objective"] in message


def test_no_readme_is_not_a_readiness_block_when_run_instructions_and_checks_exist():
    from test_project_worker import Generator

    data = {
        **payload(),
        "objective": "Only app.py and tests/test_app.py. No additional files.",
    }
    response = step(
        edits=[
            {"path": "app.py", "content": "VALUE = 7\n"},
            {
                "path": "tests/test_app.py",
                "content": "from app import VALUE\n\ndef test_value(): assert VALUE == 7\n",
            },
        ]
    )
    result = worker.run_iteration(data, Generator(response), Runner(), lambda: None)
    assert result["action"] == "complete"
    assert "README" not in result["message"]
    assert {item["path"] for item in result["files"]} == {"app.py", "tests/test_app.py"}
