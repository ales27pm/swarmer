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


def single_file_step() -> dict[str, Any]:
    return step(edits=[{"path": "app.py", "content": "value = 1\n"}])


class Generator:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0

    def generate(
        self, payload: dict[str, Any], ensure_active: Any | None = None
    ) -> dict[str, Any]:
        self.calls += 1
        if ensure_active is not None:
            ensure_active()
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


@pytest.mark.parametrize("action", ["continue", "complete"])
def test_empty_initial_response_preserves_checks_without_running_an_empty_project(
    action: str,
) -> None:
    generator = Generator(step(action=action, edits=[], message="I will create the CRM now."))
    runner = Runner()
    before = payload()
    result = worker.run_iteration(before, generator, runner, lambda: None)
    assert runner.calls == 0
    assert result["files"] == [] and result["checks"] == before["checks"]
    assert result["action"] == "continue"
    assert "no application files" in result["message"]
    assert "I will create" not in result["message"]


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
            "message": {"content": json.dumps(single_file_step())},
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
    assert schema["properties"]["edits"]["maxItems"] == 1
    assert schema["properties"]["edits"]["items"]["properties"]["path"]["pattern"]
    if large_context:
        assert any(
            branch["properties"]["edits"].get("maxItems") == 0
            and branch["properties"]["focus_paths"].get("minItems") == 1
            for branch in body["format"]["oneOf"]
        )
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
    assert body["stream"] is True
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
                        "message": {"content": json.dumps(single_file_step())},
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


@pytest.mark.parametrize("stage", ["connect", "read", "wrapped"])
def test_model_timeout_preserves_snapshot_and_receipts_without_execution_or_retry(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    calls = 0
    data = {
        **payload(),
        "files": [{"path": "app.py", "content": "original = 'été'\r\n"}],
        "plan": ["Preserve completed work", "Add tests"],
        "checks": [
            {
                "command": ["python", "-m", "pytest", "-q"],
                "status": "failed",
                "exit_code": 1,
                "output": "real previous failure",
                "duration_ms": 25,
            }
        ],
        "base_revision_id": "revision_1",
    }
    data["base_sha256"] = snapshot_sha(data["files"])
    original = copy.deepcopy(data)

    class Response(io.BytesIO):
        status = 200

        def readline(self, size: int = -1) -> bytes:
            raise TimeoutError("private response detail")

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            nonlocal calls
            calls += 1
            if stage == "read":
                return Response()
            if stage == "wrapped":
                raise worker.urllib.error.URLError(TimeoutError("private connection detail"))
            raise TimeoutError("private connection detail")

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b")
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert calls == 1 and runner.calls == 0
    assert result["action"] == "continue"
    for field in ("files", "plan", "checks", "base_revision_id", "base_sha256"):
        assert result[field] == original[field]
    assert data == original
    assert result["message"] == worker.MODEL_TIMEOUT_DIAGNOSTIC
    assert "private" not in result["message"]


def test_streaming_native_response_assembles_chunks_and_checks_active_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = single_file_step()
    serialized = json.dumps(expected)
    midpoint = len(serialized) // 2
    raw = b"\n".join(
        json.dumps(item).encode()
        for item in (
            {
                "message": {"content": serialized[:midpoint]},
                "done": False,
            },
            {
                "message": {"content": serialized[midpoint:]},
                "done": False,
            },
            {
                "message": {"content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 22,
                "eval_count": 33,
            },
        )
    ) + b"\n"
    requests = []
    active_checks = 0

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            requests.append(json.loads(request.data))
            return Response(raw)

    def active() -> None:
        nonlocal active_checks
        active_checks += 1

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b"
    )
    assert generator.generate(payload(), active)["edits"] == expected["edits"]
    assert requests[0]["stream"] is True
    assert active_checks >= 4
    assert generator.last_metrics == {"prompt_eval_count": 22, "eval_count": 33}


def test_streaming_native_response_rejects_eof_before_terminal_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        json.dumps({"message": {"content": "{\"action\":"}, "done": False})
        + "\n"
    ).encode()

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            return Response(raw)

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(worker.ModelStepError, match="incomplete"):
        worker.ProjectGenerator(
            "http://127.0.0.1:11434/v1", "qwen3-coder:30b"
        ).generate(payload())


def test_streaming_native_response_rejects_malformed_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            return Response(b"not-json\n")

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(worker.ModelStepError, match="incomplete"):
        worker.ProjectGenerator(
            "http://127.0.0.1:11434/v1", "qwen3-coder:30b"
        ).generate(payload())


def test_streaming_native_response_enforces_total_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"x" * (worker.MAX_MODEL_RESPONSE_BYTES + 1)

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            return Response(raw)

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(worker.ProjectError, match="byte limit"):
        worker.ProjectGenerator(
            "http://127.0.0.1:11434/v1", "qwen3-coder:30b"
        ).generate(payload())


def test_streaming_native_response_enforces_productive_wall_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            return Response(
                json.dumps({"message": {"content": "{"}, "done": False}).encode()
                + b"\n"
            )

    clock = iter((0.0, worker.MAX_MODEL_WALL_SECONDS + 0.01))
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(worker.ModelTimeoutError, match="timed out"):
        worker.ProjectGenerator(
            "http://127.0.0.1:11434/v1", "qwen3-coder:30b"
        ).generate(payload())


def test_streaming_native_response_bounds_each_read_by_remaining_wall_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[float] = []

    class Socket:
        def settimeout(self, value: float) -> None:
            configured.append(value)

    class Raw:
        _sock = Socket()

    class Stream:
        raw = Raw()

    class Response(io.BytesIO):
        status = 200
        fp = Stream()

        def readline(self, size: int = -1) -> bytes:
            raise TimeoutError("private response detail")

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            return Response()

    clock = iter((10.0, 10.0 + worker.MAX_MODEL_WALL_SECONDS - 1.25))
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(worker.ModelTimeoutError, match="timed out") as error:
        worker.ProjectGenerator(
            "http://127.0.0.1:11434/v1", "qwen3-coder:30b"
        ).generate(payload())
    assert configured == [pytest.approx(1.25)]
    assert "private" not in str(error.value)


def test_documentation_uses_the_normal_model_wall_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        **payload(),
        "files": [{"path": "app.py", "content": "VALUE = 1\n"}],
        "plan": ["Keep the application", "Document setup"],
        "checks": [
            {
                "command": ["python", "-m", "pytest", "-q"],
                "status": "passed",
                "exit_code": 0,
                "duration_ms": 2,
                "output": "5 passed",
            }
        ],
        "conversation": [{"role": "assistant", "content": "Progress"}],
    }
    response = step(
        action="complete",
        edits=[{"path": "README.md", "content": "# App\n\nRun `pytest`.\n"}],
        plan=data["plan"],
        run_instructions="python -m pytest -q",
    )
    raw = json.dumps(
        {
            "message": {"content": json.dumps(response)},
            "done": True,
            "done_reason": "stop",
        }
    ).encode()

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            return Response(raw)

    clock = iter((0.0, worker.MAX_MODEL_WALL_SECONDS + 30.0))
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(worker.ModelTimeoutError, match="timed out"):
        worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b").generate(data)


def test_streaming_native_error_event_is_a_fixed_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            return Response(b'{"error":"private backend detail"}\n')

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(worker.ModelTransportError) as error:
        worker.ProjectGenerator(
            "http://127.0.0.1:11434/v1", "qwen3-coder:30b"
        ).generate(payload())
    assert error.value.category == "unavailable"
    assert "private" not in str(error.value)


def test_second_consecutive_timeout_pauses_instead_of_charging_a_third_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        **payload(),
        "conversation": [
            {"role": "assistant", "content": worker.MODEL_TIMEOUT_DIAGNOSTIC}
        ],
    }

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            raise TimeoutError("private detail")

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    result = worker.run_iteration(
        data,
        worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b"),
        Runner(),
        lambda: None,
    )
    assert result["action"] == "clarify"
    assert result["files"] == data["files"] and result["checks"] == data["checks"]
    assert "paused" in result["message"].casefold()


@pytest.mark.parametrize(
    "failure,category",
    [
        ("connection", "connection_error"),
        ("url", "connection_error"),
        (400, "configuration_error"),
        (401, "configuration_error"),
        (404, "configuration_error"),
        (429, "unavailable"),
        (500, "unavailable"),
    ],
)
def test_non_timeout_transport_failure_stays_failed_and_logs_only_fixed_category(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str | int,
    category: str,
) -> None:
    calls = 0
    submitted = []
    job = {
        "id": "job_1",
        "required_skill": "code.build_project",
        "payload": payload(),
        "claim_token": "lease-proof",
        "lease_id": "lease_1",
        "lease_generation": 1,
    }

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            nonlocal calls
            calls += 1
            if failure == "connection":
                raise ConnectionRefusedError("private connection detail")
            if failure == "url":
                raise worker.urllib.error.URLError("private DNS detail")
            raise worker.urllib.error.HTTPError(
                "http://private.invalid/secret", failure, "private body", {}, None
            )

    def request(origin: str, path: str, token: str, method: str, body: Any, **kwargs: Any) -> Any:
        if path.endswith("/claim"):
            return job
        if path.endswith("/result"):
            submitted.append(body)
        return {"status": "ok"}

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(worker.protocol, "request", request)
    runner = Runner()
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b")
    assert worker.run_once("https://control.example", "agt_1", "credential", generator, runner)
    assert calls == 1 and runner.calls == 0
    assert len(submitted) == 1 and submitted[0]["status"] == "failed"
    assert submitted[0]["error"] == "project_model_" + category
    assert "result" not in submitted[0]
    assert category in caplog.text and "private" not in caplog.text
    assert not worker._JOB_LOCK.locked()


def test_timeout_is_submitted_as_completed_iteration_for_its_existing_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    submitted = []
    claimed = []

    def request(origin: str, path: str, token: str, method: str, body: Any, **kwargs: Any) -> Any:
        if path.endswith("/claim"):
            job_id = "job_" + str(len(claimed) + 1)
            claimed.append(job_id)
            return {
                "id": job_id,
                "required_skill": "code.build_project",
                "payload": payload(),
                "claim_token": "lease-proof",
                "lease_id": job_id,
                "lease_generation": 1,
            }
        if path.endswith("/result"):
            submitted.append(body)
        return {"status": "ok"}

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            nonlocal calls
            calls += 1
            raise TimeoutError("private detail")

    monkeypatch.setattr(worker.protocol, "request", request)
    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b")
    runner = Runner()
    for attempt in (1, 2):
        assert worker.run_once("https://control.example", "agt_1", "credential", generator, runner)
        assert calls == len(claimed) == len(submitted) == attempt
        assert submitted[-1]["lease_id"] == claimed[-1]
        assert submitted[-1]["status"] == "completed"
        assert submitted[-1]["result"]["action"] == "continue"
        assert submitted[-1]["result"]["checks"] == []
    assert runner.calls == 0


def test_timeout_recovery_still_discards_a_lost_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    active_checks = 0

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            nonlocal calls
            calls += 1
            raise TimeoutError("private detail")

    def active() -> None:
        nonlocal active_checks
        active_checks += 1
        if active_checks > 2:
            raise worker.protocol.LeaseLost("cancelled")

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    runner = Runner()
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b")
    with pytest.raises(worker.protocol.LeaseLost):
        worker.run_iteration(payload(), generator, runner, active)
    assert calls == 1 and runner.calls == 0


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("answered", [False, True])
def test_all_generation_phases_allow_only_one_complete_file_edit(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    answered: bool,
) -> None:
    captured = []
    data = {
        **payload(),
        "conversation": [
            {"role": "assistant", "content": "Web or desktop?"},
            {"role": "user", "content": "A local web application with persistent contacts."},
        ],
    }
    if not answered:
        data["conversation"] = []
    initial = answered and not existing
    response = step(edits=[{"path": "models.py", "content": "VALUE = 1\n"}])
    if existing:
        data["files"] = [{"path": "app.py", "content": "VALUE = 1\n"}]

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            captured.append(json.loads(request.data))
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
    result = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b").generate(data)
    assert len(captured) == 1
    body = captured[0]
    assert body["options"]["num_predict"] == 2000
    assert body["format"]["oneOf"][0]["properties"]["edits"]["maxItems"] == 1
    assert len(result["edits"]) == 1
    assert ("first implementation batch" in body["messages"][-1]["content"]) == initial


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("answered", [False, True])
def test_two_full_file_edits_are_rejected_wholesale_in_every_generation_phase(
    monkeypatch: pytest.MonkeyPatch, existing: bool, answered: bool
) -> None:
    data = payload()
    if existing:
        data["files"] = [{"path": "app.py", "content": "unchanged = True\n"}]
        data["base_revision_id"] = "revision_1"
        data["base_sha256"] = snapshot_sha(data["files"])
    if answered:
        data["conversation"] = [
            {"role": "assistant", "content": "Web or desktop?"},
            {"role": "user", "content": "Build a local web application."},
        ]
    original = copy.deepcopy(data)
    calls = 0
    response = step(
        edits=[
            {"path": "app.py", "content": "replaced = True\n"},
            {"path": "models.py", "content": "added = True\n"},
        ]
    )

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            nonlocal calls
            calls += 1
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
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b")
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert calls == 1 and runner.calls == 0
    assert result["action"] == "continue" and "one full-file edit" in result["message"]
    for field in ("files", "checks", "plan", "base_revision_id", "base_sha256"):
        assert result[field] == original[field]
    assert data == original


def test_one_full_edit_patch_and_deletion_preserve_other_files_in_maximum_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = [
        {"path": "app.py", "content": "APP = 1\n"},
        {"path": "models.py", "content": "MODEL = 1\n"},
        {"path": "obsolete.py", "content": "OBSOLETE = 1\n"},
        {"path": "README.md", "content": "Requirements and startup instructions"},
        *[{"path": f"keep{i}.py", "content": f"KEEP = {i}\n"} for i in range(76)],
    ]
    data = {
        **payload(),
        "files": files,
        "focus_paths": ["models.py", "app.py"],
        "base_revision_id": "revision_1",
        "base_sha256": snapshot_sha(files),
    }
    original = copy.deepcopy(data)
    calls = 0

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            nonlocal calls
            calls += 1
            body = json.loads(request.data)
            schema = body["format"]["oneOf"][0]
            choices = schema["properties"]["patches"]["items"]["oneOf"]
            model = next(
                choice
                for choice in choices
                if choice["properties"]["path"]["enum"] == ["models.py"]
            )
            identifier = model["properties"]["span_id"]["enum"][0]
            response = step(
                edits=[{"path": "app.py", "content": "APP = 2\n"}],
                patches=[{"path": "models.py", "span_id": identifier, "new": "MODEL = 2\n"}],
                deletions=["obsolete.py"],
                focus_paths=[],
            )
            assert Draft202012Validator(body["format"]).is_valid(response)
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
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b")
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert calls == runner.calls == 1 and result["action"] == "complete"
    updated = {item["path"]: item["content"] for item in result["files"]}
    assert len(files) == 80 and len(updated) == 79
    assert updated["app.py"] == "APP = 2\n" and updated["models.py"] == "MODEL = 2\n"
    assert "obsolete.py" not in updated
    for item in original["files"]:
        if item["path"] not in {"app.py", "models.py", "obsolete.py"}:
            assert updated[item["path"]] == item["content"]
    assert data == original and result["base_sha256"] == original["base_sha256"]


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
    with pytest.raises(ProjectError, match=rf"below {worker.MAX_PATCH_BYTES} UTF-8 bytes"):
        worker.resolve_model_patches(
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
                        "message": {"content": json.dumps(single_file_step())},
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


NODE_EMPTY_TAP = (
    "TAP version 13\n1..0\n# tests 0\n# suites 0\n# pass 0\n# fail 0\n"
    "# cancelled 0\n# skipped 0\n# todo 0\n"
)


def node_check(command: list[str], output: str, *, code: int = 5) -> dict[str, Any]:
    return {
        "command": command,
        "status": "failed",
        "exit_code": code,
        "output": output,
        "duration_ms": 1,
    }


def capture_project_request(
    monkeypatch: pytest.MonkeyPatch,
    data: dict[str, Any],
    response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    captured: list[dict[str, Any]] = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            captured.append(json.loads(request.data))
            return Response(
                json.dumps(
                    {
                        "message": {"content": json.dumps(response or single_file_step())},
                        "done": True,
                        "done_reason": "stop",
                    }
                ).encode()
            )

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b").generate(data)
    assert len(captured) == 1
    assert (
        sum(len(m["content"].encode()) for m in captured[0]["messages"]) <= worker.MAX_PROMPT_BYTES
    )
    return captured[0]


def missing_node_manifest_payload() -> dict[str, Any]:
    return {
        **payload(),
        "files": [{"path": "README.md", "content": "Create an inventory web application."}],
        "checks": [
            node_check(["npm", "run", "build"], "FileNotFoundError: package.json", code=1),
            node_check(["node", "--test"], NODE_EMPTY_TAP),
        ],
    }


def test_missing_node_manifest_prioritizes_creation_and_preserves_runtime_choice_and_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = capture_project_request(monkeypatch, missing_node_manifest_payload())
    task = request["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:")[1]
    assert "root package.json is missing" in task
    assert "before adding tests" in task
    assert "Python" in task and "static HTML/JS" in task
    validator = Draft202012Validator(request["format"])
    Draft202012Validator.check_schema(request["format"])
    for runtime in ("node", "python_node"):
        rejected = {
            **step(runtime=runtime, edits=[{"path": "README.md", "content": "Later"}]),
            "patches": [],
            "focus_paths": [],
        }
        assert not validator.is_valid(rejected)
        accepted = {
            **rejected,
            "edits": [
                {
                    "path": "package.json",
                    "content": '{"scripts":{"build":"node --check app.js"}}',
                }
            ],
        }
        assert validator.is_valid(accepted)
    python = {**single_file_step(), "patches": [], "focus_paths": []}
    assert validator.is_valid(python)
    read = {
        **step(action="continue", runtime="node", edits=[], requested_checks=[]),
        "patches": [],
        "focus_paths": ["README.md"],
    }
    assert validator.is_valid(read)
    capture_project_request(monkeypatch, missing_node_manifest_payload(), read)
    assert all(next(iter(branch["properties"])) == "edits" for branch in request["format"]["oneOf"])


@pytest.mark.parametrize(
    "changes",
    [
        {"edits": [{"path": "README.md", "content": "Will create package later"}]},
        {"edits": []},
        {"patches": [{"path": "README.md", "old": "inventory", "new": "stock"}]},
        {"deletions": ["README.md"]},
    ],
)
def test_missing_node_manifest_rejects_model_that_ignores_creation_constraint(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    response = step(
        runtime="node",
        edits=[{"path": "package.json", "content": '{"private":true}'}],
    )
    response.update(changes)
    data = missing_node_manifest_payload()
    if response.get("patches"):
        addresses = worker.addressed_patch_spans(worker.model_context(data), data)
        identifier, address = next(iter(addresses.items()))
        response["patches"] = [
            {
                "path": address["path"],
                "span_id": identifier,
                "new": "Changed documentation",
            }
        ]
    with pytest.raises(worker.ModelStepError, match="package.json"):
        capture_project_request(monkeypatch, data, response)


@pytest.mark.parametrize("runtime", ["node", "python_node"])
def test_missing_node_manifest_accepts_model_that_creates_it(
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
) -> None:
    response = step(
        runtime=runtime, edits=[{"path": "package.json", "content": '{"private":true}'}]
    )
    capture_project_request(monkeypatch, missing_node_manifest_payload(), response)


def test_real_empty_node_test_receipt_prioritizes_real_test_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "export function quantity(value) { return Number(value); }"
    data = {
        **payload(),
        "files": [
            {"path": "package.json", "content": '{"type":"module"}'},
            {"path": "app.js", "content": source},
        ],
        "checks": [node_check(["node", "--test"], NODE_EMPTY_TAP)],
    }
    request = capture_project_request(monkeypatch, data)
    task = request["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:")[1]
    assert "Node test runner collected NO TESTS" in task
    assert "node:test" in task and "*.test.js" in task and "*.test.mjs" in task
    assert "real API" in task and "do not invent" in task
    assert source in request["messages"][-1]["content"]
    assert next(iter(request["format"]["oneOf"][0]["properties"])) == "edits"


@pytest.mark.parametrize("runtime", ["python", "node"])
def test_missing_tests_cannot_select_an_empty_mutation(
    monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    path = "app.py" if runtime == "python" else "app.js"
    data = {
        **payload(),
        "files": [{"path": path, "content": "value = 1"}],
        "checks": [
            node_check(["python", "-m", "pytest", "-q"], "no tests ran in 0.00s")
            if runtime == "python"
            else node_check(["node", "--test"], NODE_EMPTY_TAP)
        ],
    }
    if runtime == "node":
        data["files"].append({"path": "package.json", "content": '{"type":"module"}'})
    request = capture_project_request(monkeypatch, data)
    validator = Draft202012Validator(request["format"])
    empty = step(action="continue", edits=[], patches=[], focus_paths=[])
    assert not validator.is_valid(empty)
    read = {**empty, "focus_paths": [path]}
    assert validator.is_valid(read)
    with pytest.raises(worker.ModelStepError, match="returned no test file"):
        capture_project_request(monkeypatch, data, empty)
    capture_project_request(monkeypatch, data, read)


@pytest.mark.parametrize(
    "command",
    [
        ["npm", "run", "build"],
        ["npm", "install", "--ignore-scripts"],
        ["python", "-m", "compileall", "-q", "."],
    ],
)
def test_empty_node_tests_do_not_hide_a_failed_build_or_dependency_check(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    diagnostic = "Required application module app.js is missing"
    data = {
        **payload(),
        "files": [{"path": "package.json", "content": "{}"}],
        "checks": [
            node_check(command, diagnostic, code=1),
            node_check(["node", "--test"], NODE_EMPTY_TAP),
        ],
    }
    request = capture_project_request(monkeypatch, data)
    task = request["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:")[1]
    assert "Node test runner collected NO TESTS" not in task
    assert diagnostic in task and "node --test" in task
    assert next(iter(request["format"]["oneOf"][0]["properties"])) == "patches"


@pytest.mark.parametrize(
    "check",
    [
        node_check(
            ["node", "--test"],
            NODE_EMPTY_TAP.replace("# tests 0", "# tests 1").replace("# skipped 0", "# skipped 1"),
        ),
        node_check(["node", "--test"], NODE_EMPTY_TAP.replace("# fail 0", "# fail 1")),
        node_check(["node", "--test"], NODE_EMPTY_TAP.replace("# cancelled 0", "# cancelled 1")),
        node_check(["node", "--test"], NODE_EMPTY_TAP.replace("# todo 0", "# todo 1")),
        node_check(["node", "--test"], "not ok 1 - import failed\n" + NODE_EMPTY_TAP),
        node_check(["node", "--test"], "Error: cannot import module"),
        node_check(["node", "--test"], NODE_EMPTY_TAP, code=1),
        node_check(["npm", "test"], NODE_EMPTY_TAP),
        node_check(["node", "--test"], "# fail 1\n" + NODE_EMPTY_TAP),
        {**node_check(["node", "--test"], NODE_EMPTY_TAP, code=0), "status": "passed"},
    ],
)
def test_node_repair_does_not_misclassify_other_receipts_as_empty_tests(
    monkeypatch: pytest.MonkeyPatch,
    check: dict[str, Any],
) -> None:
    data = {
        **payload(),
        "files": [{"path": "app.js", "content": "export const x = 1;"}],
        "checks": [check],
    }
    request = capture_project_request(monkeypatch, data)
    task = request["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:")[1]
    assert "Node test runner collected NO TESTS" not in task


@pytest.mark.parametrize(
    "files,check",
    [
        (
            [{"path": "package.json", "content": "{}"}],
            node_check(["npm", "run", "build"], "syntax error", code=1),
        ),
        (
            [{"path": "app.py", "content": "print('hello')"}],
            node_check(["python", "-m", "pytest", "-q"], "no tests ran"),
        ),
        (
            [{"path": "README.md", "content": "Project"}],
            {**node_check(["npm", "run", "build"], "ok", code=0), "status": "passed"},
        ),
    ],
)
def test_other_repairs_do_not_impose_a_node_manifest(
    monkeypatch: pytest.MonkeyPatch,
    files: list[dict[str, str]],
    check: dict[str, Any],
) -> None:
    request = capture_project_request(monkeypatch, {**payload(), "files": files, "checks": [check]})
    task = request["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:")[1]
    assert "root package.json is missing" not in task
    response = {
        **step(runtime="node", edits=[{"path": "app.js", "content": "export const x=1;"}]),
        "patches": [],
        "focus_paths": [],
    }
    assert Draft202012Validator(request["format"]).is_valid(response)


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


@pytest.mark.parametrize("prefix", ["", "./", "/workspace/project/"])
@pytest.mark.parametrize("suffix", [":", ": in test_store", ": AssertionError", ""])
def test_pytest_traceback_locations_select_exact_project_lines(prefix: str, suffix: str) -> None:
    diagnostic = f"  {prefix}tests/test_store.py:18{suffix}\n"
    assert worker.project_traceback_line("tests/test_store.py", diagnostic) == 18


@pytest.mark.parametrize(
    "displayed",
    [
        "other/tests/test_store.py:18:",
        "/dependencies/tests/test_store.py:18:",
        "/workspace/project/other/tests/test_store.py:18:",
        "mytests/test_store.py:18:",
        "tests/test_store.py.old:18:",
        "tests/test_store.py:18oops",
        "tests/test_store.py:0:",
        "FAILED tests/test_store.py:18:",
    ],
)
def test_pytest_traceback_locations_reject_foreign_or_ambiguous_paths(displayed: str) -> None:
    assert worker.project_traceback_line("tests/test_store.py", displayed) is None


def test_pytest_repair_keeps_test_lifecycle_target_in_small_address_budget() -> None:
    test_source = (
        "import pytest\nfrom crm_store import Store\n\n"
        "def test_insert_customer(tmp_path):\n"
        "    store = Store(tmp_path / 'crm.sqlite')\n"
        "    customer_id = store._insert('INSERT INTO customers(name) VALUES (?)', ('Émilie',))\n"
        "    store.close()\n"
        "    store = Store(tmp_path / 'crm.sqlite')\n"
        "    assert store._rows('SELECT name FROM customers')[0]['name'] == 'Émilie'\n"
        "    store.close()\n"
        "    with pytest.raises(ValueError):\n"
        "        store._customer(999)\n"
    )
    store_source = (
        "class Store:\n"
        "    def _create_tables(self):\n"
        "        cursor = self.connection.cursor()\n"
        "        cursor.execute('CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT)')\n"
        "    def _customer(self, customer_id):\n"
        "        return self._rows('SELECT * FROM customers WHERE id=?', (customer_id,))\n"
        "    def _rows(self, sql, params=()):\n"
        "        cursor = self.connection.cursor()\n"
        "        return cursor.execute(sql, params).fetchall()\n"
    )
    diagnostics = (
        "tests/test_store.py:12: \n"
        "crm_store.py:6: in _customer\n"
        "E sqlite3.ProgrammingError: Cannot operate on a closed database.\n"
        "crm_store.py:8: ProgrammingError\n"
    )
    files = [
        {"path": "tests/test_store.py", "content": test_source},
        {"path": "crm_store.py", "content": store_source},
    ]
    data = {**payload(), "files": files, "checks": [{"output": diagnostics}]}
    context = {"selected_complete_files": files, "selected_file_fragments": []}
    addresses = worker.addressed_patch_spans(context, data, max_bytes=500)
    first = next(iter(addresses.values()))
    assert first["path"] == "tests/test_store.py"
    assert first["old"] == test_source[test_source.index("def test_insert_customer") :]
    assert "store.close()" in first["old"] and "pytest.raises(ValueError)" in first["old"]
    assert all("CREATE TABLE" not in address["old"] for address in addresses.values())


@pytest.mark.parametrize("mutation", ["repair", "read", "unchanged_edit", "empty", "new_module"])
def test_repairs_and_no_op_steps_preserve_accepted_milestones(mutation: str) -> None:
    files = [{"path": "app.py", "content": "VALUE = 1\n"}]
    data = {
        **payload(),
        "files": files,
        "plan": [
            "Implement customers and quotes",
            "Add calendar and email drafts",
            "Test and document",
        ],
    }
    edits = []
    focus = []
    if mutation == "repair":
        data["checks"] = [node_check(["python", "-m", "pytest", "-q"], "AssertionError", code=1)]
        edits = [{"path": "app.py", "content": "VALUE = 2\n"}]
    elif mutation == "read":
        focus = ["app.py"]
    elif mutation == "unchanged_edit":
        edits = files
    elif mutation == "new_module":
        edits = [{"path": "customers.py", "content": "def customers():\n    return []\n"}]
    response = step(action="continue", plan=[], edits=edits, focus_paths=focus)
    result = worker.run_iteration(data, Generator(response), Runner(), lambda: None)
    assert result["plan"] == data["plan"]


def test_repair_task_preserves_user_scope_and_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    request = "Repair only tests/test_store.py; preserve Store and all persistence assertions."
    data = {
        **payload(),
        "files": [
            {"path": "tests/test_store.py", "content": "def test_store():\n    assert False\n"}
        ],
        "plan": ["Implement the remaining CRM modules", "Test and document"],
        "conversation": [{"role": "user", "content": request}],
        "checks": [node_check(["python", "-m", "pytest", "-q"], "AssertionError", code=1)],
    }
    body = capture_project_request(monkeypatch, data)
    task = body["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:", 1)[1]
    assert request in task
    assert "test lifecycle" in task and "do not weaken" in task
    assert "Include README.md" not in task
    for branch in body["format"]["oneOf"]:
        assert branch["properties"]["plan"] == {"const": data["plan"]}


@pytest.mark.parametrize("change", ["edit", "patch", "new_file"])
def test_invalid_python_candidate_preserves_snapshot_without_execution(change: str) -> None:
    original = "def customers():\n    return []\n"
    data = {
        **payload(),
        "files": [{"path": "crm.py", "content": original}],
        "plan": ["Implement customers", "Implement calendar"],
        "checks": [node_check(["python", "-m", "pytest", "-q"], "AssertionError", code=1)],
    }
    path = "crm_calendar.py" if change == "new_file" else "crm.py"
    response = step(action="continue", edits=[{"path": path, "content": "def broken(:\n"}])
    if change == "patch":
        response = step(
            action="continue",
            edits=[],
            patches=[
                {"path": path, "old": "    return []\n", "new": "        return [\n"},
            ],
        )
    generator, runner = Generator(response), Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert runner.calls == 0 and generator.calls == 1
    assert result["files"] == data["files"] and result["checks"] == data["checks"]
    assert result["plan"] == data["plan"] and result["action"] == "continue"
    assert path in result["message"] and "SyntaxError" in result["message"]
    assert "proposed" in result["message"] and "No changes" in result["message"]


def test_syntax_preflight_does_not_execute_source_or_reject_unchanged_broken_files() -> None:
    files = [{"path": "old.py", "content": "def previously_broken(:\n"}]
    data = {**payload(), "files": files}
    response = step(
        action="continue",
        edits=[
            {"path": "new.py", "content": "raise RuntimeError('must not execute on the host')\n"},
        ],
    )
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert runner.calls == 1
    assert {item["path"]: item["content"] for item in result["files"]} == {
        item["path"]: item["content"] for item in files + response["edits"]
    }


def test_parser_complexity_rejection_preserves_valid_snapshot_without_execution() -> None:
    data = {
        **payload(),
        "files": [{"path": "crm.py", "content": "VALUE = 1\n"}],
        "plan": ["Implement customers", "Implement calendar"],
        "checks": [node_check(["python", "-m", "pytest", "-q"], "AssertionError", code=1)],
    }
    # Valid bounded contract text can still exceed CPython's AST recursion limit.
    deep_expression = "VALUE = " + "1+" * 15000 + "1\n"
    assert len(deep_expression.encode()) < 64_000
    response = step(action="continue", edits=[{"path": "crm.py", "content": deep_expression}])
    generator, runner = Generator(response), Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert generator.calls == 1 and runner.calls == 0
    assert result["files"] == data["files"] and result["checks"] == data["checks"]
    assert result["plan"] == data["plan"] and result["action"] == "continue"
    assert result["message"] == (
        "The proposed Python source exceeds the parser complexity limit. "
        "No changes or checks were accepted. Return one smaller complete module "
        "or simplify the proposed patch."
    )


@pytest.mark.parametrize("kind", ["empty", "identical"])
def test_non_effective_mutation_preserves_snapshot_and_receipts_without_checks(kind: str) -> None:
    files = [{"path": "crm.py", "content": "VALUE = 1\n"}]
    data = {**payload(), "files": files, "plan": ["Implement remaining modules"]}
    response = step(
        action="continue",
        edits=files if kind == "identical" else [],
        message="I will implement the next module.",
    )
    generator, runner = Generator(response), Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert generator.calls == 1 and runner.calls == 0
    assert result["files"] == files and result["checks"] == data["checks"]
    assert result["plan"] == data["plan"]
    assert "no effective project operation" in result["message"]
    assert "I will" not in result["message"]


def test_explicit_checks_only_request_still_executes_the_existing_snapshot() -> None:
    files = [{"path": "crm.py", "content": "VALUE = 1\n"}]
    data = {**payload(), "files": files, "plan": ["Test existing source"]}
    response = step(
        action="continue", edits=[], requested_checks=[["python", "-m", "pytest", "-q"]]
    )
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert runner.calls == 1 and runner.files == files
    assert result["checks"][0]["status"] == "passed"


def test_mutation_schema_requires_an_operation_but_preserves_read_and_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {**payload(), "files": [{"path": "crm.py", "content": "VALUE = 1\n"}]}
    body = capture_project_request(monkeypatch, data)
    validator = Draft202012Validator(body["format"])
    response = step(action="continue", edits=[], patches=[], focus_paths=[])
    assert not validator.is_valid(response)
    assert validator.is_valid(
        {**response, "edits": [{"path": "people.py", "content": "VALUE=2\n"}]}
    )
    assert validator.is_valid({**response, "deletions": ["crm.py"]})
    assert validator.is_valid({**response, "requested_checks": [["python", "-m", "pytest", "-q"]]})
    assert validator.is_valid({**response, "focus_paths": ["crm.py"]})
    assert validator.is_valid({**response, "action": "clarify", "message": "Which interface?"})
    patch_branch = next(
        branch
        for branch in body["format"]["oneOf"]
        if branch["properties"]["patches"].get("minItems") == 1
    )
    patch_choice = patch_branch["properties"]["patches"]["items"]["oneOf"][0]["properties"]
    patch = {
        "path": patch_choice["path"]["enum"][0],
        "span_id": patch_choice["span_id"]["enum"][0],
        "new": "VALUE = 2\n",
    }
    assert validator.is_valid({**response, "patches": [patch]})
    assert validator.is_valid(
        {
            **response,
            "patches": [patch],
            "edits": [{"path": "people.py", "content": "VALUE = 3\n"}],
        }
    )
    assert "anyOf" not in json.dumps(body["format"])
    with pytest.raises(worker.ModelStepError, match="no effective project operation"):
        capture_project_request(monkeypatch, data, response)


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
                        "message": {"content": json.dumps(single_file_step())},
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


def test_resolved_patch_rejects_noop_after_terminal_newline_preservation() -> None:
    data = {**payload(), "files": [{"path": "app.py", "content": "value = 1\n"}]}
    addresses = worker.addressed_patch_spans(worker.model_context(data), data)
    address = next(item for item in addresses.values() if item["old"] == "value = 1\n")
    response = step(
        edits=[],
        patches=[{"path": "app.py", "span_id": address["span_id"], "new": "value = 1"}],
    )
    with pytest.raises(ProjectError, match="identical to the selected source span"):
        worker.resolve_model_patches(response, addresses)


def test_resolved_patch_rejects_replacement_over_utf8_byte_limit() -> None:
    data = {**payload(), "files": [{"path": "app.py", "content": "value = 1\n"}]}
    addresses = worker.addressed_patch_spans(worker.model_context(data), data)
    address = next(item for item in addresses.values() if item["old"] == "value = 1\n")
    response = step(
        edits=[],
        patches=[
            {
                "path": "app.py",
                "span_id": address["span_id"],
                "new": "é" * (worker.MAX_PATCH_BYTES // 2 + 1),
            }
        ],
    )
    with pytest.raises(ProjectError, match=rf"below {worker.MAX_PATCH_BYTES} UTF-8 bytes"):
        worker.resolve_model_patches(response, addresses)


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
        def generate(
            self, payload: dict[str, Any], ensure_active: Any | None = None
        ) -> dict[str, Any]:
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


def test_passing_submodule_tests_do_not_force_documentation_or_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        **payload(),
        "files": [
            {"path": "app.py", "content": "VALUE = 1\n"},
            {"path": "tests/test_app.py", "content": "def test_value(): assert True\n"},
        ],
        "plan": ["Implement customers", "Implement calendar", "Document setup"],
        "checks": [
            {
                "command": ["python", "-m", "pytest", "-q"],
                "status": "passed",
                "exit_code": 0,
                "duration_ms": 1,
                "output": "1 passed",
            }
        ],
        "conversation": [
            {"role": "user", "content": "Build the application"},
            {"role": "assistant", "content": "Required before readiness: README.md"},
        ],
    }
    response = step(
        action="continue",
        edits=[{"path": "customers.py", "content": "def customers():\n    return []\n"}],
        patches=[],
        requested_checks=[],
        run_instructions="python -m pytest -q",
        focus_paths=[],
    )
    body = capture_project_request(monkeypatch, data, response)
    schema = body["format"]["oneOf"][0]
    assert Draft202012Validator.check_schema(body["format"]) is None
    assert Draft202012Validator(body["format"]).is_valid(response)
    assert "continue" in schema["properties"]["action"]["enum"]
    assert schema["properties"]["edits"]["maxItems"] == 1
    assert body["options"]["num_predict"] == worker.MAX_OUTPUT_TOKENS == 2000
    task = body["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:", 1)[1]
    assert "Create exactly README.md" not in task
    result = worker.run_iteration(data, Generator(response), Runner(), lambda: None)
    assert result["action"] == "continue"
    assert any(file["path"] == "customers.py" for file in result["files"])


def test_readme_only_completion_runs_checks_on_the_new_revision() -> None:
    checks = [
        {
            "command": ["python", "-m", "compileall", "-q", "."],
            "status": "passed",
            "exit_code": 0,
            "duration_ms": 1,
            "output": "",
        },
        {
            "command": ["python", "-m", "pytest", "-q"],
            "status": "passed",
            "exit_code": 0,
            "duration_ms": 2,
            "output": "5 passed",
        },
    ]
    data = {
        **payload(),
        "files": [{"path": "app.py", "content": "VALUE = 1\n"}],
        "plan": ["Keep the application", "Document setup"],
        "checks": checks,
        "conversation": [
            {"role": "user", "content": "Build the application"},
            {"role": "assistant", "content": worker.MODEL_TIMEOUT_DIAGNOSTIC},
        ],
    }
    response = step(
        action="complete",
        edits=[{"path": "README.md", "content": "# App\n\nRun `pytest`.\n"}],
        plan=data["plan"],
        run_instructions="python -m pytest -q",
    )
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert runner.calls == 1
    assert result["action"] == "complete"
    assert result["checks"] != checks
    assert result["checks"][0]["status"] == "passed"
    assert {item["path"] for item in result["files"]} == {"app.py", "README.md"}


def test_no_op_cannot_promote_passing_submodule_checks_to_completion() -> None:
    checks = [
        {
            "command": ["python", "-m", "pytest", "-q"],
            "status": "passed",
            "exit_code": 0,
            "duration_ms": 2,
            "output": "5 passed",
        }
    ]
    data = {
        **payload(),
        "files": [
            {"path": "app.py", "content": "VALUE = 1\n"},
            {"path": "README.md", "content": "Storage module implemented; CRM features pending."},
        ],
        "plan": ["Implement customers", "Implement calendar", "Document setup"],
        "checks": checks,
        "conversation": [{"role": "assistant", "content": "Progress"}],
    }
    response = step(
        action="complete",
        edits=[],
        plan=[],
        run_instructions="python -m pytest -q",
    )
    runner = Runner()
    result = worker.run_iteration(data, Generator(response), runner, lambda: None)
    assert result["action"] == "continue"
    assert snapshot_sha(result["files"]) == snapshot_sha(data["files"])
    assert result["plan"] == data["plan"]


def test_new_user_request_takes_priority_over_missing_readme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        **payload(),
        "files": [{"path": "app.py", "content": "VALUE = 1\n"}],
        "checks": [
            {
                "command": ["python", "-m", "pytest", "-q"],
                "status": "passed",
                "exit_code": 0,
                "duration_ms": 1,
                "output": "1 passed",
            }
        ],
        "conversation": [
            {"role": "assistant", "content": "Required before readiness: README.md"},
            {"role": "user", "content": "Add CSV export first."},
        ],
    }
    body = capture_project_request(monkeypatch, data)
    schema = body["format"]["oneOf"][0]
    assert schema["properties"]["edits"]["maxItems"] == 1
    assert schema["properties"]["edits"]["items"]["properties"]["path"].get(
        "const"
    ) is None
    assert body["options"]["num_predict"] == worker.MAX_OUTPUT_TOKENS
    task = body["messages"][-1]["content"].split("YOUR TASK FOR THIS ITERATION:", 1)[1]
    assert "Add CSV export first." in task
    assert "Create exactly README.md" not in task


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


def compact_recovery_payload() -> dict[str, Any]:
    return {
        **payload(),
        "files": [{"path": "app.py", "content": "VALUE = 1\n"}],
        "plan": ["Preserve accepted behavior", "Repair the failing test"],
        "checks": [node_check(["python", "-m", "pytest", "-q"], "app.py:1: failure", code=1)],
        "conversation": [
            {"role": "user", "content": "Keep the existing requirements and repair the failure."},
            {"role": "assistant", "content": worker.MODEL_TIMEOUT_DIAGNOSTIC},
        ],
        "iteration": 3,
    }


def compact_step(**changes: Any) -> dict[str, Any]:
    return {
        "action": "continue",
        "message": "Correction à vérifier.",
        "edits": [{"path": "app.py", "content": "VALUE = 2\n"}],
        "patches": [],
        "focus_paths": [],
        "run_instructions": "python -m pytest -q",
        "runtime": "python",
        **changes,
    }


def test_timeout_repair_uses_smaller_contract_and_preserves_worker_owned_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = compact_recovery_payload()
    data["memory"] = {
        "mode": "semantic",
        "reason": "matched",
        "items": [{"summary": "OMIT_MEMORY"}],
    }
    original = copy.deepcopy(data)
    body = capture_project_request(monkeypatch, data, compact_step())
    assert data == original
    assert body["options"]["num_predict"] == 512
    assert worker.MAX_MODEL_WALL_SECONDS == 240
    assert sum(len(m["content"].encode()) for m in body["messages"]) <= 10_000
    assert data["conversation"][0] in body["messages"]
    assert "OMIT_MEMORY" not in json.dumps(body["messages"])
    validator = Draft202012Validator(body["format"])
    assert validator.is_valid(compact_step())
    assert not validator.is_valid(compact_step(plan=["model cannot rewrite the plan"]))
    expanded = worker.expand_compact_repair(compact_step(), data)
    assert expanded["plan"] == data["plan"] and expanded["plan"] is not data["plan"]
    assert expanded["requested_checks"] == expanded["deletions"] == []


def test_compact_recovery_keeps_missing_node_manifest_priority_and_python_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        **missing_node_manifest_payload(),
        "conversation": compact_recovery_payload()["conversation"],
    }
    manifest = compact_step(
        runtime="node",
        edits=[
            {"path": "package.json", "content": '{"scripts":{"build":"node --check app.js"}}\n'}
        ],
    )
    body = capture_project_request(monkeypatch, data, manifest)
    validator = Draft202012Validator(body["format"])
    assert validator.is_valid(manifest)
    assert not validator.is_valid(compact_step(runtime="node"))
    assert validator.is_valid(compact_step(runtime="python"))
    assert validator.is_valid(compact_step(runtime="node", edits=[], focus_paths=["README.md"]))
    task = body["messages"][-1]["content"]
    assert "root package.json is missing" in task and "static HTML/JS" in task
    assert body["options"]["num_predict"] == 512


def test_compact_recovery_schema_permits_one_addressed_patch_or_read_but_no_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = capture_project_request(monkeypatch, compact_recovery_payload(), compact_step())
    branch = next(b for b in body["format"]["oneOf"] if b["properties"]["patches"]["maxItems"] == 1)
    address = branch["properties"]["patches"]["items"]["oneOf"][0]["properties"]["span_id"]["enum"][
        0
    ]
    patch = {"path": "app.py", "span_id": address, "new": "VALUE = 2\n"}
    validator = Draft202012Validator(body["format"])
    assert validator.is_valid(compact_step(edits=[], patches=[patch]))
    assert validator.is_valid(compact_step(edits=[], focus_paths=["app.py"]))
    assert not validator.is_valid(compact_step(patches=[patch]))
    assert not validator.is_valid(compact_step(edits=[], patches=[patch, patch]))
    assert not validator.is_valid(compact_step(edits=[], focus_paths=["app.py", "app.py"]))
    assert not validator.is_valid(compact_step(edits=[{"path": "app.py", "content": "x" * 801}]))
    assert not validator.is_valid(compact_step(message="x" * 161))
    assert not validator.is_valid(compact_step(run_instructions="x" * 241))


@pytest.mark.parametrize(
    "changes",
    [
        {"plan": ["invented"]},
        {"edits": []},
        {"edits": [{"path": "app.py", "content": "x" * 801}]},
        {"focus_paths": ["app.py"]},
        {"message": "x" * 161},
    ],
)
def test_compact_recovery_enforces_limits_after_model_response(changes: dict[str, Any]) -> None:
    with pytest.raises(ProjectError, match="compact repair"):
        worker.expand_compact_repair(compact_step(**changes), compact_recovery_payload())


@pytest.mark.parametrize("mode", ["edit", "patch"])
def test_compact_repair_reaches_normal_merge_and_checks(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    data = compact_recovery_payload()
    requests = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, **kwargs: Any) -> Response:
            body = json.loads(request.data)
            requests.append(body)
            response = compact_step()
            if mode == "patch":
                branch = next(
                    b
                    for b in body["format"]["oneOf"]
                    if b["properties"]["patches"]["maxItems"] == 1
                )
                span = branch["properties"]["patches"]["items"]["oneOf"][0]
                response = compact_step(
                    edits=[],
                    patches=[
                        {
                            "path": "app.py",
                            "span_id": span["properties"]["span_id"]["enum"][0],
                            "new": "VALUE = 2\n",
                        }
                    ],
                )
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
    runner = Runner(fail=True)
    result = worker.run_iteration(
        data,
        worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b"),
        runner,
        lambda: None,
    )
    assert len(requests) == runner.calls == 1
    assert result["files"] == [{"path": "app.py", "content": "VALUE = 2\n"}]
    assert runner.files == result["files"] and result["plan"] == data["plan"]
    assert result["action"] == "continue" and result["checks"][0]["status"] == "failed"


def test_compact_response_cannot_bypass_missing_node_manifest_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        **missing_node_manifest_payload(),
        "conversation": compact_recovery_payload()["conversation"],
    }
    with pytest.raises(worker.ModelStepError, match="create exactly package.json"):
        capture_project_request(monkeypatch, data, compact_step(runtime="node"))


def test_compact_context_retains_valid_large_unicode_user_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = compact_recovery_payload()
    data["objective"] = "Requirement " * 180
    data["conversation"][0]["content"] = "é" * 4_000
    response = compact_step(edits=[], focus_paths=["app.py"])
    body = capture_project_request(monkeypatch, data, response)
    size = sum(len(m["content"].encode()) for m in body["messages"])
    assert worker.MAX_RECOVERY_PROMPT_BYTES < size <= worker.MAX_PROMPT_BYTES
    assert data["conversation"][0] in body["messages"]
    assert body["options"]["num_predict"] == 512


@pytest.mark.parametrize("kind", ["no_timeout", "ordinary_assistant", "checks_passed"])
def test_compact_recovery_does_not_change_other_generation_phases(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    data = compact_recovery_payload()
    if kind == "no_timeout":
        data["conversation"].pop()
    elif kind == "ordinary_assistant":
        data["conversation"].append({"role": "assistant", "content": "Accepted the repair."})
        data["conversation"].append(
            {"role": "user", "content": "Resume with this new requirement."}
        )
    else:
        data["checks"][0].update(status="passed", exit_code=0)
        data["files"].append({"path": "README.md", "content": "Existing instructions"})
    body = capture_project_request(monkeypatch, data)
    assert body["options"]["num_predict"] == 2000
    assert "plan" in body["format"]["oneOf"][0]["properties"]


@pytest.mark.parametrize(
    "diagnostic", [worker.MODEL_TIMEOUT_DIAGNOSTIC, worker.MODEL_REPEATED_TIMEOUT_DIAGNOSTIC]
)
def test_first_user_resumed_repair_stays_compact_and_resets_timeout_pause_counter(
    monkeypatch: pytest.MonkeyPatch, diagnostic: str
) -> None:
    data = compact_recovery_payload()
    data["conversation"][-1]["content"] = diagnostic
    data["conversation"].extend(
        [
            {"role": "user", "content": "Continue la réparation."},
            {"role": "user", "content": "Conserve précisément cette exigence : éèà.\n"},
        ]
    )
    body = capture_project_request(monkeypatch, data, compact_step())
    assert body["options"]["num_predict"] == 512
    assert data["conversation"][-1] in body["messages"]
    assert not worker.previous_model_timeout(data)

    class Opener:
        def open(self, request: Any, **kwargs: Any) -> Any:
            raise TimeoutError("private detail")

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    runner = Runner()
    result = worker.run_iteration(
        data,
        worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b"),
        runner,
        lambda: None,
    )
    assert result["action"] == "continue" and runner.calls == 0
    assert result["message"] == worker.MODEL_TIMEOUT_DIAGNOSTIC
    assert result["files"] == data["files"] and result["checks"] == data["checks"]


@pytest.mark.parametrize("role", ["system", "tool"])
def test_timeout_recovery_rejects_intervening_non_user_roles(role: str) -> None:
    data = compact_recovery_payload()
    data["conversation"].extend(
        [{"role": role, "content": "intervening message"}, {"role": "user", "content": "Continue"}]
    )
    assert not worker.repair_follows_model_timeout(data)


def test_timeout_recovery_requires_exact_assistant_diagnostic() -> None:
    data = compact_recovery_payload()
    data["conversation"][-1]["content"] += " modified"
    data["conversation"].append({"role": "user", "content": worker.MODEL_TIMEOUT_DIAGNOSTIC})
    assert not worker.repair_follows_model_timeout(data)


@pytest.mark.parametrize("terminal", ["missing", "length"])
def test_compact_recovery_rejects_truncated_json_without_edits_or_checks(
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    data = compact_recovery_payload()
    raw = json.dumps({"message": {"content": '{"edits":['}, "done": False}).encode() + b"\n"
    if terminal == "length":
        raw += json.dumps(
            {"message": {"content": ""}, "done": True, "done_reason": "length"}
        ).encode()
    calls = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request: Any, **kwargs: Any) -> Response:
            calls.append(request)
            return Response(raw)

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    runner = Runner()
    result = worker.run_iteration(
        data,
        worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b"),
        runner,
        lambda: None,
    )
    assert len(calls) == 1 and runner.calls == 0
    assert result["files"] == data["files"] and result["checks"] == data["checks"]
    assert "incomplete" in result["message"]


def test_recovery_timeout_metrics_survive_partial_stream_without_source_or_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = compact_recovery_payload()

    class Response(io.BytesIO):
        status = 200
        count = 0

        def readline(self, size: int = -1) -> bytes:
            self.count += 1
            if self.count == 1:
                return b'{"message":{"content":"PRIVATE_SOURCE"},"done":false}\n'
            raise TimeoutError("PRIVATE_EXCEPTION")

    class Opener:
        def open(self, request: Any, **kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "qwen3-coder:30b")
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    metrics = generator.last_transport_metrics
    assert result["action"] == "clarify" and runner.calls == 0
    assert result["files"] == data["files"] and result["checks"] == data["checks"]
    assert metrics["compact_repair"] == 1 and metrics["output_token_limit"] == 512
    assert metrics["chunks"] == 1 and metrics["content_bytes"] == len("PRIVATE_SOURCE")
    assert metrics["terminal_received"] == 0 and metrics["response_bytes"] > 0
    assert 0 <= metrics["first_content_ms"] <= metrics["elapsed_ms"]
    assert all(type(value) is int and value >= 0 for value in metrics.values())
    assert "PRIVATE" not in caplog.text and "127.0.0.1" not in caplog.text
