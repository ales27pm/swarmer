from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.project_contracts import ProjectResult
from app.services.project_execution_contracts import ProjectExecutionReceipt


def measured_project():
    files = [{"path": "app.py", "content": "value=1\n"}]
    digest = hashlib.sha256(b'[{"content":"value=1\\n","path":"app.py"}]').hexdigest()
    profiles = ["python_build", "python_test"]
    commands = [["python", "-m", "compileall", "-q", "."], ["python", "-m", "pytest", "-q"]]
    observation = {
        "schema_version": "project-check-observation-v1",
        "source_before_sha256": digest,
        "source_after_sha256": digest,
        "workspace_before_sha256": "b" * 64,
        "workspace_after_sha256": "b" * 64,
        "dependency_before_sha256": "c" * 64,
        "dependency_after_sha256": "c" * 64,
        "harness_sha256": "d" * 64,
        "source_unchanged": True,
        "environment_unchanged": True,
        "errors": [],
    }
    receipt = {
        "schema_version": "project-execution-receipt-v1",
        "origin": "worker_reported_measurement",
        "run_id": "1" * 32,
        "runtime": "python",
        "source_sha256": digest,
        "runtime_image_id": "sha256:" + "a" * 64,
        "runner_sha256": "e" * 64,
        "policy_sha256": "f" * 64,
        "profiles_expected": profiles,
        "profiles": [
            {
                "profile": profile,
                "check_index": index,
                "exit_code": 0,
                "tests_executed": index,
                "test_failures": 0,
                "duration_ms": 3,
                "observation": dict(observation),
                "measurement_error": None,
            }
            for index, profile in enumerate(profiles)
        ],
        "observation_status": "complete",
        "incomplete_reasons": [],
    }
    return {
        "schema_version": "1.0",
        "action": "complete",
        "message": "Observed checks.",
        "files": files,
        "plan": ["Check the source"],
        "run_instructions": "python -m pytest -q",
        "runtime": "python",
        "base_revision_id": None,
        "base_sha256": None,
        "checks": [
            {
                "command": command,
                "status": "passed",
                "exit_code": 0,
                "duration_ms": 3,
                "output": "synthetic private output",
            }
            for command in commands
        ],
        "execution_receipt": receipt,
    }


def test_legacy_project_roundtrip_omits_unavailable_execution_measurement():
    value = measured_project()
    del value["execution_receipt"]
    result = ProjectResult.model_validate(value)
    assert "execution_receipt" not in result.model_dump()
    assert "execution_receipt" not in result.model_dump_json()
    assert result.checks[1].status == "passed"


def test_typed_measurement_roundtrips_as_worker_report_without_promotion():
    value = measured_project()
    result = ProjectResult.model_validate(value)
    assert result.model_dump()["execution_receipt"] == value["execution_receipt"]
    assert result.execution_receipt.origin == "worker_reported_measurement"
    assert "synthetic private output" not in result.execution_receipt.model_dump_json()
    assert len(result.execution_receipt.model_dump_json().encode()) < 16_384
    schema = ProjectResult.model_json_schema()
    assert "ProjectExecutionReceipt" in schema["$defs"]
    assert schema["$defs"]["ProjectExecutionReceipt"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["files"][0].update(content="value=2\n"),
        lambda r: r.update(runtime="node"),
        lambda r: r.update(action="clarify"),
        lambda r: r.update(action="continue", focus_paths=["app.py"]),
        lambda r: r["checks"][1].update(duration_ms=4),
        lambda r: r["checks"][1].update(command=["npm", "run", "build"]),
        lambda r: r["execution_receipt"].update(origin="trusted"),
        lambda r: r["execution_receipt"]["profiles"][1].update(tests_executed=True),
        lambda r: r["execution_receipt"]["profiles"][1].update(check_index=11),
        lambda r: r["execution_receipt"]["profiles"][0]["observation"].update(
            source_unchanged=False
        ),
    ],
)
def test_project_refuses_mismatched_receipt_and_false_binding(change):
    value = measured_project()
    change(value)
    with pytest.raises(ValidationError):
        ProjectResult.model_validate(value)


def test_incomplete_environment_remains_explicitly_incomplete():
    receipt = measured_project()["execution_receipt"]
    for profile in receipt["profiles"]:
        profile["observation"].update(
            dependency_before_sha256=None,
            dependency_after_sha256=None,
            environment_unchanged=None,
            errors=["environment_unbound"],
        )
    receipt.update(observation_status="incomplete", incomplete_reasons=["environment_unbound"])
    assert ProjectExecutionReceipt.model_validate(receipt).observation_status == "incomplete"
    receipt["observation_status"] = "complete"
    with pytest.raises(ValidationError):
        ProjectExecutionReceipt.model_validate(receipt)


def test_standalone_worker_and_server_receipt_guards_are_exact_mirrors():
    root = Path(__file__).resolve().parents[2]
    assert (root / "workers/project-worker/project_execution.py").read_bytes() == (
        root / "server/app/services/project_execution_receipts.py"
    ).read_bytes()
