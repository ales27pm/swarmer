from __future__ import annotations

import copy
import importlib.util
import io
import json
import threading
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def worker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "tested_code_worker", Path(__file__).with_name("code_worker.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def proposal(
    content: str = "def main():\n    print('CRM proposal')\n",
) -> dict[str, str]:
    return {
        "schema_version": "1.0",
        "path": "app.py",
        "content": content,
        "summary": "Python proposal; not executed or tested.",
    }


def job() -> dict[str, Any]:
    return {
        "id": "job_123",
        "required_skill": "code.generate_python",
        "payload": {"objective": "Crées une Application CRM en python"},
        "claim_token": "opaque-claim-proof",
        "lease_id": "lease_123",
        "lease_generation": 4,
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1/chat/completions"),
        ("http://localhost:11434", "http://127.0.0.1:11434/v1/chat/completions"),
        ("https://[::1]:443/v1/", "https://[::1]:443/v1/chat/completions"),
    ],
)
def test_model_url_is_pinned_to_numeric_loopback(
    worker: ModuleType, url: str, expected: str
) -> None:
    assert worker.validate_model_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://model.example/v1",
        "http://192.168.1.2/v1",
        "http://127.0.0.1.evil.invalid/v1",
        "http://user:pass@127.0.0.1/v1",
        "http://127.0.0.1/v1?",
        "http://127.0.0.1/v1#fragment",
        "http://127.0.0.1/v1/other",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:/v1",
        "http://127.0.0.1:99999/v1",
        "http://[::1%25lo0]/v1",
        " http://127.0.0.1/v1",
        "file:///tmp/model",
    ],
)
def test_model_url_rejects_remote_or_ambiguous_destinations(worker: ModuleType, url: str) -> None:
    with pytest.raises(ValueError):
        worker.validate_model_url(url)


@pytest.mark.parametrize(
    "model", ["qwen3.5:cloud", "x:CLOUD", "https://model.invalid", "", "bad model"]
)
def test_model_configuration_rejects_cloud_aliases_and_unsafe_identifiers(
    worker: ModuleType, model: str
) -> None:
    with pytest.raises(ValueError):
        worker.CodeGenerator("http://127.0.0.1:11434", model)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"objective": ""},
        {"objective": "x" * 4_001},
        {"objective": "bad\0value"},
        {"objective": "\ud800"},
        {"objective": "CRM", "path": "../app.py"},
        {"objective": "CRM", "model": "other"},
        {"objective": "CRM", "url": "http://attacker.invalid"},
        {"objective": "CRM", "capability_request": {}},
    ],
)
def test_job_accepts_only_a_bounded_objective(worker: ModuleType, payload: dict[str, Any]) -> None:
    claimed = job()
    claimed["payload"] = payload
    with pytest.raises(worker.GenerationError):
        worker.parse_job(claimed)


def test_job_rejects_other_skills(worker: ModuleType) -> None:
    claimed = job()
    claimed["required_skill"] = "workspace.write_text"
    with pytest.raises(worker.GenerationError):
        worker.parse_job(claimed)


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "../app.py"},
        {"schema_version": "2.0"},
        {"command": "python3 app.py"},
        {"content": ""},
        {"content": "# only a comment"},
        {"content": "```python\nprint('x')\n```"},
        {"content": "def broken("},
        {"content": "from flask import Flask\n"},
        {"content": "import django\n"},
        {"content": "import sqlite3, requests\n"},
        {"content": "from . import helpers\n"},
        {"content": "from helpers import work\n"},
        {"content": "\ud800"},
        {"content": "pass\n#" + "é" * 32_000},
        {"summary": "s" * 501},
        {"summary": "\ud800"},
    ],
)
def test_proposal_rejects_wrong_shape_bounds_or_syntax(
    worker: ModuleType, changes: dict[str, Any]
) -> None:
    with pytest.raises(worker.GenerationError):
        worker.validate_proposal({**proposal(), **changes})


def test_source_is_parsed_without_execution_or_rewriting(
    worker: ModuleType, tmp_path: Path
) -> None:
    forbidden = tmp_path / "must-not-exist"
    source = f"from pathlib import Path\nPath({str(forbidden)!r}).write_text('never run')\n"
    assert worker.validate_proposal(proposal(source))["content"] == source
    assert not forbidden.exists()
    exact_limit = "pass\n#" + "a" * (64_000 - len("pass\n#"))
    assert worker.validate_proposal(proposal(exact_limit))["content"] == exact_limit


def test_standard_library_submodules_and_future_imports_are_accepted(
    worker: ModuleType,
) -> None:
    source = (
        "from __future__ import annotations\nfrom http.server import HTTPServer\nimport sqlite3\n"
    )
    assert worker.validate_proposal(proposal(source))["content"] == source


@pytest.mark.parametrize(
    "raw",
    ['{"content":"one","content":"two"}', '{"content":NaN}', '{"content":Infinity}'],
)
def test_strict_json_rejects_duplicate_keys_and_constants(worker: ModuleType, raw: str) -> None:
    with pytest.raises(worker.GenerationError):
        worker._parse_json(raw)


def install_model_response(
    worker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    artifact: dict[str, Any] | None = None,
    finish_reason: str = "stop",
    error: Exception | None = None,
    raw_response: bytes | None = None,
) -> list[Any]:
    requests: list[Any] = []

    class Response(io.BytesIO):
        status = 200

        def read(self, size: int = -1) -> bytes:
            assert size == worker.MAX_RESPONSE_BYTES + 1
            return super().read(size)

    class Opener:
        def open(self, request: Any, timeout: float) -> Response:
            requests.append(request)
            assert timeout == 90
            if error is not None:
                raise error
            envelope = {
                "choices": [
                    {
                        "message": {"content": json.dumps(artifact or proposal())},
                        "finish_reason": finish_reason,
                    }
                ]
            }
            return Response(
                raw_response if raw_response is not None else json.dumps(envelope).encode()
            )

    def opener(*handlers: Any) -> Opener:
        assert len(handlers) == 2
        assert isinstance(handlers[0], worker.urllib.request.ProxyHandler)
        assert handlers[0].proxies == {}
        assert (
            handlers[1].redirect_request(None, None, 302, "redirect", {}, "http://evil.invalid")
            is None
        )
        return Opener()

    monkeypatch.setattr(worker.urllib.request, "build_opener", opener)
    return requests


def test_single_model_call_has_no_credentials_and_preserves_valid_source(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONGARS_AGENT_CREDENTIAL", "must-not-reach-model")
    requests = install_model_response(worker, monkeypatch)
    generator = worker.CodeGenerator("http://localhost:11434/v1", "local-python-model")
    assert generator.generate("Create CRM") == proposal()
    assert len(requests) == 1
    request = requests[0]
    body = json.loads(request.data)
    assert request.full_url == "http://127.0.0.1:11434/v1/chat/completions"
    assert request.get_header("Authorization") is None
    assert b"must-not-reach-model" not in request.data
    assert body["model"] == "local-python-model"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "maxLength" not in json.dumps(body["response_format"])


@pytest.mark.parametrize("reason", ["length", "tool_calls", "content_filter"])
def test_unfinished_generation_is_rejected_without_retry(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    requests = install_model_response(worker, monkeypatch, finish_reason=reason)
    with pytest.raises(worker.GenerationError):
        worker.CodeGenerator("http://127.0.0.1:11434", "model").generate("CRM")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"artifact": proposal("def invalid(")},
        {"raw_response": b"x" * 512_001},
        {"raw_response": b'{"choices": []}'},
        {"raw_response": b"not JSON"},
        {"error": TimeoutError("private transport context")},
        {"error": urllib.error.HTTPError("http://127.0.0.1", 302, "redirect", {}, None)},
    ],
)
def test_generation_failures_do_not_retry(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]
) -> None:
    requests = install_model_response(worker, monkeypatch, **options)
    with pytest.raises(worker.GenerationError):
        worker.CodeGenerator("http://127.0.0.1:11434", "model").generate("CRM")
    assert len(requests) == 1


def test_lease_renewal_and_terminal_result_keep_exact_proof(worker: ModuleType) -> None:
    calls: list[tuple[str, Any]] = []
    renewed = threading.Event()
    renewals = 0
    generated = 0

    def request(origin: str, path: str, token: str, method: str, body: Any) -> Any:
        nonlocal renewals
        assert token == "worker-credential"
        calls.append((path, copy.deepcopy(body)))
        if path.endswith("/claim"):
            return job()
        if path.endswith("/jobs/job_123/heartbeat"):
            renewals += 1
            if renewals >= 2:
                renewed.set()
        return {"status": "ok"}

    class Generator:
        def generate(self, objective: str) -> dict[str, str]:
            nonlocal generated
            generated += 1
            assert objective == job()["payload"]["objective"]
            assert renewed.wait(1)
            # A second concurrent caller cannot claim or generate any work.
            assert (
                worker.run_once("https://control.example", "agt_1", "worker-credential", self)
                is False
            )
            return proposal()

    worker.protocol.request = request
    assert worker.run_once(
        "https://control.example",
        "agt_1",
        "worker-credential",
        Generator(),
        heartbeat_interval_seconds=0.01,
    )
    assert generated == 1
    proof = {name: job()[name] for name in ("claim_token", "lease_id", "lease_generation")}
    results = [body for path, body in calls if path.endswith("/result")]
    assert results == [{**proof, "status": "completed", "result": proposal()}]
    assert all(body == proof for path, body in calls if path.endswith("/jobs/job_123/heartbeat"))
    assert [body["status"] for path, body in calls if path.endswith("/agt_1/heartbeat")] == [
        "online",
        "busy",
        "online",
    ]


@pytest.mark.parametrize("heartbeat_error", [409, 503])
def test_failed_or_uncertain_lease_suppresses_generated_proposal(
    worker: ModuleType, heartbeat_error: int
) -> None:
    results: list[Any] = []
    failed_renewal = threading.Event()
    renewals = 0
    heartbeat_thread: threading.Thread | None = None

    def request(origin: str, path: str, token: str, method: str, body: Any) -> Any:
        nonlocal renewals, heartbeat_thread
        if path.endswith("/claim"):
            return job()
        if path.endswith("/jobs/job_123/heartbeat"):
            renewals += 1
            if renewals > 1:
                heartbeat_thread = threading.current_thread()
                failed_renewal.set()
                raise urllib.error.HTTPError(
                    origin + path, heartbeat_error, "unavailable", {}, None
                )
        if path.endswith("/result"):
            results.append(body)
        return {"status": "ok"}

    class Generator:
        def generate(self, objective: str) -> dict[str, str]:
            assert failed_renewal.wait(1)
            # Wait until the heartbeat thread has recorded its failure.
            assert heartbeat_thread is not None
            heartbeat_thread.join(1)
            return proposal()

    worker.protocol.request = request
    assert worker.run_once(
        "https://control.example",
        "agt_1",
        "credential",
        Generator(),
        heartbeat_interval_seconds=0.01,
    )
    assert results == []
    assert not worker._JOB_LOCK.locked()


def test_invalid_job_fails_without_calling_model_or_exposing_objective(
    worker: ModuleType,
) -> None:
    results: list[Any] = []
    claimed = job()
    claimed["payload"]["url"] = "http://private.invalid"

    def request(origin: str, path: str, token: str, method: str, body: Any) -> Any:
        if path.endswith("/claim"):
            return claimed
        if path.endswith("/result"):
            results.append(body)
        return {"status": "ok"}

    class Generator:
        def generate(self, objective: str) -> dict[str, str]:
            raise AssertionError("invalid jobs must not reach the model")

    worker.protocol.request = request
    assert worker.run_once("https://control.example", "agt_1", "credential", Generator())
    assert results[0]["status"] == "failed"
    assert "result" not in results[0]
    assert "private.invalid" not in json.dumps(results)


def test_empty_queue_is_idle_and_releases_single_job_lock(worker: ModuleType) -> None:
    worker.protocol.request = lambda *args: None
    assert worker.run_once("https://control.example", "agt_1", "credential", object()) is False
    assert not worker._JOB_LOCK.locked()


def test_manifest_declares_only_proposal_generation(worker: ModuleType) -> None:
    manifest = json.loads(Path(__file__).with_name("agent-card.json").read_text())
    assert manifest["skills"] == [{"id": worker.SKILL, "risk": "low", "result_trust": "untrusted"}]
    assert manifest["limits"]["max_concurrency"] == 1
    assert manifest["policy"]["filesystem"] == "none"
    assert manifest["policy"]["writes"] is False
    assert manifest["policy"]["shell"] is False
