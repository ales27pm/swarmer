"""A compact retry must retain a current, actionable source target."""

import copy
import json

import project_worker as worker
import pytest
from jsonschema import Draft202012Validator
from test_project_model_budget import capture


def diagnostic(path="tests/test_browser.py", line=40):
    return (
        f"browser_sandbox_disabled: {path}:{line}. "
        "Static browser preflight blocked execution; no tests ran. "
        "Remove --no-sandbox and keep Chromium sandbox enabled."
    )


def repair_payload():
    source = (
        "from selenium import webdriver\n\ndef test_browser():\n"
        + "    # Preserve the existing fixture lifecycle.\n" * 35
        + "    options = webdriver.ChromeOptions()\n"
        + "    options.add_argument('--no-sandbox')\n"
        + "    assert options is not None\n"
        + "# Existing test module context.\n" * 150
    )
    assert source.splitlines()[39] == "    options.add_argument('--no-sandbox')"
    return {
        "objective": "Preserve the existing HTML and repair the browser test only.",
        "conversation": [
            {"role": "user", "content": "Keep every assertion and Chromium sandbox enabled."},
            {"role": "assistant", "content": worker.MODEL_TIMEOUT_DIAGNOSTIC},
        ],
        "files": [
            {"path": "index.html", "content": "<!doctype html>\n" + "<!-- preserve -->\n" * 200},
            {"path": "requirements.txt", "content": "Flask==3.1.3"},
            {"path": "tests/test_browser.py", "content": source},
        ],
        "plan": ["Keep accepted behavior", "Verify with real browser tests"],
        "checks": [
            {
                "command": ["swarmer", "project-checks"],
                "status": "failed",
                "exit_code": 1,
                "duration_ms": 0,
                "output": diagnostic(),
            }
        ],
        "iteration": 7,
        "base_revision_id": "revision_current",
        "base_sha256": "a" * 64,
        "durable_context": {
            "version": 1,
            "fingerprint": "b" * 64,
            "base_revision_id": "revision_current",
            "requirements": [
                {"source_id": f"message_{i}", "text": f"Requirement {i}: " + "é" * 3900}
                for i in range(2)
            ],
        },
    }


@pytest.mark.parametrize("focused", [False, True])
def test_compact_budget_reserves_exact_diagnostic_source_and_resolvable_patch(monkeypatch, focused):
    data = repair_payload()
    if focused:
        data["focus_paths"] = ["tests/test_browser.py"]
    before = copy.deepcopy(data)
    generator = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1",
        "local-model",
        context_tokens=64_000,
        prompt_max_bytes=50_000,
    )
    generator.runtime_instruction = "Runtime facts: " + "x" * 970
    body = capture(monkeypatch, generator, data)
    assert data == before
    size = sum(len(message["content"].encode()) for message in body["messages"])
    assert 10_000 < size <= 50_000
    assert body["options"]["num_predict"] == 512
    assert data["conversation"][0] in body["messages"]
    workspace = body["messages"][-1]["content"]
    metadata, _ = json.JSONDecoder().raw_decode(workspace.removeprefix("Current workspace data:\n"))
    assert metadata["durable_project_requirements"] == data["durable_context"]
    assert workspace.count("SOURCE ") == 1
    assert 'SOURCE {"path":"tests/test_browser.py"' in workspace
    assert "options.add_argument('--no-sandbox')" in workspace
    assert len(metadata["editable_spans"]) == 1
    address = metadata["editable_spans"][0]
    assert address["path"] == "tests/test_browser.py"
    original = data["files"][-1]["content"]
    old = original[address["start_character"] : address["end_character"]]
    assert old == "    options.add_argument('--no-sandbox')\n"
    patch = {"path": address["path"], "span_id": address["span_id"], "new": ""}
    step = {
        "action": "continue",
        "message": "Remove the forbidden flag.",
        "edits": [],
        "patches": [patch],
        "focus_paths": [],
        "runtime": "python",
        "run_instructions": "python -m pytest -q",
    }
    assert Draft202012Validator(body["format"]).is_valid(step)
    resolved = worker.resolve_model_patches(
        step,
        {address["span_id"]: {**address, "old": old}},
    )
    assert resolved["patches"] == [{"path": address["path"], "old": old, "new": ""}]
    changed = worker.merge_files(data["files"], worker.expand_compact_repair(resolved, data))
    assert changed[-1]["content"] == original.replace(old, "", 1)
    assert changed[:2] == data["files"][:2]
    with pytest.raises(worker.ProjectError, match="unknown, stale"):
        worker.resolve_model_patches(step, {})


@pytest.mark.parametrize(
    "path",
    [
        "other/tests/test_browser.py",
        "/workspace/other/tests/test_browser.py",
        "/dependencies/tests/test_browser.py",
        "mytests/test_browser.py",
        "tests/test_browser.py.old",
        "./tests/test_browser.py",
        "tests/test_browser.py/../other.py",
    ],
)
def test_browser_policy_location_requires_exact_accepted_path(path):
    assert worker.project_traceback_line("tests/test_browser.py", diagnostic(path)) is None


@pytest.mark.parametrize(
    "text",
    [
        diagnostic(line=0),
        diagnostic().replace(":40.", ":40oops."),
        "prefix " + diagnostic(),
        diagnostic().replace("no tests ran.", "tests passed."),
    ],
)
def test_browser_policy_location_requires_exact_fixed_diagnostic(text):
    assert worker.project_traceback_line("tests/test_browser.py", text) is None


@pytest.mark.parametrize("context_tokens,prompt_max_bytes", [(32_768, 22_000), (64_000, 50_000)])
def test_required_metadata_and_repair_source_over_hard_limit_fail_before_transport(
    monkeypatch,
    context_tokens,
    prompt_max_bytes,
):
    data = repair_payload()
    data["durable_context"]["requirements"] = [
        {"source_id": f"message_{i}", "text": str(i) * 9900} for i in range(3)
    ]
    data["objective"] = "Preserve " + "x" * 3990
    data["conversation"][0]["content"] = "界" * 3999
    generator = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1",
        "local-model",
        context_tokens=context_tokens,
        prompt_max_bytes=prompt_max_bytes,
    )
    monkeypatch.setattr(
        worker.urllib.request, "build_opener", lambda *_: pytest.fail("no transport")
    )
    before = copy.deepcopy(data)
    with pytest.raises(worker.ProjectError, match="context budget"):
        generator.generate(data)
    assert data == before
