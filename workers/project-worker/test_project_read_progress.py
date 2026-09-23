from __future__ import annotations

import copy
import io
import json
from typing import Any

import project_worker as worker
import pytest
from jsonschema import Draft202012Validator
from project_contract import snapshot_sha
from test_project_worker import Runner, compact_step, payload, step

READ_DIAGNOSTIC = (
    "The model requested files already fully visible in the current prompt. "
    "No changes or checks were accepted. Use the supplied source to make an effective "
    "edit or patch, request checks explicitly, or read an omitted or partial file."
)


def calculator_payload(iteration: int = 2) -> dict[str, Any]:
    objective = "Fix addition in src/calculator.py; preserve the existing tests."
    files = [
        {
            "path": "AGENTS.md",
            "content": "Only change src/calculator.py. Preserve the existing tests.\n",
        },
        {"path": "src/AGENTS.md", "content": "add(left, right) must return left + right.\n"},
        {
            "path": "src/calculator.py",
            "content": "def add(left, right):\n    return left - right\n",
        },
        {
            "path": "tests/test_calculator.py",
            "content": "from src.calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        },
    ]
    return {
        **payload(),
        "guidance_version": 1,
        "objective": objective,
        "conversation": [
            {"role": "user", "content": objective},
            {
                "role": "assistant",
                "content": "Files requested for reading: src/calculator.py. No project files changed.",
            },
        ],
        "files": files,
        "plan": ["Repair addition", "Run existing tests"],
        "iteration": iteration,
        "base_revision_id": "revision_calculator",
        "base_sha256": snapshot_sha(files),
        "focus_paths": ["src/calculator.py"],
    }


def model_transport(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> tuple[worker.ProjectGenerator, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

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
    return worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b"), requests


@pytest.mark.parametrize("iteration", [2, 3])
def test_fully_shown_calculator_cannot_charge_another_read(
    monkeypatch: pytest.MonkeyPatch, iteration: int
) -> None:
    data = calculator_payload(iteration)
    original = copy.deepcopy(data)
    read = step(action="continue", edits=[], patches=[], focus_paths=["src/calculator.py"])
    generator, requests = model_transport(monkeypatch, read)
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)

    assert len(requests) == 1 and runner.calls == 0
    workspace = requests[0]["messages"][-1]["content"]
    assert "def add(left, right):\n    return left - right\n" in workspace
    assert {item["path"] for item in generator.last_guidance_reads} == {
        "AGENTS.md",
        "src/AGENTS.md",
    }
    assert not Draft202012Validator(requests[0]["format"]).is_valid(read)
    assert result["focus_paths"] == [] and result["action"] == "continue"
    assert result["message"] == READ_DIAGNOSTIC
    assert snapshot_sha(result["files"]) == snapshot_sha(data["files"])
    assert result["checks"] == data["checks"]
    assert result["plan"] == data["plan"]
    assert result["base_revision_id"] == data["base_revision_id"]
    assert result["base_sha256"] == data["base_sha256"]
    assert data == original


@pytest.mark.parametrize("path", ["AGENTS.md", "src/AGENTS.md", "src/calculator.py"])
def test_complete_guidance_and_source_are_not_readable_even_after_a_failed_check(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    data = calculator_payload()
    data["checks"] = Runner(fail=True).run(data["files"])["checks"]
    read = step(action="continue", edits=[], patches=[], plan=data["plan"], focus_paths=[path])
    generator, requests = model_transport(monkeypatch, read)
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert len(requests) == 1 and runner.calls == 0
    assert result["files"] == data["files"] and result["checks"] == data["checks"]
    assert result["message"] == READ_DIAGNOSTIC
    assert not Draft202012Validator(requests[0]["format"]).is_valid(read)


@pytest.mark.parametrize("kind", ["omitted", "partial", "unloaded_guidance"])
def test_reads_that_supply_missing_source_remain_available(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    data = calculator_payload()
    if kind == "unloaded_guidance":
        path = "other/AGENTS.md"
        data["files"].append({"path": path, "content": "Preserve public APIs.\n"})
    else:
        path = "other.py"
        data["files"].append({"path": path, "content": "# source context\n" * 3_000})
        if kind == "partial":
            data["focus_paths"].append(path)
    data["base_sha256"] = snapshot_sha(data["files"])
    read = step(action="continue", edits=[], patches=[], focus_paths=[path])
    generator, requests = model_transport(monkeypatch, read)
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert result["focus_paths"] == [path] and path not in generator.last_visible_paths
    assert len(requests) == 1 and runner.calls == 0
    assert snapshot_sha(result["files"]) == snapshot_sha(data["files"])
    assert result["checks"] == data["checks"]
    validator = Draft202012Validator(requests[0]["format"])
    assert validator.is_valid(read)
    assert not validator.is_valid({**read, "focus_paths": [path, "src/calculator.py"]})
    if kind == "partial":
        assert '"path":"other.py"' in requests[0]["messages"][-1]["content"]


def test_a_labelled_fragment_containing_all_source_is_already_read() -> None:
    data = calculator_payload()
    item = data["files"][2]
    fragment = worker.source_fragment(item, data, "")
    assert fragment["complete"] is False and fragment["content"] == item["content"]
    context = {"selected_complete_files": [], "selected_file_fragments": [fragment]}
    assert worker.fully_visible_paths(context, data) == {item["path"]}


def test_read_eligibility_uses_final_prompt_after_source_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = calculator_payload()
    large = {"path": "large.py", "content": "# more source context\n" * 2_000}
    data["files"].append(large)
    data["focus_paths"].append(large["path"])
    data["base_sha256"] = snapshot_sha(data["files"])
    context = worker.model_context(data)
    # Force the first selection to include the full source. The final request
    # must trim it to a fragment while retaining the right to read more later.
    context["selected_complete_files"].append(large)
    context["selected_file_fragments"] = []
    monkeypatch.setattr(worker, "model_context", lambda value: context)
    read = step(action="continue", edits=[], patches=[], focus_paths=[large["path"]])
    generator, requests = model_transport(monkeypatch, read)
    result = worker.run_iteration(data, generator, Runner(), lambda: None)
    assert len(requests) == 1 and result["focus_paths"] == [large["path"]]
    assert large["path"] not in generator.last_visible_paths
    assert Draft202012Validator(requests[0]["format"]).is_valid(read)
    workspace = requests[0]["messages"][-1]["content"]
    assert 'SOURCE {"path":"large.py","complete":false' in workspace
    assert sum(len(item["content"].encode()) for item in requests[0]["messages"]) <= 22_000


def test_compact_recovery_rejects_a_redundant_read_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = calculator_payload()
    data["checks"] = Runner(fail=True).run(data["files"])["checks"]
    data["conversation"].append({"role": "assistant", "content": worker.MODEL_TIMEOUT_DIAGNOSTIC})
    read = compact_step(edits=[], focus_paths=["src/calculator.py"])
    generator, requests = model_transport(monkeypatch, read)
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert len(requests) == 1 and runner.calls == 0
    assert requests[0]["options"]["num_predict"] == 512
    assert not Draft202012Validator(requests[0]["format"]).is_valid(read)
    assert result["message"] == READ_DIAGNOSTIC and result["focus_paths"] == []
    assert result["files"] == data["files"] and result["plan"] == data["plan"]


@pytest.mark.parametrize("mode", ["edit", "patch"])
def test_shown_calculator_can_be_repaired_with_revision_bound_guidance(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    data = calculator_payload(3)
    original = copy.deepcopy(data)
    item = data["files"][2]
    fixed = {**item, "content": item["content"].replace("left - right", "left + right")}
    response = step(action="continue", edits=[fixed], patches=[], focus_paths=[])
    if mode == "patch":
        addresses = worker.addressed_patch_spans(worker.model_context(data), data)
        identifier, target = next(
            (identifier, target)
            for identifier, target in addresses.items()
            if target["path"] == item["path"] and "left - right" in target["old"]
        )
        response.update(
            edits=[],
            patches=[
                {
                    "path": item["path"],
                    "span_id": identifier,
                    "new": target["old"].replace("left - right", "left + right"),
                }
            ],
        )
    generator, requests = model_transport(monkeypatch, response)
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert len(requests) == 1 and runner.calls == 1
    assert Draft202012Validator(requests[0]["format"]).is_valid(response)
    assert [item for item in result["files"] if item["path"] == fixed["path"]] == [fixed]
    assert [item for item in result["files"] if item["path"] != fixed["path"]] == [
        item for item in data["files"] if item["path"] != fixed["path"]
    ]
    assert result["guidance_reads"] == generator.last_guidance_reads
    assert result["base_sha256"] == data["base_sha256"]
    assert result["checks"] and runner.files == result["files"]
    assert data == original
