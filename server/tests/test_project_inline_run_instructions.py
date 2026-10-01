from __future__ import annotations

import copy
from typing import Any

import pytest

from app.services.project_contracts import ProjectResult


def completed_result() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "action": "complete",
        "message": "The requested files and measured checks are ready for review.",
        "plan": ["Implement addition", "Verify addition"],
        "files": [
            {"path": "app.py", "content": "def add(left, right):\n    return left + right\n"},
            {
                "path": "tests/test_app.py",
                "content": "from app import add\ndef test_add():\n    assert add(2, 3) == 5\n",
            },
        ],
        "checks": [
            {
                "command": ["python", "-m", "pytest", "-q"],
                "status": "passed",
                "exit_code": 0,
                "output": '1 passed\nSWARMER_RUNNER_RECEIPT={"exit_code":0,"tests_executed":1,"test_failures":0}',
                "duration_ms": 20,
            }
        ],
        "run_instructions": "Run python -m pytest -q from the project directory.",
        "runtime": "python",
        "base_revision_id": None,
        "base_sha256": None,
    }


def test_completion_uses_inline_instructions_without_adding_unrequested_readme() -> None:
    value = completed_result()
    original = copy.deepcopy(value)
    result = ProjectResult.model_validate(value)
    assert result.action == "complete"
    assert {file.path for file in result.files} == {"app.py", "tests/test_app.py"}
    assert result.run_instructions == value["run_instructions"]
    assert result.checks[0].status == "passed" and result.checks[0].exit_code == 0
    assert value == original


@pytest.mark.parametrize("instructions", ["", " ", "\n\t"])
def test_inline_completion_still_requires_nonblank_run_instructions(instructions: str) -> None:
    value = completed_result()
    value["run_instructions"] = instructions
    with pytest.raises(ValueError, match="run instructions"):
        ProjectResult.model_validate(value)


@pytest.mark.parametrize("status,exit_code", [("failed", 1), ("failed", 5), ("skipped", None)])
def test_inline_completion_still_rejects_failed_or_unexecuted_checks(
    status: str, exit_code: int | None
) -> None:
    value = completed_result()
    value["checks"][0].update(status=status, exit_code=exit_code, output="No successful test run.")
    with pytest.raises(ValueError, match="successful executed checks"):
        ProjectResult.model_validate(value)


def test_inline_completion_still_rejects_absent_checks() -> None:
    value = completed_result()
    value["checks"] = []
    with pytest.raises(ValueError, match="successful executed checks"):
        ProjectResult.model_validate(value)


def test_inline_completion_still_requires_a_test_not_only_compilation() -> None:
    value = completed_result()
    value["checks"][0].update(command=["python", "-m", "compileall", "-q", "."], output="")
    with pytest.raises(ValueError, match="executed test command"):
        ProjectResult.model_validate(value)
