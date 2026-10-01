from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from test_project_read_progress import model_transport
from test_project_worker import node_check, payload, step


@pytest.mark.parametrize(
    "failed_command,diagnostic,repair_path,repair_content",
    [
        (
            ["python", "-m", "compileall", "-q", "."],
            "app.py: SyntaxError: ( was never closed",
            "app.py",
            "VALUE = 7\n",
        ),
        (
            ["python", "-m", "pip", "install", "-r", "requirements.txt"],
            "The required package version is unavailable.",
            "requirements.txt",
            "flask==3.1.3\n",
        ),
    ],
)
def test_empty_pytest_does_not_block_other_measured_failure_repair(
    monkeypatch: pytest.MonkeyPatch,
    failed_command: list[str],
    diagnostic: str,
    repair_path: str,
    repair_content: str,
) -> None:
    data = {
        **payload(),
        "objective": "Repair the actual build/dependency failure before creating real tests.",
        "files": [{"path": "app.py", "content": "VALUE = (\n"}],
        "checks": [
            node_check(failed_command, diagnostic, code=1),
            node_check(["python", "-m", "pytest", "-q"], "no tests ran in 0.00s", code=5),
        ],
    }
    response = step(
        action="continue",
        runtime="python",
        edits=[{"path": repair_path, "content": repair_content}],
        patches=[],
        focus_paths=[],
    )
    generator, requests = model_transport(monkeypatch, response)
    actual = generator.generate(data)
    assert actual["edits"] == response["edits"]
    assert len(requests) == 1
    assert Draft202012Validator(requests[0]["format"]).is_valid(response)
    context, _ = json.JSONDecoder().raw_decode(
        requests[0]["messages"][-1]["content"].split("Current workspace data:\n", 1)[1]
    )
    assert context["editable_spans"], "other application repairs still need their patch spans"
    source_headers = [
        json.loads(line.removeprefix("SOURCE "))
        for line in requests[0]["messages"][-1]["content"].splitlines()
        if line.startswith("SOURCE ")
    ]
    assert any(item["path"] == "app.py" and item["complete"] for item in source_headers)
    assert "VALUE = (\n" in requests[0]["messages"][-1]["content"]
