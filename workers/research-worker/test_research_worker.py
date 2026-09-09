from __future__ import annotations

import copy
import gzip
import importlib.util
import json
import socket
import threading
import time
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
        "https://research-adapter.example/v1/query?redirect=https://internal.invalid",
        " https://research-adapter.example/v1/query",
        "https://research-adapter.example\\@attacker.invalid/v1/query",
        "https://research-adapter.example:/v1/query",
        "https://research-adapter.example:0/v1/query",
        "https://localhost/v1/query",
        "https://127.0.0.1/v1/query",
        "https://10.0.0.1/v1/query",
        "https://169.254.169.254/v1/query",
        "https://0.0.0.0/v1/query",
        "https://224.0.0.1/v1/query",
        "https://192.0.2.1/v1/query",
        "https://[::1]/v1/query",
        "https://[fc00::1]/v1/query",
        "https://[fe80::1]/v1/query",
        "https://[ff02::1]/v1/query",
        "https://[::]/v1/query",
        "https://[::ffff:127.0.0.1]/v1/query",
        "https://[fe80::1%25en0]/v1/query",
        "https://[2001:db8::1]/v1/query",
        "file:///tmp/adapter",
        "not-a-url",
    ],
)
def test_adapter_endpoint_requires_fixed_credential_free_https(endpoint: str) -> None:
    worker = load_worker()
    with pytest.raises(ValueError, match="research adapter endpoint"):
        worker.ResearchAdapterClient(endpoint, "adapter-secret")


def test_adapter_endpoint_is_canonicalized_before_injected_transport() -> None:
    worker = load_worker()
    calls: list[str] = []

    def transport(endpoint: str, *_: Any) -> dict[str, list[Any]]:
        calls.append(endpoint)
        return {"results": []}

    adapter = worker.ResearchAdapterClient(
        "HTTPS://Research-Adapter.Example:443/v1/query",
        "adapter-secret",
        transport=transport,
    )

    assert adapter.query(worker.ResearchQuery("query", 1))["results"] == []
    assert calls == ["https://research-adapter.example/v1/query"]


def test_adapter_resolution_is_single_pass_and_rejects_any_non_global_answer() -> None:
    worker = load_worker()
    calls = 0

    def mixed_resolver(*_: Any, **__: Any) -> list[tuple[Any, ...]]:
        nonlocal calls
        calls += 1
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            ),
        ]

    with pytest.raises(worker.ResearchAdapterError, match="non-global"):
        worker._resolve_global_addresses(
            "research-adapter.example",
            443,
            time.monotonic() + 1,
            resolver=mixed_resolver,
        )
    assert calls == 1

    def public_resolver(*_: Any, **__: Any) -> list[tuple[Any, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2606:4700:4700::1111", 443, 0, 0),
            ),
        ]

    addresses = worker._resolve_global_addresses(
        "research-adapter.example",
        443,
        time.monotonic() + 1,
        resolver=public_resolver,
    )
    assert [address.sockaddr[0] for address in addresses] == [
        "93.184.216.34",
        "2606:4700:4700::1111",
    ]


def test_adapter_resolution_obeys_absolute_deadline() -> None:
    worker = load_worker()

    def slow_resolver(*_: Any, **__: Any) -> list[Any]:
        time.sleep(0.05)
        return []

    with pytest.raises(worker.ResearchAdapterError, match="deadline"):
        worker._resolve_global_addresses(
            "research-adapter.example",
            443,
            time.monotonic() + 0.005,
            resolver=slow_resolver,
        )


def test_adapter_resolution_timeouts_keep_at_most_one_resolver_in_flight() -> None:
    worker = load_worker()
    baseline_threads = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name == "research-adapter-dns" and thread.is_alive()
    }
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_resolver(*_: Any, **__: Any) -> list[tuple[Any, ...]]:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]

    with pytest.raises(worker.ResearchAdapterError, match="deadline"):
        worker._resolve_global_addresses(
            "research-adapter.example",
            443,
            time.monotonic() + 0.005,
            resolver=blocked_resolver,
        )
    assert started.wait(timeout=1)

    for _ in range(10):
        with pytest.raises(worker.ResearchAdapterError, match="deadline"):
            worker._resolve_global_addresses(
                "research-adapter.example",
                443,
                time.monotonic() + 0.005,
                resolver=blocked_resolver,
            )

    assert calls == 1
    active = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name == "research-adapter-dns" and thread.is_alive()
    }
    assert len(active - baseline_threads) == 1

    release.set()

    addresses = worker._resolve_global_addresses(
        "research-adapter.example",
        443,
        time.monotonic() + 1,
        resolver=lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ],
    )
    assert [address.sockaddr[0] for address in addresses] == ["93.184.216.34"]
    assert calls == 1


def test_default_adapter_transport_pins_address_and_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    address = worker.ResolvedAddress(
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        ("93.184.216.34", 443),
    )
    observed: dict[str, Any] = {}

    class FakeSocket:
        def settimeout(self, value: float) -> None:
            assert value > 0

    class FakeResponse:
        status = 200

        def __init__(self) -> None:
            self._chunks = [b'{"results":[]}', b""]

        def getheader(self, name: str) -> str | None:
            return None

        def read1(self, _: int) -> bytes:
            return self._chunks.pop(0)

    class FakeConnection:
        def __init__(
            self,
            hostname: str,
            port: int,
            resolved: Any,
            deadline: float,
        ) -> None:
            observed["connection"] = (hostname, port, resolved, deadline)
            self.sock = FakeSocket()

        def connect(self) -> None:
            observed["connected"] = True

        def request(self, method: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
            observed["request"] = (method, path, body, headers)

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_: pytest.fail("adapter transport must not consult urllib proxy handlers"),
    )
    monkeypatch.setattr(worker, "_resolve_global_addresses", lambda *_args, **_kwargs: [address])
    monkeypatch.setattr(worker, "_PinnedHTTPSConnection", FakeConnection)

    result = worker.research_adapter_request(
        "https://research-adapter.example/v1/query",
        "adapter-secret",
        {"query": "bounded", "max_results": 1},
        5,
    )

    assert result == {"results": []}
    assert observed["connection"][0:3] == (
        "research-adapter.example",
        443,
        address,
    )
    assert observed["request"][0:2] == ("POST", "/v1/query")
    assert observed["request"][3]["Authorization"] == "Bearer adapter-secret"
    assert observed["connected"] is True
    assert observed["closed"] is True


def test_adapter_response_has_absolute_deadline_and_bounded_decompression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    real_monotonic = time.monotonic

    class FakeSocket:
        def settimeout(self, value: float) -> None:
            assert value > 0

    class FakeConnection:
        sock = FakeSocket()

    class SlowResponse:
        status = 200

        def getheader(self, name: str) -> str | None:
            return None

        def read1(self, _: int) -> bytes:
            return b"{"

    moments = iter((0.0, 0.6, 1.2))
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(moments))
    with pytest.raises(worker.ResearchAdapterError, match="deadline"):
        worker._read_bounded_adapter_json(SlowResponse(), FakeConnection(), 1.0)

    class CompressedResponse:
        status = 200

        def __init__(self, payload: bytes) -> None:
            self._chunks = [payload, b""]

        def getheader(self, name: str) -> str | None:
            return "gzip" if name.casefold() == "content-encoding" else None

        def read1(self, _: int) -> bytes:
            return self._chunks.pop(0)

    monkeypatch.setattr(worker.time, "monotonic", real_monotonic)
    valid = gzip.compress(b'{"results":[]}')
    assert worker._read_bounded_adapter_json(
        CompressedResponse(valid), FakeConnection(), real_monotonic() + 1
    ) == {"results": []}
    bomb = gzip.compress(b"x" * (worker.MAX_ADAPTER_RESPONSE_BYTES + 1))
    with pytest.raises(worker.ResearchAdapterError, match="size limit"):
        worker._read_bounded_adapter_json(
            CompressedResponse(bomb), FakeConnection(), real_monotonic() + 1
        )


def test_adapter_peer_pin_and_redirects_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    address = worker.ResolvedAddress(
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        ("93.184.216.34", 443),
    )

    class WrongPeer:
        def getpeername(self) -> tuple[str, int]:
            return "127.0.0.1", 443

    with pytest.raises(worker.ResearchAdapterError, match="pinned address"):
        worker._verify_pinned_peer(WrongPeer(), address)

    class FakeSocket:
        def settimeout(self, value: float) -> None:
            assert value > 0

    class RedirectResponse:
        status = 302

    class FakeConnection:
        def __init__(self, *_: Any) -> None:
            self.sock = FakeSocket()

        def connect(self) -> None:
            return None

        def request(self, *_: Any, **__: Any) -> None:
            return None

        def getresponse(self) -> RedirectResponse:
            return RedirectResponse()

        def close(self) -> None:
            return None

    monkeypatch.setattr(worker, "_resolve_global_addresses", lambda *_args, **_kwargs: [address])
    monkeypatch.setattr(worker, "_PinnedHTTPSConnection", FakeConnection)
    with pytest.raises(worker.ResearchAdapterError, match="redirects are not allowed"):
        worker.research_adapter_request(
            "https://research-adapter.example/v1/query",
            "adapter-secret",
            {"query": "bounded", "max_results": 1},
            5,
        )


def test_adapter_header_drip_cannot_outlive_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    address = worker.ResolvedAddress(
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        ("93.184.216.34", 443),
    )
    closed = threading.Event()

    class FakeSocket:
        def settimeout(self, value: float) -> None:
            assert value > 0

        def shutdown(self, _: int) -> None:
            closed.set()

    class FakeConnection:
        def __init__(self, *_: Any) -> None:
            self.sock = FakeSocket()

        def connect(self) -> None:
            return None

        def request(self, *_: Any, **__: Any) -> None:
            return None

        def getresponse(self) -> None:
            closed.wait(timeout=1)

        def close(self) -> None:
            closed.set()

    monkeypatch.setattr(worker, "_resolve_global_addresses", lambda *_args, **_kwargs: [address])
    monkeypatch.setattr(worker, "_PinnedHTTPSConnection", FakeConnection)

    started = time.monotonic()
    with pytest.raises(worker.ResearchAdapterError, match="deadline"):
        worker.research_adapter_request(
            "https://research-adapter.example/v1/query",
            "adapter-secret",
            {"query": "bounded", "max_results": 1},
            0.01,
        )
    assert time.monotonic() - started < 0.5
    assert closed.is_set()


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


def test_lease_heartbeat_stop_is_bounded_when_request_does_not_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    blocked = threading.Event()
    release = threading.Event()
    calls = 0

    class BlockingClient:
        def heartbeat_job(self, _job_id: str, _lease: Any) -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                blocked.set()
                release.wait(timeout=1)

    monkeypatch.setattr(worker, "HEARTBEAT_JOIN_TIMEOUT_SECONDS", 0.01)
    lease = worker.LeaseProof("claim", "lease", 1)
    heartbeat = worker.LeaseHeartbeat(BlockingClient(), "job", lease, 0.001)
    heartbeat.start()
    assert blocked.wait(timeout=1)

    started = time.monotonic()
    heartbeat.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert heartbeat._thread is not None and heartbeat._thread.is_alive()
    release.set()
    heartbeat._thread.join(timeout=1)


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
