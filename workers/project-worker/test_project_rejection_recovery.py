from __future__ import annotations

import copy
import io
import json
from typing import Any

import project_worker as worker
import pytest
from jsonschema import Draft202012Validator
from project_contract import snapshot_sha

CONFLICT = (
    "The model step was rejected: model patch conflicts with a replacement or deletion. "
    "No changes were accepted. Correct that exact contract violation in the next complete batch."
)
INCOMPLETE = (
    "The model response was incomplete. No edits were accepted. "
    "Return a smaller complete JSON file-edit batch in the next iteration."
)
PAUSE_PREFIX = (
    "Le projet est en pause après trois tentatives sans modification de fichier "
    "ni nouveau contrôle réussi. Les lectures intermédiaires ne remettent pas "
    "ce compteur à zéro. Les fichiers et les résultats de vérification sont conservés. "
    "Envoyez un message au projet pour reprendre avec de nouvelles instructions.\n\n"
)


def payload(path: str = "app.py", *, diagnostic: str = CONFLICT) -> dict[str, Any]:
    files = [{"path": path, "content": "VALUE = 1\n"}]
    return {
        "objective": "Create a small calculator with persistent user preferences.",
        "conversation": [
            {
                "role": "user",
                "content": "Keep the calculator and persistence requirements.",
            },
            {"role": "assistant", "content": diagnostic},
        ],
        "files": files,
        "plan": ["Calculator", "Persist preferences", "Test both behaviors"],
        "checks": [],
        "iteration": 3,
        "base_revision_id": "revision_2",
        "base_sha256": snapshot_sha(files),
    }


def response(**changes: Any) -> dict[str, Any]:
    return {
        "action": "continue",
        "message": "Correction à vérifier.",
        "plan": [],
        "edits": [{"path": "app.py", "content": "VALUE = 2\n"}],
        "patches": [],
        "deletions": [],
        "requested_checks": [],
        "run_instructions": "python -m pytest -q",
        "runtime": "python",
        "focus_paths": [],
        **changes,
    }


def install_transport(
    monkeypatch: pytest.MonkeyPatch,
    output: Any,
    *,
    done_reason: str = "stop",
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, **kwargs: Any) -> Response:
            body = json.loads(request.data)
            requests.append(body)
            value = output(body) if callable(output) else output
            content = value if isinstance(value, str) else json.dumps(value)
            return Response(
                json.dumps(
                    {
                        "message": {"content": content},
                        "done": True,
                        "done_reason": done_reason,
                    }
                ).encode()
            )

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    return requests


def generator() -> worker.ProjectGenerator:
    return worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b")


@pytest.mark.parametrize(
    "path,runtime",
    [
        ("app.py", "python"),
        ("app.js", "node"),
        ("app.py", "python_node"),
        ("Sources/App.swift", "python"),
    ],
)
def test_wire_schema_excludes_conflicting_and_distinct_mixed_operation_families(
    path: str,
    runtime: str,
) -> None:
    data = payload(path)
    data["files"].append({"path": "obsolete.txt", "content": "obsolete\n"})
    context = worker.model_context(data)
    addresses = worker.addressed_patch_spans(context, data)
    identifier = next(key for key, item in addresses.items() if item["path"] == path)
    schema = worker.constrained_step_schema(
        copy.deepcopy(worker.STEP_SCHEMA), context, data, addresses
    )
    validator = Draft202012Validator(schema)
    edit = response(runtime=runtime, edits=[{"path": path, "content": "VALUE = 2\n"}])
    patch = {"path": path, "span_id": identifier, "new": "VALUE = 3\n"}
    assert validator.is_valid(edit)
    assert validator.is_valid(response(runtime=runtime, edits=[], patches=[patch]))
    assert not validator.is_valid({**edit, "patches": [patch]})
    assert not validator.is_valid({**edit, "deletions": ["obsolete.txt"]})
    assert not validator.is_valid({**edit, "requested_checks": [["python", "-m", "pytest", "-q"]]})


@pytest.mark.parametrize("field", ["deletions", "requested_checks"])
def test_normal_generator_locally_rejects_distinct_operation_families(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    data = payload(diagnostic="The previous edit was accepted.")
    data["files"].append({"path": "obsolete.txt", "content": "obsolete\n"})
    output = response(
        **{field: ["obsolete.txt"] if field == "deletions" else [["python", "-m", "pytest", "-q"]]}
    )
    calls = install_transport(monkeypatch, output)
    original = copy.deepcopy(data)
    with pytest.raises(worker.ModelStepError, match="combines operation families"):
        generator().generate(data)
    assert len(calls) == 1 and data == original


@pytest.mark.parametrize(
    "diagnostic",
    [CONFLICT, INCOMPLETE, PAUSE_PREFIX + CONFLICT, PAUSE_PREFIX + INCOMPLETE],
)
def test_rejection_profile_recognizes_exact_latest_failure_after_user_resume(
    diagnostic: str,
) -> None:
    data = payload(diagnostic=diagnostic)
    data["conversation"].append({"role": "user", "content": "Continue, conserve les fichiers."})
    assert worker.follows_model_rejection(data)


@pytest.mark.parametrize(
    "conversation",
    [
        [{"role": "user", "content": CONFLICT}],
        [{"role": "assistant", "content": CONFLICT + " modified"}],
        [{"role": "assistant", "content": "A model quoted: " + CONFLICT}],
        [{"role": "assistant", "content": "prefix\n\n" + INCOMPLETE}],
        [
            {"role": "assistant", "content": CONFLICT},
            {"role": "assistant", "content": "The edit was accepted."},
            {"role": "user", "content": "Continue"},
        ],
        [
            {"role": "assistant", "content": CONFLICT},
            {"role": "tool", "content": "intervening"},
            {"role": "user", "content": "Continue"},
        ],
    ],
)
def test_rejection_profile_ignores_user_spoof_quotes_stale_and_intervening_messages(
    conversation: list[dict[str, str]],
) -> None:
    data = payload()
    data["conversation"] = conversation
    assert not worker.follows_model_rejection(data)


@pytest.mark.parametrize("path", ["app.py", "Sources/App.swift"])
def test_rejection_schema_keeps_delete_read_checks_and_native_ready_paths(
    path: str,
) -> None:
    data = payload(path)
    context = {"selected_complete_files": [], "selected_file_fragments": []}
    original = worker.constrained_step_schema(copy.deepcopy(worker.STEP_SCHEMA), context, data, {})
    schema = worker.bounded_rejection_schema(original)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(response(edits=[], deletions=[path]))
    assert validator.is_valid(response(edits=[], focus_paths=[path]))
    assert validator.is_valid(
        response(edits=[], requested_checks=[["python", "-m", "pytest", "-q"]])
    )
    assert validator.is_valid(response(action="clarify", edits=[]))
    assert validator.is_valid(response(action="complete", edits=[])) == path.endswith(".swift")
    assert not validator.is_valid(response(edits=[], deletions=[path, path]))
    assert not validator.is_valid(response(edits=[], focus_paths=[path, path]))
    assert not validator.is_valid(
        response(edits=[], requested_checks=[["python", "-m", "pytest", "-q"]] * 2)
    )


@pytest.mark.parametrize("diagnostic", [CONFLICT, INCOMPLETE, PAUSE_PREFIX + INCOMPLETE])
def test_smaller_next_charged_batch_preserves_exact_plan_and_input(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic: str,
) -> None:
    data = payload(diagnostic=diagnostic)
    original = copy.deepcopy(data)
    calls = install_transport(monkeypatch, response())
    result = generator().generate(data)
    assert len(calls) == 1
    assert calls[0]["options"]["num_predict"] == 2000
    assert worker.MAX_MODEL_WALL_SECONDS == 240
    assert result["plan"] == data["plan"] and result["plan"] is not data["plan"]
    assert data == original
    validator = Draft202012Validator(calls[0]["format"])
    assert validator.is_valid(response())
    assert not validator.is_valid(response(plan=["Replace the accepted requirements"]))
    assert not validator.is_valid(response(edits=[{"path": "app.py", "content": "x" * 801}]))


@pytest.mark.parametrize(
    "changes",
    [
        {"message": "x" * 161},
        {"run_instructions": "x" * 241},
        {"plan": ["Rewritten plan"]},
        {"edits": [{"path": "app.py", "content": "x" * 801}]},
    ],
)
def test_rejection_bounds_are_enforced_locally_when_model_ignores_wire_schema(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    data = payload()
    original = copy.deepcopy(data)
    calls = install_transport(monkeypatch, response(**changes))
    with pytest.raises(worker.ModelStepError):
        generator().generate(data)
    assert len(calls) == 1 and data == original


@pytest.mark.parametrize("failure", ["conflict", "truncated", "length"])
def test_failed_recovery_preserves_snapshot_and_receipts_without_runner_or_hidden_retry(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    data = payload()
    data["checks"] = [
        {
            "command": ["python", "-m", "pytest", "-q"],
            "status": "passed",
            "exit_code": 0,
            "output": "1 passed",
            "duration_ms": 1,
        }
    ]
    original = copy.deepcopy(data)

    def conflicting(body: dict[str, Any]) -> dict[str, Any]:
        branch = next(
            b for b in body["format"]["oneOf"] if b["properties"]["patches"].get("minItems") == 1
        )
        choices = branch["properties"]["patches"]["items"]["oneOf"][0]["properties"]
        return response(
            patches=[
                {
                    "path": "app.py",
                    "span_id": choices["span_id"]["enum"][0],
                    "new": "VALUE = 3\n",
                }
            ]
        )

    output = conflicting if failure == "conflict" else '{"action":"continue","edits":['
    calls = install_transport(
        monkeypatch, output, done_reason="length" if failure == "length" else "stop"
    )

    class NoRunner:
        def run(self, *args: Any, **kwargs: Any) -> Any:
            pytest.fail("A rejected or truncated model batch must never reach the runner")

    result = worker.run_iteration(data, generator(), NoRunner(), lambda: None)
    assert len(calls) == 1
    for field in ("files", "plan", "checks", "base_revision_id", "base_sha256"):
        assert result[field] == original[field]
    assert result["action"] == "continue"
    assert data == original


def test_native_ready_recovery_reaches_separate_validation_without_fabricated_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = payload("Sources/App.swift", diagnostic=INCOMPLETE)
    calls = install_transport(monkeypatch, response(action="complete", edits=[]))

    class NoRunner:
        def run(self, *args: Any, **kwargs: Any) -> Any:
            pytest.fail("Native readiness cannot execute Python checks")

    result = worker.run_iteration(data, generator(), NoRunner(), lambda: None)
    assert len(calls) == 1
    assert result["native_validation"] == "required"
    assert result["files"] == data["files"] and result["plan"] == data["plan"]
    assert result["checks"] == []
