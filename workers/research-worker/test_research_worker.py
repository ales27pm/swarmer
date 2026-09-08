from __future__ import annotations

import copy
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def load_worker() -> ModuleType:
    path = Path(__file__).with_name("research_worker.py")
    spec = importlib.util.spec_from_file_location("mongars_research_worker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def claimed_job(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": "job_research_123",
        "required_skill": "research.query",
        "payload": payload or {"query": "lease-safe research", "max_results": 3},
        "claim_token": "claim-token-with-enough-entropy",
        "lease_id": "lease_research_123",
        "lease_generation": 7,
    }


@pytest.mark.parametrize(
    "origin",
    [
        "https://control.example",
        "https://control.example:8443",
        "http://localhost:8710",
        "http://127.0.0.1:8710",
        "http://[::1]:8710",
    ],
)
def test_control_plane_origin_accepts_https_and_loopback_http(origin: str) -> None:
    worker = load_worker()

    assert worker.validate_control_plane_origin(origin) == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://control.example",
        "https://user:password@control.example",
        "https://control.example/",
        "https://control.example/api",
        "https://control.example?",
        "https://control.example?debug=1",
        "https://control.example#fragment",
        "https://control.example\\@attacker.invalid",
        "https://control.example:",
        "https://control .example",
        "ftp://control.example",
        "not-a-url",
    ],
)
def test_control_plane_origin_rejects_non_bare_or_unsafe_urls(origin: str) -> None:
    worker = load_worker()

    with pytest.raises(ValueError, match="control-plane URL"):
        worker.validate_control_plane_origin(origin)


def test_control_plane_request_disables_redirects_with_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    handlers: list[Any] = []

    class RejectingOpener:
        def open(self, request: Any, timeout: float) -> Any:
            assert timeout == 30
            assert request.get_header("Authorization") == "Bearer agent-secret"
            assert len(handlers) == 1
            assert isinstance(handlers[0], worker._RejectRedirects)
            assert (
                handlers[0].redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {},
                    "https://attacker.invalid/capture",
                )
                is None
            )
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, None)

    def build_opener(*configured: Any) -> RejectingOpener:
        handlers.extend(configured)
        return RejectingOpener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)

    with pytest.raises(urllib.error.HTTPError):
        worker.control_plane_request(
            "https://control.example",
            "/agents/agent/claim",
            "agent-secret",
            "POST",
            {"wait_seconds": 0},
        )


def test_research_job_payload_is_exact_and_bounded() -> None:
    worker = load_worker()

    request = worker.parse_research_job(claimed_job())

    assert request.query == "lease-safe research"
    assert request.max_results == 3
    for payload in (
        {"query": "x", "url": "https://attacker.invalid"},
        {"query": "x", "max_results": True},
        {"query": "x", "max_results": 0},
        {"query": "x", "max_results": 11},
        {"query": "x" * 2001},
        {"query": "   "},
    ):
        with pytest.raises((TypeError, ValueError)):
            worker.parse_research_job(claimed_job(payload))
    with pytest.raises(ValueError, match="unsupported worker skill"):
        worker.parse_research_job({**claimed_job(), "required_skill": "research.fetch_url"})


def test_adapter_uses_only_configured_endpoint_and_marks_results_untrusted() -> None:
    worker = load_worker()
    calls: list[tuple[str, str, dict[str, Any], float]] = []

    def transport(
        endpoint: str,
        token: str,
        body: dict[str, Any],
        timeout_seconds: float,
    ) -> Any:
        calls.append((endpoint, token, copy.deepcopy(body), timeout_seconds))
        return {
            "results": [
                {
                    "title": "Primary source",
                    "url": "https://source.example/report",
                    "snippet": "Treat this adapter content as untrusted.",
                }
            ]
        }

    adapter = worker.ResearchAdapterClient(
        "https://research-adapter.example/v1/query",
        "adapter-secret",
        timeout_seconds=9,
        transport=transport,
    )

    result = adapter.query(worker.ResearchQuery("bounded question", 2))

    assert calls == [
        (
            "https://research-adapter.example/v1/query",
            "adapter-secret",
            {"query": "bounded question", "max_results": 2},
            9,
        )
    ]
    assert result == {
        "content_trust": "untrusted",
        "results": [
            {
                "title": "Primary source",
                "url": "https://source.example/report",
                "snippet": "Treat this adapter content as untrusted.",
            }
        ],
    }
    assert "adapter-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://research-adapter.example/v1/query",
        "https://user:pass@research-adapter.example/v1/query",
        "https://research-adapter.example/v1/query#fragment",
        "file:///tmp/adapter",
        "not-a-url",
    ],
)
def test_adapter_endpoint_requires_fixed_credential_free_https(endpoint: str) -> None:
    worker = load_worker()
    with pytest.raises(ValueError, match="research adapter endpoint"):
        worker.ResearchAdapterClient(endpoint, "adapter-secret")


def test_adapter_response_contract_rejects_oversized_or_ambiguous_data() -> None:
    worker = load_worker()
    valid = {"results": [{"title": "Title", "url": "https://source.example", "snippet": "Summary"}]}
    assert worker.normalize_adapter_response(valid, 1)["content_trust"] == "untrusted"
    for response in (
        {**valid, "debug": "internal metadata"},
        {"results": [{**valid["results"][0], "token": "leak"}]},
        {"results": [valid["results"][0], valid["results"][0]]},
        {"results": [{**valid["results"][0], "url": "file:///etc/passwd"}]},
        {"results": [{**valid["results"][0], "snippet": "x" * 4001}]},
    ):
        with pytest.raises((TypeError, ValueError)):
            worker.normalize_adapter_response(response, 1)


def test_run_once_renews_lease_and_submits_only_bounded_untrusted_result() -> None:
    worker = load_worker()
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = []
    call_lock = threading.Lock()
    renewed_twice = threading.Event()
    heartbeat_count = 0
    job = claimed_job()

    def control_request(
        base_url: str,
        path: str,
        token: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal heartbeat_count
        with call_lock:
            calls.append((base_url, path, token, copy.deepcopy(body)))
        if path.endswith("/claim"):
            return job
        if path.endswith("/jobs/job_research_123/heartbeat"):
            heartbeat_count += 1
            if heartbeat_count >= 2:
                renewed_twice.set()
            return {"id": job["id"], "status": "running"}
        if path.endswith("/jobs/job_research_123/result"):
            return {"id": job["id"], "status": "completed"}
        return {"status": "ok"}

    class BlockingAdapter:
        def query(self, request: Any) -> dict[str, Any]:
            assert renewed_twice.wait(timeout=1)
            assert request.query == "lease-safe research"
            return {
                "content_trust": "untrusted",
                "results": [
                    {
                        "title": "Safe projection",
                        "url": "https://source.example/item",
                        "snippet": "Untrusted evidence",
                    }
                ],
            }

    worker.control_plane_request = control_request

    assert worker.run_once(
        "https://control.example",
        "agt_research",
        "agent-secret",
        BlockingAdapter(),
        heartbeat_interval_seconds=0.01,
    )

    proof = {
        "claim_token": job["claim_token"],
        "lease_id": job["lease_id"],
        "lease_generation": job["lease_generation"],
    }
    heartbeats = [call for call in calls if call[1].endswith("/heartbeat") and "/jobs/" in call[1]]
    assert len(heartbeats) >= 2
    assert all(call[3] == proof for call in heartbeats)
    result_call = next(call for call in calls if call[1].endswith("/result"))
    assert result_call[3] == {
        **proof,
        "status": "completed",
        "result": {
            "content_trust": "untrusted",
            "results": [
                {
                    "title": "Safe projection",
                    "url": "https://source.example/item",
                    "snippet": "Untrusted evidence",
                }
            ],
        },
    }
    assert "agent-secret" not in json.dumps(result_call[3])


def test_run_once_discards_result_after_lease_loss() -> None:
    worker = load_worker()
    paths: list[str] = []
    lease_lost = threading.Event()
    heartbeat_count = 0

    def control_request(
        base_url: str,
        path: str,
        token: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal heartbeat_count
        paths.append(path)
        if path.endswith("/claim"):
            return claimed_job()
        if "/jobs/" in path and path.endswith("/heartbeat"):
            heartbeat_count += 1
            if heartbeat_count > 1:
                lease_lost.set()
                raise worker.LeaseLost("stale lease")
        return {"status": "ok"}

    class BlockingAdapter:
        def query(self, request: Any) -> dict[str, Any]:
            assert lease_lost.wait(timeout=1)
            return {"content_trust": "untrusted", "results": []}

    worker.control_plane_request = control_request

    assert worker.run_once(
        "https://control.example",
        "agt_research",
        "agent-secret",
        BlockingAdapter(),
        heartbeat_interval_seconds=0.01,
    )
    assert not any(path.endswith("/result") for path in paths)


def test_agent_card_contains_only_static_safe_metadata() -> None:
    manifest = json.loads(Path(__file__).with_name("agent-card.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == "1"
    assert manifest["name"] == "mongars-research-worker"
    assert [skill["id"] for skill in manifest["skills"]] == ["research.query"]
    encoded = json.dumps(manifest).casefold()
    for forbidden in (
        "credential",
        "token",
        "secret",
        "hostname",
        "/users/",
        "http://",
        "https://",
    ):
        assert forbidden not in encoded
