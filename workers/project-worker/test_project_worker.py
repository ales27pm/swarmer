from __future__ import annotations

import copy
import io
import json
from typing import Any

import project_worker as worker
import pytest
from jsonschema import Draft202012Validator
from project_contract import ProjectError, snapshot_sha


def payload() -> dict[str, Any]:
    return {
        "objective": "Créer une application",
        "conversation": [],
        "files": [],
        "plan": [],
        "checks": [],
        "iteration": 1,
        "base_revision_id": None,
        "base_sha256": None,
    }


def step(**changes: Any) -> dict[str, Any]:
    return {
        "action": "complete",
        "message": "Projet à vérifier",
        "plan": ["Application", "Tests"],
        "edits": [
            {"path": "README.md", "content": "Run pytest"},
            {"path": "app.py", "content": "value = 1\n"},
            {"path": "tests/test_app.py", "content": "def test_value():\n    assert True\n"},
        ],
        "deletions": [],
        "requested_checks": [],
        "run_instructions": "python -m pytest",
        "runtime": "python",
        **changes,
    }


class Generator:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return copy.deepcopy(self.response)


class Runner:
    def __init__(self, *, count: int = 1, fail: bool = False) -> None:
        self.count = count
        self.fail = fail
        self.calls = 0
        self.files: list[dict[str, str]] = []

    def run(self, files: list[dict[str, str]], *args: Any) -> dict[str, Any]:
        self.calls += 1
        self.files = files
        return {
            "checks": [
                {
                    "command": ["python", "-m", "pytest", "-q"],
                    "status": "failed" if self.fail else "passed",
                    "exit_code": 1 if self.fail else 0,
                    "output": "runner output",
                    "duration_ms": 12,
                }
            ],
            "tests_executed": self.count,
            "test_failures": 1 if self.fail else 0,
            "build_passed": True,
        }


def test_clarification_charges_one_model_call_without_execution() -> None:
    generator = Generator(step(action="clarify", message="Web ou bureau ?", edits=[]))
    runner = Runner()
    result = worker.run_iteration(payload(), generator, runner, lambda: None)
    assert generator.calls == 1 and runner.calls == 0
    assert result["action"] == "clarify" and result["checks"] == []


@pytest.mark.parametrize("count,fail", [(0, False), (1, True)])
def test_model_complete_cannot_override_empty_or_failed_real_tests(count: int, fail: bool) -> None:
    generator = Generator(step())
    result = worker.run_iteration(
        payload(), generator, Runner(count=count, fail=fail), lambda: None
    )
    assert generator.calls == 1
    assert result["action"] == "continue"
    assert result["files"] and result["checks"]


def test_followup_merges_files_and_copies_server_base_identity() -> None:
    files = [
        {"path": "README.md", "content": "Keep exact documentation"},
        {"path": "models.py", "content": "VALUE = 1"},
    ]
    data = {
        **payload(),
        "files": files,
        "base_revision_id": "revision_123",
        "base_sha256": snapshot_sha(files),
        "iteration": 2,
        "conversation": [{"role": "user", "content": "Add the contacts view"}],
    }
    response = step(edits=[{"path": "views.py", "content": "VIEW = 'contacts'"}])
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert result["action"] == "complete"
    assert all(item in result["files"] for item in files)
    assert result["base_revision_id"] == "revision_123"
    assert result["base_sha256"] == snapshot_sha(files)
    assert runner.files == result["files"]


def test_exact_patch_can_repair_a_fragment_without_replacing_unseen_file_content() -> None:
    files = [
        {"path": "README.md", "content": "Setup and requirements"},
        {"path": "app.py", "content": "#" + "unseen" * 5000 + "\nvalue = 1\n"},
    ]
    data = {
        **payload(),
        "files": files,
        "base_revision_id": "r1",
        "base_sha256": snapshot_sha(files),
    }
    generator = Generator(
        step(edits=[], patches=[{"path": "app.py", "old": "value = 1", "new": "value = 2"}])
    )
    generator.last_visible_paths = set()
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert generator.calls == runner.calls == 1
    assert result["files"][1]["content"] == files[1]["content"].replace("value = 1", "value = 2")
    assert result["base_sha256"] == data["base_sha256"]
    assert "patches" not in result


def test_failed_patch_batch_preserves_snapshot_and_real_receipts_without_execution() -> None:
    files = [{"path": "app.py", "content": "value = 1\n"}]
    data = {**payload(), "files": files}
    generator = Generator(
        step(edits=[], patches=[{"path": "app.py", "old": "absent", "new": "replacement"}])
    )
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert generator.calls == 1 and runner.calls == 0
    assert result["action"] == "continue" and result["files"] == files and result["checks"] == []
    assert "No edits were accepted" in result["message"]


def test_lease_loss_after_model_call_never_executes_or_returns_snapshot() -> None:
    calls = 0

    def active() -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise worker.protocol.LeaseLost("cancelled")

    generator, runner = Generator(step()), Runner()
    with pytest.raises(worker.protocol.LeaseLost):
        worker.run_iteration(payload(), generator, runner, active)
    assert generator.calls == 1 and runner.calls == 0


def test_context_keeps_complete_files_and_full_manifest_under_budget() -> None:
    data = payload()
    data["files"] = [{"path": f"module{i}.py", "content": "#" + "x" * 40_000} for i in range(20)]
    context = worker.model_context(data)
    assert len(context["file_manifest"]) == 20
    assert (
        sum(len(item["content"].encode()) for item in context["selected_complete_files"]) <= 60_000
    )
    assert all(item in data["files"] for item in context["selected_complete_files"])


@pytest.mark.parametrize(
    "model",
    [
        "qwen2.5-coder:7b",
        "swarmer-project-qwen3.5:9b-32k",
        "swarmer-project-qwen3-coder:30b-32k-06c1097e",
    ],
)
@pytest.mark.parametrize("large_context", [False, True])
def test_model_transport_uses_one_local_schema_request_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    large_context: bool,
) -> None:
    requests: list[Any] = []
    raw = json.dumps(
        {
            "message": {"content": json.dumps(step())},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 31,
            "prompt_eval_duration": 400_000,
            "eval_count": 12,
        }
    ).encode()

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            requests.append(request)
            return Response(raw)

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    data = payload()
    if large_context:
        data["files"] = [{"path": f"module{i}.py", "content": "#" + "é" * 3_000} for i in range(20)]
        data["conversation"] = [
            {"role": "user" if i % 2 else "assistant", "content": "contexte " * 400}
            for i in range(40)
        ]
        data["checks"] = [
            {
                "command": ["python", "-m", "pytest", "-q"],
                "status": "failed",
                "exit_code": 1,
                "output": "échec " * 1_000,
                "duration_ms": 10,
            }
        ]
    result = worker.ProjectGenerator("http://127.0.0.1:11434/v1", model).generate(data)
    assert result["action"] == "complete" and len(requests) == 1
    assert requests[0].get_header("Authorization") is None
    body = json.loads(requests[0].data)
    schema = body["format"]["oneOf"][0]
    assert all(branch["additionalProperties"] is False for branch in body["format"]["oneOf"])
    assert schema["properties"]["edits"]["maxItems"] == 3
    assert schema["properties"]["edits"]["items"]["properties"]["path"]["pattern"]
    if large_context:
        assert "minItems" not in schema["properties"]["edits"]
        assert "patches" in schema["properties"]
        assert next(iter(schema["properties"])) == "patches"
        assert schema["properties"]["focus_paths"]["items"]["enum"] == [
            item["path"] for item in data["files"]
        ]
    else:
        for field in ("patches", "deletions", "focus_paths"):
            assert schema["properties"][field]["maxItems"] == 0
        assert '"enum": []' not in json.dumps(schema)
    assert "maxLength" not in json.dumps(schema)
    assert body["stream"] is False
    assert requests[0].full_url == "http://127.0.0.1:11434/api/chat"
    assert body["keep_alive"] == "10m"
    assert body["options"]["num_ctx"] == 32_768
    assert body["options"]["num_predict"] == worker.MAX_OUTPUT_TOKENS
    assert (
        sum(len(item["content"].encode()) for item in body["messages"]) <= worker.MAX_PROMPT_BYTES
    )
    if "qwen3.5" in model:
        assert body["think"] is False
        assert body["options"] == {
            "num_ctx": 32_768,
            "num_predict": worker.MAX_OUTPUT_TOKENS,
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0,
            "presence_penalty": 1.5,
        }
    else:
        assert "think" not in body
    if "qwen3-coder" in model:
        assert body["options"] == {
            "num_ctx": 32_768,
            "num_predict": worker.MAX_OUTPUT_TOKENS,
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "repeat_penalty": 1.05,
        }


@pytest.mark.parametrize("timeout", [29, 241, float("nan"), float("inf")])
def test_project_timeout_rejects_unbounded_operator_configuration(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        worker.ProjectGenerator("http://127.0.0.1:11434/v1", "local-model", timeout_seconds=timeout)


def test_project_timeout_can_exceed_legacy_one_file_timeout() -> None:
    generator = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1", "local-model", timeout_seconds=240
    )
    assert generator.timeout_seconds == 240


def test_final_request_trimming_keeps_error_context_as_a_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "HIDDEN_HEADER = 99\n" + "# prefix\n" * 1222 + "BROKEN = !\n" + "# end\n" * 20
    data = {
        **payload(),
        "files": [{"path": "app.py", "content": content}],
        "conversation": [
            {"role": "assistant", "content": "I created the app."},
            {"role": "user", "content": "Preserve the application and fix tests."},
        ],
        "checks": [
            {
                "command": ["python", "-m", "compileall"],
                "status": "failed",
                "exit_code": 1,
                "duration_ms": 2,
                "output": 'File "/workspace/project/app.py", line 1224\n BROKEN = !\n'
                + "SyntaxError: invalid syntax\n"
                + "context " * 350,
            }
        ],
    }
    assert worker.model_context(data)["selected_complete_files"]
    captured = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            captured.append(json.loads(request.data))
            return Response(
                json.dumps(
                    {
                        "message": {"content": json.dumps(step())},
                        "done": True,
                        "done_reason": "stop",
                    }
                ).encode()
            )

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3.5:9b")
    generator.generate(data)
    messages = captured[0]["messages"]
    raw = (
        messages[-1]["content"]
        .split("Current workspace data:\n", 1)[1]
        .split("\n\nYOUR TASK", 1)[0]
    )
    assert 'SOURCE {"path":"app.py","complete":false' in raw
    assert "BROKEN = !" in raw
    assert "app.py" not in generator.last_visible_paths
    assert sum(len(item["content"].encode()) for item in messages) <= worker.MAX_PROMPT_BYTES
    choices = captured[0]["format"]["oneOf"][0]["properties"]["patches"]["items"]["oneOf"]
    identifiers = {
        identifier for choice in choices for identifier in choice["properties"]["span_id"]["enum"]
    }
    displayed = json.loads(raw.split("\n\nSOURCE ", 1)[0])["editable_spans"]
    assert identifiers == {item["span_id"] for item in displayed}
    assert any(item["start_line"] == 1224 for item in displayed)
    assert displayed and all(
        content[item["start_character"] : item["end_character"]] in raw
        and "HIDDEN_HEADER" not in content[item["start_character"] : item["end_character"]]
        for item in displayed
    )


def test_model_invalid_response_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            nonlocal calls
            calls += 1
            raise TimeoutError("offline")

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ProjectError):
        worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen2.5-coder:7b").generate(payload())
    assert calls == 1


def test_wire_grammar_separates_mutation_read_and_clarification() -> None:
    data = {**payload(), "files": [{"path": "app.py", "content": "value = 1\n"}]}
    context = worker.model_context(data)
    identifier = next(iter(worker.addressed_patch_spans(context, data)))
    schema = worker.constrained_step_schema(copy.deepcopy(worker.STEP_SCHEMA), context, data)
    validator = Draft202012Validator(schema)
    mutation = step(
        edits=[],
        patches=[{"path": "app.py", "span_id": identifier, "new": "value = 2\n"}],
        focus_paths=[],
    )
    assert validator.is_valid(mutation)
    assert not validator.is_valid({**mutation, "focus_paths": ["app.py"]})
    read = step(
        action="continue", edits=[], patches=[], requested_checks=[], focus_paths=["app.py"]
    )
    assert validator.is_valid(read)
    assert not validator.is_valid({**read, "requested_checks": [["python", "-m", "pytest", "-q"]]})
    assert validator.is_valid(step(action="clarify", edits=[], patches=[], focus_paths=[]))
    assert not validator.is_valid({**read, "action": "clarify"})
    assert not validator.is_valid(
        {**mutation, "patches": [{"path": "app.py", "span_id": "invented", "new": "x"}]}
    )


def test_patch_choices_are_unique_visible_and_bounded_without_exposing_hidden_source() -> None:
    visible = "    target = 'été' \n" + "repeat\n" * 2
    content = "UNSEEN_PRIVATE_SOURCE\n" + visible + "UNSEEN_END\n"
    data = {**payload(), "files": [{"path": "app.py", "content": content}]}
    context = {
        "selected_complete_files": [],
        "selected_file_fragments": [
            {"path": "app.py", "content": visible, "complete": False, "start_character": 22}
        ],
    }
    spans = worker.visible_patch_spans(context, data)
    assert "    target = 'été' \n" in spans["app.py"]
    assert "repeat\n" not in spans["app.py"]
    assert all(old in visible and content.count(old) == 1 for old in spans["app.py"])
    assert not any("UNSEEN" in old for old in spans["app.py"])
    data["files"] = [
        {
            "path": f"module{i}.py",
            "content": "\n".join(f"value{j} = '{i}" + "x" * 300 + "'" for j in range(40)),
        }
        for i in range(12)
    ]
    context = {"selected_complete_files": data["files"], "selected_file_fragments": []}
    spans = worker.visible_patch_spans(context, data)
    assert len(spans) <= 8 and all(len(choices) <= 24 for choices in spans.values())
    assert (
        sum(len(old.encode()) for choices in spans.values() for old in choices)
        <= worker.MAX_SPAN_BYTES
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("final_newline", [True, False])
def test_span_coordinates_preserve_unicode_whitespace_and_real_line_endings(
    newline: str, final_newline: bool
) -> None:
    prefix = "# été" + newline
    visible = "\tvalue = 'été' " + newline + "other = 2" + (newline if final_newline else "")
    content = prefix + visible
    data = {**payload(), "files": [{"path": "app.py", "content": content}]}
    context = {
        "selected_complete_files": [],
        "selected_file_fragments": [{"path": "app.py", "content": visible}],
    }
    addresses = worker.addressed_patch_spans(context, data)
    first = next(item for item in addresses.values() if item["old"] == visible.splitlines(True)[0])
    assert first["start_character"] == len(prefix)
    assert first["start_character"] != len(prefix.encode())
    assert first["start_line"] == first["end_line"] == 2
    assert not first["partial_start"] and not first["partial_end"]
    tail = next(item for item in addresses.values() if item["old"] == visible.splitlines(True)[1])
    assert tail["start_line"] == tail["end_line"] == 3
    assert not tail["partial_end"]
    original = copy.deepcopy(data["files"])
    resolved = worker.resolve_model_patches(
        step(edits=[], patches=[{"path": "app.py", "span_id": first["span_id"], "new": "changed"}]),
        addresses,
    )
    merged = worker.merge_files(data["files"], resolved)
    assert merged[0]["content"] == prefix + "changed" + newline + visible.splitlines(True)[1]
    assert data["files"] == original


def test_partial_span_coordinates_do_not_claim_complete_boundary_lines() -> None:
    content = "# é\r\nfirst = 1\r\nsecond = 2\r\nlast = 3"
    start, end = content.index("st ="), content.index(" = 3")
    coordinates = worker.source_coordinates(content, start, end)
    assert coordinates["start_line"] == 2 and coordinates["end_line"] == 4
    assert coordinates["partial_start"] and coordinates["partial_end"]
    between_crlf = content.index("\n")
    assert worker.source_coordinates(content, between_crlf, between_crlf + 1)["partial_start"]
    data = {**payload(), "files": [{"path": "app.py", "content": content}]}
    context = {
        "selected_complete_files": [],
        "selected_file_fragments": [{"path": "app.py", "content": content[start:end]}],
    }
    addresses = worker.addressed_patch_spans(context, data)
    assert addresses and all(item["old"] in content[start:end] for item in addresses.values())
    assert any(item["partial_start"] for item in addresses.values())
    assert any(item["partial_end"] for item in addresses.values())


@pytest.mark.parametrize("fence", ["files", "base_revision_id", "iteration"])
def test_span_ids_are_fenced_to_the_full_base_revision_and_iteration(fence: str) -> None:
    data = {**payload(), "files": [{"path": "app.py", "content": "value = 1\n"}]}
    context = worker.model_context(data)
    addresses = worker.addressed_patch_spans(context, data)
    changed = copy.deepcopy(data)
    if fence == "files":
        changed["files"].append({"path": "unseen.py", "content": "private"})
    else:
        changed[fence] = "revision_new" if fence == "base_revision_id" else 2
    updated = worker.addressed_patch_spans(context, changed)
    assert not addresses.keys() & updated.keys()
    stale = step(
        edits=[], patches=[{"path": "app.py", "span_id": next(iter(addresses)), "new": "x"}]
    )
    with pytest.raises(ProjectError, match="unknown, stale"):
        worker.resolve_model_patches(stale, updated)
    stale["patches"][0]["path"] = "wrong.py"
    with pytest.raises(ProjectError, match="differently targeted"):
        worker.resolve_model_patches(stale, addresses)


def test_addressed_overlapping_patches_still_fail_atomically() -> None:
    files = [{"path": "app.py", "content": "first = 1\nsecond = 2\nthird = 3\n"}]
    data = {**payload(), "files": files}
    addresses = worker.addressed_patch_spans(worker.model_context(data), data)
    selected = [item for item in addresses.values() if item["start_line"] == 1][:2]
    assert len(selected) == 2 and selected[0]["end_line"] < selected[1]["end_line"]
    raw = step(
        edits=[],
        patches=[
            {"path": "app.py", "span_id": item["span_id"], "new": "changed"} for item in selected
        ],
    )
    original = copy.deepcopy(files)
    with pytest.raises(ProjectError, match="overlap"):
        worker.merge_files(files, worker.resolve_model_patches(raw, addresses))
    assert files == original and all("span_id" in patch for patch in raw["patches"])


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("replacement", ["# comment", "", "# explicit\n", "# explicit\r\n"])
def test_addressed_editor_preserves_only_missing_terminal_separators(
    newline: str, replacement: str
) -> None:
    old = "value = 1" + newline
    content = old + "following = 2" + newline
    data = {**payload(), "files": [{"path": "app.py", "content": content}]}
    addresses = worker.addressed_patch_spans(worker.model_context(data), data)
    identifier = next(key for key, item in addresses.items() if item["old"] == old)
    resolved = worker.resolve_model_patches(
        step(edits=[], patches=[{"path": "app.py", "span_id": identifier, "new": replacement}]),
        addresses,
    )
    expected = replacement + (newline if replacement == "# comment" else "")
    assert (
        worker.merge_files(data["files"], resolved)[0]["content"]
        == expected + "following = 2" + newline
    )


def test_addressed_editor_does_not_add_a_newline_at_unterminated_eof() -> None:
    data = {**payload(), "files": [{"path": "app.py", "content": "value = 1"}]}
    addresses = worker.addressed_patch_spans(worker.model_context(data), data)
    resolved = worker.resolve_model_patches(
        step(
            edits=[],
            patches=[{"path": "app.py", "span_id": next(iter(addresses)), "new": "value = 2"}],
        ),
        addresses,
    )
    assert worker.merge_files(data["files"], resolved)[0]["content"] == "value = 2"


def test_added_terminal_separator_is_included_in_strict_patch_size_limit() -> None:
    data = {**payload(), "files": [{"path": "app.py", "content": "value = 1\r\n"}]}
    addresses = worker.addressed_patch_spans(worker.model_context(data), data)
    resolved = worker.resolve_model_patches(
        step(
            edits=[],
            patches=[
                {
                    "path": "app.py",
                    "span_id": next(iter(addresses)),
                    "new": "x" * worker.MAX_PATCH_BYTES,
                }
            ],
        ),
        addresses,
    )
    with pytest.raises(ProjectError, match="exceeds"):
        worker.merge_files(data["files"], resolved)
    assert data["files"][0]["content"] == "value = 1\r\n"


def test_source_priority_keeps_app_and_user_requirements_over_old_progress_and_success_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "API_START = True\n" + "# implementation detail\n" * 420 + "API_END = True\n"
    latest_user = "Keep CRUD, SQLite persistence and validation. " * 10
    data = {
        **payload(),
        "files": [
            {"path": "README.md", "content": "Durable user decisions\n" * 45},
            {"path": "requirements.txt", "content": "flask==3.0.0\n"},
            {"path": "app.py", "content": source},
        ],
        "conversation": [
            {"role": "user", "content": latest_user},
            *[
                {"role": "assistant", "content": "obsolete repeated diagnostic " * 80}
                for _ in range(12)
            ],
            {
                "role": "assistant",
                "content": "Latest progress: application compiles; add real tests.",
            },
        ],
        "checks": [
            {
                "command": ["pip", "install"],
                "status": "passed",
                "exit_code": 0,
                "duration_ms": 1,
                "output": "successful installation log " * 250,
            },
            {
                "command": ["python", "-m", "pytest", "-q"],
                "status": "failed",
                "exit_code": 5,
                "duration_ms": 1,
                "output": "no tests ran",
            },
        ],
    }
    captured = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            captured.append(json.loads(request.data))
            return Response(
                json.dumps(
                    {
                        "message": {"content": json.dumps(step())},
                        "done": True,
                        "done_reason": "stop",
                    }
                ).encode()
            )

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3.5:9b")
    generator.generate(data)
    messages = captured[0]["messages"]
    assert "app.py" in generator.last_visible_paths and source in messages[-1]["content"]
    assert any(message["content"] == latest_user for message in messages)
    assert "Latest progress" in json.dumps(messages) and "no tests ran" in messages[-1]["content"]
    assert "obsolete repeated" not in json.dumps(messages)
    assert "successful installation log" not in json.dumps(messages)
    assert sum(len(message["content"].encode()) for message in messages) <= worker.MAX_PROMPT_BYTES
    assert next(iter(captured[0]["format"]["oneOf"][0]["properties"])) == "edits"
    assert "collected NO TESTS" in messages[-1]["content"]
    assert "Adding pytest to requirements does not create tests" in messages[-1]["content"]


def test_prompt_compaction_fails_instead_of_dropping_an_oversized_latest_user_request() -> None:
    data = {
        **payload(),
        "files": [{"path": "app.py", "content": "value = 1\n"}],
        "conversation": [
            {"role": "user", "content": "😀" * 4_000},
            {"role": "assistant", "content": "progress " * 400},
        ],
    }
    original = copy.deepcopy(data)
    with pytest.raises(ProjectError, match="context budget"):
        worker.model_context(data)
    assert data == original


@pytest.mark.parametrize("displayed", ["app.py", "./app.py", "/workspace/project/app.py"])
def test_source_fragment_uses_exact_runner_or_relative_traceback_paths(displayed: str) -> None:
    item = {"path": "app.py", "content": "line\n" * 3_000}
    fragment = worker.source_fragment(item, payload(), f'File "{displayed}", line 1000')
    assert fragment["start_character"] == 999 * 5 - 700


@pytest.mark.parametrize(
    "displayed",
    [
        "/usr/lib/python/site-packages/flask/app.py",
        "flask/app.py",
        "/workspace/other/app.py",
        "other_app.py",
    ],
)
def test_source_fragment_ignores_foreign_basename_collisions(displayed: str) -> None:
    item = {"path": "app.py", "content": "line\n" * 3_000}
    foreign = f'File "{displayed}", line 1000'
    assert worker.source_fragment(item, payload(), foreign)["start_character"] == 0
    actual = foreign + '\nFile "/workspace/project/app.py", line 200'
    assert worker.source_fragment(item, payload(), actual)["start_character"] == 199 * 5 - 700


@pytest.mark.parametrize(
    "diagnostic",
    ["ERROR collecting tests/test_app.py\nModuleNotFoundError: missing\n", "1 skipped in 0.01s\n"],
)
def test_zero_executed_tests_does_not_misclassify_collection_errors_or_skips(
    monkeypatch: pytest.MonkeyPatch, diagnostic: str
) -> None:
    data = {
        **payload(),
        "files": [{"path": "tests/test_app.py", "content": "import missing\n"}],
        "checks": [
            {
                "command": ["python", "-m", "pytest", "-q"],
                "status": "failed",
                "exit_code": 5,
                "duration_ms": 1,
                "output": diagnostic,
            }
        ],
    }
    captured = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            captured.append(json.loads(request.data))
            return Response(
                json.dumps(
                    {
                        "message": {"content": json.dumps(step())},
                        "done": True,
                        "done_reason": "stop",
                    }
                ).encode()
            )

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b").generate(data)
    assert next(iter(captured[0]["format"]["oneOf"][0]["properties"])) == "patches"
    task = captured[0]["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:")[1]
    assert "collected NO TESTS" not in task and diagnostic in task


def test_diagnostic_function_span_preserves_decorators_unicode_and_multiline_signature() -> None:
    prefix = "# été\r\n" * 100 + "@route('/search')\r\n"
    function = "def recherche(\r\n    query: str,\r\n):\r\n    return query\r\n"
    content = prefix + function + "\r\ndef other():\r\n    return None\r\n"
    item = {"path": "app.py", "content": content}
    diagnostic = "File \"/dependencies/python/flask/app.py\", line 1161\nTypeError: view function for 'recherche' returned None"
    functions = worker.diagnostic_functions(item, diagnostic)
    assert functions == [(len(prefix), len(prefix + function))]
    fragment = worker.source_fragment(item, payload(), diagnostic)
    assert function in fragment["content"]
    data = {**payload(), "files": [item], "checks": [{"output": diagnostic}]}
    context = {"selected_complete_files": [], "selected_file_fragments": [fragment]}
    addresses = worker.addressed_patch_spans(context, data)
    first = next(iter(addresses.values()))
    assert first["old"] == function and first["start_line"] == 102
    resolved = worker.resolve_model_patches(
        step(
            edits=[],
            patches=[
                {
                    "path": "app.py",
                    "span_id": first["span_id"],
                    "new": "def recherche(query):\r\n    return query.strip()",
                }
            ],
        ),
        addresses,
    )
    merged = worker.merge_files([item], resolved)[0]["content"]
    assert merged.startswith(prefix) and "return query.strip()\r\n\r\ndef other" in merged


@pytest.mark.parametrize("prefix", ['MESSAGE = "first\u2028second"\n', "# first\fsecond\n"])
def test_ast_spans_count_physical_lines_instead_of_unicode_text_separators(prefix: str) -> None:
    function = "def recherche():\n    return None\n"
    item = {"path": "app.py", "content": prefix + function}
    assert worker.diagnostic_functions(item, "view 'recherche' failed") == [
        (len(prefix), len(prefix + function))
    ]
    assert worker.physical_source_lines(item["content"])[0] == prefix
    assert (
        worker.source_coordinates(item["content"], len(prefix), len(prefix + function))[
            "start_line"
        ]
        == 2
    )


@pytest.mark.parametrize("diagnostic", ["view 'unknown' failed", "view 'same' failed"])
def test_unknown_or_ambiguous_diagnostic_function_names_do_not_select_a_target(
    diagnostic: str,
) -> None:
    item = {"path": "app.py", "content": "def same():\n    pass\n\ndef same():\n    pass\n"}
    assert worker.diagnostic_functions(item, diagnostic) == []
    assert worker.source_fragment(item, payload(), diagnostic)["start_character"] == 0


def test_partial_oversized_function_never_gets_a_whole_function_span_id() -> None:
    item = {
        "path": "app.py",
        "content": "def large():\n" + "    # source context\n" * 200 + "    return 1\n",
    }
    diagnostic = "view 'large' failed"
    fragment = worker.source_fragment(item, payload(), diagnostic)
    assert len(fragment["content"]) == 2_000
    data = {**payload(), "files": [item], "checks": [{"output": diagnostic}]}
    context = {"selected_complete_files": [], "selected_file_fragments": [fragment]}
    addresses = worker.addressed_patch_spans(context, data)
    assert all(address["old"] in fragment["content"] for address in addresses.values())
    assert not any(address["old"] == item["content"] for address in addresses.values())


def test_quoted_diagnostic_function_admits_a_file_not_named_in_the_traceback() -> None:
    item = {
        "path": "views.py",
        "content": "# padding\n" * 2_400 + "def recherche():\n    return None\n",
    }
    data = {
        **payload(),
        "files": [item],
        "checks": [
            {
                "command": ["python", "-m", "pytest", "-q"],
                "status": "failed",
                "exit_code": 1,
                "duration_ms": 1,
                "output": "TypeError: view function for 'recherche' returned None\n/dependencies/python/flask/app.py:1161",
            }
        ],
    }
    context = worker.model_context(data)
    assert context["selected_complete_files"] == []
    assert len(context["selected_file_fragments"]) == 1
    assert "def recherche" in context["selected_file_fragments"][0]["content"]
    addresses = worker.addressed_patch_spans(context, data)
    assert any(
        address["old"] == "def recherche():\n    return None\n" for address in addresses.values()
    )


def test_focus_rotates_patch_candidates_even_when_complete_source_is_visible() -> None:
    item = {
        "path": "app.py",
        "content": "".join(f"value_{index} = {index}\n" for index in range(500)),
    }
    data = {**payload(), "files": [item], "iteration": 2, "focus_paths": ["app.py"]}
    context = {"selected_complete_files": [item], "selected_file_fragments": []}
    addresses = worker.addressed_patch_spans(context, data)
    assert addresses and all(
        address["old"] in item["content"][2_000:4_000] for address in addresses.values()
    )
    assert all(address["start_character"] >= 2_000 for address in addresses.values())


def test_small_address_budget_allocates_across_files_before_more_spans_per_file() -> None:
    files = [
        {"path": name, "content": "".join(f"value{index} = '{name}'\n" for index in range(30))}
        for name in ("a.py", "b.py", "c.py")
    ]
    data = {**payload(), "files": files}
    context = {"selected_complete_files": files, "selected_file_fragments": []}
    addresses = worker.addressed_patch_spans(context, data, max_bytes=1_000)
    assert {item["path"] for item in addresses.values()} == {"a.py", "b.py", "c.py"}


def test_precise_traceback_file_precedes_incidental_named_functions_in_small_catalog() -> None:
    files = [
        {"path": "other_a.py", "content": "def noisy_a():\n    return 1\n"},
        {"path": "other_b.py", "content": "def noisy_b():\n    return 2\n"},
        {"path": "broken.py", "content": "first = 1\nBROKEN = ?\n"},
    ]
    data = {
        **payload(),
        "files": files,
        "checks": [
            {
                "command": ["python", "-m", "compileall", "-q", "."],
                "status": "failed",
                "exit_code": 1,
                "duration_ms": 1,
                "output": "'noisy_a' and 'noisy_b'\nFile \"/workspace/project/broken.py\", line 2\nBROKEN = ?",
            }
        ],
    }
    context = worker.model_context(data)
    assert context["selected_complete_files"][0]["path"] == "broken.py"
    addresses = worker.addressed_patch_spans(context, data, max_bytes=500)
    assert next(iter(addresses.values()))["path"] == "broken.py"


def test_exact_traceback_target_expands_only_until_unique_before_incidental_text_matches() -> None:
    duplicate = "    return value\n\n@route('/same')\n"
    before = "INCIDENTAL = 1\n" + duplicate + "def first(): pass\n"
    content = before + duplicate + "def second(): pass\n"
    target = len(worker.physical_source_lines(before)) + 1
    diagnostic = (
        f'INCIDENTAL = 1\nFile "/workspace/project/app.py", line {target}\n    return value\n'
    )
    data = {
        **payload(),
        "files": [{"path": "app.py", "content": content}],
        "checks": [{"output": diagnostic}],
    }
    context = {"selected_complete_files": data["files"], "selected_file_fragments": []}
    addresses = worker.addressed_patch_spans(context, data)
    first = next(iter(addresses.values()))
    assert first["start_line"] <= target <= first["end_line"]
    assert "INCIDENTAL" not in first["old"] and content.count(first["old"]) == 1
    assert first["end_line"] - first["start_line"] == 1


def test_unique_expansion_never_uses_unshown_context_or_exceeds_the_byte_limit() -> None:
    shown = "same\n" * 4
    original = "first\n" + shown + "second\n" + shown
    assert worker.unique_diagnostic_span(worker.physical_source_lines(shown), 1, original) is None
    large = "é" * (worker.MAX_PATCH_BYTES // 2 + 1) + "\n"
    assert worker.unique_diagnostic_span([large], 0, large) is None


@pytest.mark.parametrize("path", ["app.py", "./app.py", "/workspace/project/app.py"])
def test_precise_line_parser_accepts_parenthetical_compile_receipts(path: str) -> None:
    assert worker.project_traceback_line("app.py", f"IndentationError ({path}, line 137)") == 137
    assert (
        worker.project_traceback_line(
            "app.py", "IndentationError (/dependencies/flask/app.py, line 137)"
        )
        is None
    )


def test_raw_patch_preview_preserves_exact_boundaries_and_is_counted_in_rendered_context() -> None:
    source = "before = 1\n\tvalue = 'été' \r\nafter = 2\n"
    target = "\tvalue = 'été' \r\n"
    context = {
        "selected_complete_files": [{"path": "app.py", "content": source}],
        "selected_file_fragments": [],
        "editable_span_previews": [{"span_id": "app.py@L2-2:example", "content": target}],
    }
    rendered = worker.render_workspace_context(context)
    preview = rendered.split("PATCH_TARGET ", 1)[1]
    assert target in preview and "before = 1" not in preview and "after = 2" not in preview
    assert '"span_id":"app.py@L2-2:example"' in preview
    without = worker.render_workspace_context({**context, "editable_span_previews": []})
    assert len(rendered.encode()) >= len(without.encode()) + len(target.encode())


def test_raw_source_renderer_preserves_whitespace_and_avoids_delimiter_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Digest:
        def hexdigest(self) -> str:
            return "0" * 64

    monkeypatch.setattr(worker.hashlib, "sha256", lambda value: Digest())
    source = "\tvalue = 1 \nSWARMER_SOURCE_0000000000000000\n"
    context = {
        "objective": "Read source",
        "selected_complete_files": [{"path": "app.py", "content": source}],
        "selected_file_fragments": [],
    }
    rendered = worker.render_workspace_context(context)
    assert source in rendered
    assert "SWARMER_SOURCE_0000000000000000_NEXT_BEGIN\n" in rendered
    assert 'SOURCE {"path":"app.py","complete":true' in rendered


@pytest.mark.parametrize("done_reason", ["stop", "length"])
def test_rejected_native_response_keeps_metrics_and_specific_safe_diagnostic(
    monkeypatch: pytest.MonkeyPatch, done_reason: str
) -> None:
    invalid = step(patches=[{"path": "app.py", "span_id": "private-source-marker", "new": "fixed"}])
    raw = json.dumps(
        {
            "message": {"content": json.dumps(invalid)},
            "done": True,
            "done_reason": done_reason,
            "eval_count": 3000,
        }
    ).encode()

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            return Response(raw)

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3.5:9b")
    with pytest.raises(worker.ModelStepError) as error:
        generator.generate(payload())
    assert generator.last_metrics["eval_count"] == 3000
    assert ("unknown" if done_reason == "stop" else "incomplete") in str(error.value)
    assert "private-source-marker" not in str(error.value)


def test_native_response_cannot_bypass_current_visible_patch_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {**payload(), "files": [{"path": "app.py", "content": "value = 1\n"}]}
    response = step(
        edits=[], patches=[{"path": "app.py", "span_id": "stale-span", "new": "value = 2"}]
    )
    raw = json.dumps(
        {"message": {"content": json.dumps(response)}, "done": True, "done_reason": "stop"}
    ).encode()

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            return Response(raw)

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(worker.ModelStepError, match="unknown, stale"):
        worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3.5:9b").generate(data)
    assert data["files"][0]["content"] == "value = 1\n"


def test_shared_transport_default_stays_one_mb_and_project_limit_is_opt_in() -> None:
    raw = json.dumps({"content": "x" * 1_100_000}).encode()
    with pytest.raises(worker.protocol.WorkerProtocolError, match="size"):
        worker.protocol._read_control_response(io.BytesIO(raw))
    assert worker.protocol._read_control_response(io.BytesIO(raw), 4_000_000)["content"]
    client = worker.protocol.ControlPlaneClient("http://127.0.0.1:8000", "a", "c")
    assert client._request is worker.protocol.request
    project = worker.protocol.ControlPlaneClient(
        "http://127.0.0.1:8000", "a", "c", max_response_bytes=4_000_000
    )
    assert project._request.keywords["max_response_bytes"] == 4_000_000


def test_custom_protocol_transport_retains_existing_signature() -> None:
    calls = []
    client = worker.protocol.ControlPlaneClient(
        "http://127.0.0.1:8000",
        "a",
        "c",
        request_fn=lambda *args: calls.append(args),
        max_response_bytes=4_000_000,
    )
    client.heartbeat_agent("online")
    assert len(calls[0]) == 5


def test_rejected_model_step_preserves_snapshot_without_a_forged_check_or_second_call() -> None:
    files = [{"path": "README.md", "content": "existing"}, {"path": "app.py", "content": "x=1"}]
    data = {
        **payload(),
        "files": files,
        "base_revision_id": "revision_1",
        "base_sha256": snapshot_sha(files),
        "plan": ["Keep existing behavior"],
    }

    class InvalidGenerator(Generator):
        def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls += 1
            raise worker.ModelStepError(
                "The model step failed strict validation. No edits were accepted."
            )

    generator, runner = InvalidGenerator({}), Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert generator.calls == 1 and runner.calls == 0
    assert result["action"] == "continue" and result["files"] == files
    assert result["checks"] == data["checks"] and result["plan"] == data["plan"]
    assert result["base_sha256"] == data["base_sha256"]
    assert "No edits were accepted" in result["message"]


def test_focus_reads_preserve_receipts_and_do_not_execute() -> None:
    files = [{"path": "large.py", "content": "x=1"}]
    data = {
        **payload(),
        "files": files,
        "checks": [
            {
                "command": ["pytest"],
                "status": "failed",
                "exit_code": 1,
                "duration_ms": 2,
                "output": "failure",
            }
        ],
    }
    generator = Generator(step(action="continue", edits=[], focus_paths=["large.py"]))
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert result["focus_paths"] == ["large.py"]
    assert result["files"] == files and result["checks"] == data["checks"]
    assert runner.calls == 0 and generator.calls == 1


def test_unknown_focus_is_rejected_without_losing_existing_files() -> None:
    data = {**payload(), "files": [{"path": "app.py", "content": "x=1"}]}
    result = worker.run_iteration(
        data,
        Generator(step(action="continue", edits=[], focus_paths=["absent.py"])),
        Runner(),
        lambda: None,
    )
    assert result["files"] == data["files"] and result["focus_paths"] == []
    assert "absent" in result["message"]


def test_unread_existing_files_are_not_overwritten_by_model_guesses() -> None:
    data = {**payload(), "files": [{"path": "app.py", "content": "original"}]}
    generator = Generator(step(edits=[{"path": "app.py", "content": "guessed replacement"}]))
    generator.last_visible_paths = set()
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert result["files"] == data["files"] and result["focus_paths"] == ["app.py"]
    assert runner.calls == 0


def test_readiness_diagnostic_names_missing_readme_even_when_tests_pass() -> None:
    response = step(edits=[{"path": "app.py", "content": "x=1"}])
    result = worker.run_iteration(payload(), Generator(response), Runner(), lambda: None)
    assert result["action"] == "continue" and "README.md" in result["message"]
    assert "failed check" not in result["message"]


def test_context_labels_partial_reads_and_historical_memory_without_exceeding_byte_budget() -> None:
    data = {
        **payload(),
        "files": [{"path": "large.py", "content": "#" + "x" * 63_000}],
        "focus_paths": ["large.py"],
        "memory": {
            "mode": "semantic",
            "reason": "matched",
            "items": [
                {
                    "id": "memory_1",
                    "summary": "Historical hint",
                    "score": 0.5,
                    "source_id": "revision_1",
                }
            ],
        },
    }
    context = worker.model_context(data)
    assert context["selected_complete_files"] == []
    assert context["selected_file_fragments"][0]["complete"] is False
    assert context["historical_memory_hints"]["items"][0]["summary"] == "Historical hint"
    total = len(worker.SYSTEM_PROMPT.encode()) + len(
        json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode()
    )
    assert total <= worker.MAX_PROMPT_BYTES
