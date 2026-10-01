"""A compact retry must retain a current, actionable source target."""

import copy
import json

import project_worker as worker
import pytest
from jsonschema import Draft202012Validator
from test_project_model_budget import capture
from test_project_read_progress import model_transport
from test_project_worker import Runner, compact_step


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


def after_compact_read(data, path="tests/test_browser.py"):
    data["focus_paths"] = [path]
    data["conversation"].append(
        {
            "role": "assistant",
            "content": f"Files requested for reading: {path}. No project files changed.",
        }
    )
    return data


def generator_64k():
    generator = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1", "local-model", context_tokens=64_000, prompt_max_bytes=50_000
    )
    generator.runtime_instruction = "x" * 992
    return generator


@pytest.mark.parametrize("after_read", [False, True])
def test_small_diagnostic_file_is_complete_in_compact_recovery_without_expanding_context(
    monkeypatch, after_read
):
    data = repair_payload()
    data["files"][-1]["content"] = "".join(data["files"][-1]["content"].splitlines(True)[:60])
    if after_read:
        after_compact_read(data)
    original = copy.deepcopy(data)
    generator = generator_64k()
    body = capture(monkeypatch, generator, data)
    workspace = body["messages"][-1]["content"]
    metadata, _ = json.JSONDecoder().raw_decode(workspace.removeprefix("Current workspace data:\n"))
    source = data["files"][-1]

    assert body["options"]["num_predict"] == 512
    assert 10_000 < sum(len(message["content"].encode()) for message in body["messages"]) < 28_000
    assert metadata["durable_project_requirements"] == data["durable_context"]
    assert data["conversation"][0] in body["messages"]
    assert workspace.count("SOURCE ") == 1
    assert source["content"] in workspace
    assert 'SOURCE {"path":"tests/test_browser.py","complete":true' in workspace
    assert generator.last_visible_paths == {source["path"]}
    assert len(metadata["editable_spans"]) == 1
    address = metadata["editable_spans"][0]
    assert address["path"] == source["path"] and address["start_line"] == 40
    assert source["content"][address["start_character"] : address["end_character"]] == (
        "    options.add_argument('--no-sandbox')\n"
    )
    assert data == original


@pytest.mark.parametrize("after_read", [False, True])
def test_small_complete_recovery_source_cannot_be_requested_again(monkeypatch, after_read):
    data = repair_payload()
    if after_read:
        after_compact_read(data)
    generator, requests = model_transport(
        monkeypatch, compact_step(edits=[], focus_paths=["tests/test_browser.py"])
    )
    generator.context_tokens, generator.prompt_max_bytes = 64_000, 50_000
    runner = Runner()

    result = worker.run_iteration(data, generator, runner, lambda: None)

    assert len(requests) == 1 and runner.calls == 0
    assert requests[0]["options"]["num_predict"] == 512
    assert result["message"] == worker.REDUNDANT_READ_DIAGNOSTIC
    assert result["focus_paths"] == []
    assert result["files"] == data["files"] and result["checks"] == data["checks"]


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_recovery_complete_source_ceiling_is_utf8_bytes(monkeypatch, extra_bytes):
    data = after_compact_read(repair_payload())
    source = "".join(data["files"][-1]["content"].splitlines(True)[:42])
    count = worker.MAX_SPAN_BYTES + extra_bytes - len(source.encode()) - len("# é\n".encode())
    data["files"][-1]["content"] = source + "# é" + "x" * count + "\n"
    generator = generator_64k()
    body = capture(monkeypatch, generator, data)
    workspace = body["messages"][-1]["content"]
    assert body["options"]["num_predict"] == 512
    assert "options.add_argument('--no-sandbox')" in workspace
    assert ("tests/test_browser.py" in generator.last_visible_paths) == (extra_bytes == 0)
    if extra_bytes:
        assert 'SOURCE {"path":"tests/test_browser.py","complete":false' in workspace
        assert data["files"][-1]["content"] not in workspace
    assert sum(len(message["content"].encode()) for message in body["messages"]) <= 50_000


def test_compact_read_can_supply_a_different_existing_dependency_file(monkeypatch):
    data = after_compact_read(repair_payload(), "requirements.txt")
    generator = generator_64k()
    body = capture(monkeypatch, generator, data)
    workspace = body["messages"][-1]["content"]
    assert body["options"]["num_predict"] == 512
    assert generator.last_visible_paths == {"requirements.txt"}
    assert workspace.count("SOURCE ") == 1 and "Flask==3.1.3" in workspace


@pytest.mark.parametrize(
    "change",
    ["new_user", "wrong_message", "ordinary_history", "foreign_focus", "multiple_focus", "passed"],
)
def test_compact_read_does_not_reuse_unrelated_or_unproven_recovery(monkeypatch, change):
    data = after_compact_read(repair_payload())
    if change == "new_user":
        data["conversation"].append({"role": "user", "content": "Implement a new export module."})
    elif change == "wrong_message":
        data["conversation"][-1]["content"] += " Continue."
    elif change == "ordinary_history":
        data["conversation"][-2]["content"] = "A historical repair was attempted."
    elif change == "foreign_focus":
        data["focus_paths"] = ["other/tests/test_browser.py"]
    elif change == "multiple_focus":
        data["focus_paths"].append("requirements.txt")
    else:
        data["checks"][0].update(status="passed", exit_code=0)
    body = capture(monkeypatch, generator_64k(), data)
    assert body["options"]["num_predict"] == 2000


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
