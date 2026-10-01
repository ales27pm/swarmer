from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import check_harness
import project_worker as worker
import pytest
from jsonschema import Draft202012Validator
from project_contract import snapshot_sha
from test_project_read_progress import model_transport
from test_project_research import dependency, source
from test_project_worker import node_check, payload, step

BUILD = ["python", "-m", "compileall", "-q", "."]
PYTEST = ["python", "-m", "pytest", "-q"]


def bootstrap_payload() -> dict[str, Any]:
    html = (
        '<!doctype html><html lang="fr"><body><main id="tasks">\n'
        + "".join(
            f'<div data-task="{i}">Tâche {i}: préserver son état</div>\n'
            for i in range(150)
        )
        + "</main><script>localStorage.setItem('tasks', '[]');</script></body></html>\n"
    )
    files = [{"path": "index.html", "content": html}]
    objective = (
        "Préserver index.html. Créer tests/test_browser.py avec de vrais tests Selenium "
        "des filtres et de localStorage. Aucun fichier README supplémentaire."
    )
    return {
        **payload(),
        "objective": objective,
        "conversation": [{"role": "user", "content": objective}],
        "files": files,
        "iteration": 2,
        "base_revision_id": "revision_bootstrap",
        "base_sha256": snapshot_sha(files),
        "checks": [
            node_check(BUILD, "", code=1),
            node_check(PYTEST, "no tests ran in 0.00s", code=5),
        ],
        "durable_context": {
            "version": 1,
            "fingerprint": "a" * 64,
            "base_revision_id": "revision_bootstrap",
            "requirements": [{"text": objective, "source_id": "message_original"}],
        },
        "research_sources": [
            source(
                title="Selenium documentation",
                url="https://www.selenium.dev/documentation/webdriver/",
                snippet="Use the supplied browser and driver; verify actual DOM state.",
            )
        ],
        "dependency_context": [
            dependency(summary="The accepted snapshot contains index.html.")
        ],
    }


def creation_response() -> dict[str, Any]:
    return step(
        action="continue",
        runtime="python",
        edits=[
            {
                "path": "tests/test_browser.py",
                "content": "from pathlib import Path\n\ndef test_application_exists():\n    assert Path('index.html').is_file()\n",
            }
        ],
        patches=[],
        focus_paths=[],
    )


def request_for(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]
) -> dict[str, Any]:
    generator, requests = model_transport(monkeypatch, creation_response())
    generator.context_tokens = 64_000
    generator.prompt_max_bytes = 50_000
    generator.generate(data)
    assert len(requests) == 1
    assert requests[0]["options"]["num_ctx"] == 64_000
    assert requests[0]["options"]["num_predict"] == 2_000
    return requests[0]


def workspace(request: dict[str, Any]) -> dict[str, Any]:
    context, _ = json.JSONDecoder().raw_decode(
        request["messages"][-1]["content"].split("Current workspace data:\n", 1)[1]
    )
    return context


def request_sizes(request: dict[str, Any]) -> dict[str, int]:
    return {
        "prompt_bytes": sum(
            len(item["content"].encode()) for item in request["messages"]
        ),
        "schema_bytes": len(
            json.dumps(request["format"], separators=(",", ":")).encode()
        ),
    }


def test_html_only_build_failure_is_a_bootstrap_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    monkeypatch.setattr(check_harness, "PROJECT_ROOT", tmp_path)

    def unexpected_compilation(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a no-Python snapshot must not run lint or compilation")

    monkeypatch.setattr(check_harness.subprocess, "run", unexpected_compilation)
    monkeypatch.setattr(check_harness.py_compile, "compile", unexpected_compilation)
    assert check_harness.python_build() == {
        "exit_code": 1,
        "tests_executed": 0,
        "test_failures": 0,
    }


def test_bootstrap_no_python_and_empty_pytest_select_test_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = bootstrap_payload()
    original = copy.deepcopy(data)
    request = request_for(monkeypatch, data)
    context = workspace(request)
    validator = Draft202012Validator(request["format"])
    assert validator.is_valid(creation_response())
    html_only = creation_response()
    html_only["edits"] = [
        {"path": "index.html", "content": "<html>unrelated repair</html>"}
    ]
    assert not validator.is_valid(html_only), (
        "bootstrap must require a discoverable test file"
    )
    assert context["editable_spans"] == []
    assert "PATCH_TARGET " not in request["messages"][-1]["content"]
    assert context["checks"] == data["checks"], (
        "do not rewrite the failed build as success"
    )
    assert data == original


def test_bootstrap_creation_reduces_request_without_losing_requirements_or_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = bootstrap_payload()
    # Counterfactual ordinary-repair branch, with the exact same accepted input.
    # It provides a deterministic reference without a real inference request.
    with monkeypatch.context() as ordinary:
        ordinary.setattr(worker, "python_collected_no_tests", lambda check: False)
        baseline = request_for(ordinary, data)
    actual = request_for(monkeypatch, data)
    for request in (baseline, actual):
        context = workspace(request)
        message = request["messages"][-1]["content"]
        assert context["objective"] == data["objective"]
        assert context["durable_project_requirements"] == data["durable_context"]
        assert context["research_sources"] == data["research_sources"]
        assert context["dependency_context"] == data["dependency_context"]
        assert context["research_sources_omitted"] == 0
        assert context["dependency_context_omitted"] == 0
        assert data["files"][0]["content"] in message
        assert context["checks"] == data["checks"]
        assert request_sizes(request)["prompt_bytes"] <= 50_000
    measured = {
        "ordinary_repair": request_sizes(baseline),
        "test_creation": request_sizes(actual),
    }
    print(json.dumps(measured, sort_keys=True))
    assert (
        measured["test_creation"]["prompt_bytes"]
        < measured["ordinary_repair"]["prompt_bytes"]
    )
    assert (
        measured["test_creation"]["schema_bytes"]
        < measured["ordinary_repair"]["schema_bytes"]
    )
    assert workspace(actual)["editable_spans"] == []


@pytest.mark.parametrize(
    "kind",
    [
        "syntax",
        "nested_python",
        "hidden_python",
        "python_directory",
        "pip",
        "exit_two",
        "other_command",
        "no_empty_pytest",
    ],
)
def test_other_failures_keep_repair_priority(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    data = bootstrap_payload()
    if kind in {"syntax", "nested_python", "hidden_python"}:
        data["files"].append(
            {
                "path": "app.py" if kind == "syntax" else "src/app.py",
                "content": "VALUE = (\n",
            }
        )
        data["checks"][0]["output"] = "SyntaxError: '(' was never closed"
    elif kind == "python_directory":
        data["files"].append({"path": "fixtures.py/index.html", "content": "<html></html>"})
    elif kind == "pip":
        data["checks"].append(
            node_check(
                ["python", "-m", "pip", "install", "-r", "requirements.txt"],
                "A dependency installation failed.",
                code=1,
            )
        )
    elif kind == "exit_two":
        data["checks"][0]["exit_code"] = 2
    elif kind == "other_command":
        data["checks"][0]["command"] = ["python", "-m", "compileall", "."]
    else:
        data["checks"][1]["output"] = "collection failed before running tests"
        data["checks"][1]["exit_code"] = 2
    data["base_sha256"] = snapshot_sha(data["files"])
    if kind == "hidden_python":
        original_context = worker.model_context

        def omit_python_source(*args: Any, **kwargs: Any) -> dict[str, Any]:
            context = original_context(*args, **kwargs)
            for field in ("selected_complete_files", "selected_file_fragments"):
                context[field] = [
                    item for item in context[field] if not item["path"].endswith(".py")
                ]
            return context

        monkeypatch.setattr(worker, "model_context", omit_python_source)
    request = request_for(monkeypatch, data)
    if kind == "hidden_python":
        assert "VALUE = (\n" not in request["messages"][-1]["content"]
        assert any(
            item["path"].endswith(".py") for item in workspace(request)["file_manifest"]
        )
    assert workspace(request)["editable_spans"], (
        "actual repairs still need source patch targets"
    )
    repair = creation_response()
    repair["edits"] = [{"path": "index.html", "content": "<html>repair</html>"}]
    assert Draft202012Validator(request["format"]).is_valid(repair)
