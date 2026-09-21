from __future__ import annotations

import gzip
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

import pytest
from test_research_worker import claimed_job, load_worker


@contextmanager
def search_server(
    respond: Callable[[BaseHTTPRequestHandler], None], *, ipv6: bool = False
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(int(self.headers["Content-Length"])),
                }
            )
            try:
                respond(self)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.close_connection = True

        def log_message(self, _format: str, *args: Any) -> None:
            pass

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ipv6 else socket.AF_INET
        daemon_threads = True

    try:
        server = Server(("::1" if ipv6 else "127.0.0.1", 0), Handler)
    except OSError:
        if ipv6:
            pytest.skip("IPv6 loopback is unavailable")
        raise
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    host = "[::1]" if ipv6 else "127.0.0.1"
    try:
        yield f"http://{host}:{server.server_port}/search", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def json_response(value: Any, *, close: bool = False) -> Callable[[BaseHTTPRequestHandler], None]:
    def respond(handler: BaseHTTPRequestHandler) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        if close:
            handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)

    return respond


@pytest.mark.parametrize("ipv6", [False, True])
@pytest.mark.parametrize("close", [False, True])
def test_searxng_real_form_post_is_numeric_loopback_without_bearer_proxy_or_dns(
    monkeypatch: pytest.MonkeyPatch, ipv6: bool, close: bool
) -> None:
    worker = load_worker()
    response = {
        "query": "ignored metadata",
        "results": [{"title": "Source", "url": "https://example.org/a", "content": "Résumé"}],
    }
    with search_server(json_response(response, close=close), ipv6=ipv6) as (
        endpoint,
        requests,
    ):
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
        ):
            monkeypatch.setenv(name, "http://127.0.0.1:1")
        monkeypatch.setenv("MONGARS_RESEARCH_ADAPTER_TOKEN", "must-not-be-forwarded")
        monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("unexpected DNS"))
        client = worker.SearXNGClient(endpoint, timeout_seconds=1)
        result = worker.execute(
            client, claimed_job({"query": "échéances & café", "max_results": 1})
        )
    assert result == {
        "content_trust": "untrusted",
        "results": [{"title": "Source", "url": "https://example.org/a", "snippet": "Résumé"}],
    }
    assert len(requests) == 1 and requests[0]["path"] == "/search"
    assert parse_qs(requests[0]["body"].decode()) == {
        "q": ["échéances & café"],
        "format": ["json"],
    }
    headers = {key.lower(): value for key, value in requests[0]["headers"].items()}
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    assert "authorization" not in headers and "proxy-authorization" not in headers
    assert "must-not-be-forwarded" not in str(requests)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8080/search",
        "https://127.0.0.1/search",
        "http://127.0.0.2/search",
        "http://10.0.0.1/search",
        "http://8.8.8.8/search",
        "http://[::ffff:127.0.0.1]/search",
        "http://[0:0:0:0:0:0:0:1]/search",
        "http://127.1/search",
        "http://2130706433/search",
        "http://user:pass@127.0.0.1/search",
        "http://127.0.0.1/search?",
        "http://127.0.0.1/search#",
        "http://127.0.0.1/search?q=extra",
        "http://127.0.0.1/",
        "http://127.0.0.1/search/",
        "http://127.0.0.1/%73earch",
        "http://127.0.0.1:0/search",
        "http://127.0.0.1:65536/search",
        "http://127.0.0.1:/search",
        " http://127.0.0.1/search",
        "http://127.0.0.1/search\n",
        "http://127.0.0.1\\@example.org/search",
    ],
)
def test_searxng_endpoint_rejects_every_non_exact_local_destination(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError):
        load_worker().SearXNGClient(endpoint)


def test_searxng_filters_truncates_and_deduplicates_without_fetching_results() -> None:
    worker = load_worker()
    results = [
        None,
        {
            "title": "bad credentials",
            "url": "https://user:pass@example.org/a",
            "content": "x",
        },
        {"title": "bad scheme", "url": "file:///etc/passwd", "content": "x"},
        {"title": "bad control", "url": "https://example.org/\nfoo", "content": "x"},
        {"title": "bad port", "url": "https://example.org:99999/a", "content": "x"},
        {"title": 3, "url": "https://example.org/a", "content": "x"},
        {"title": " ", "url": "https://example.org/a", "content": "x"},
        {
            "title": "First" * 100,
            "url": "https://EXAMPLE.org:443/a#one",
            "content": "é" * 5000,
        },
        {
            "title": "Duplicate",
            "url": "https://example.org/a#two",
            "content": "duplicate",
        },
        {
            "title": "Second\x00 title",
            "url": "https://example.org/b",
            "content": " hello\nworld ",
        },
        {"title": "Third", "url": "https://example.org/c"},
    ]
    with search_server(json_response({"results": results})) as (endpoint, _):
        result = worker.SearXNGClient(endpoint).query(worker.ResearchQuery("query", 2))
    assert len(result["results"]) == 2
    first, second = result["results"]
    assert len(first["title"]) == 300 and len(first["snippet"]) == 4000
    assert second == {
        "title": "Second title",
        "url": "https://example.org/b",
        "snippet": "hello world",
    }
    assert result["content_trust"] == "untrusted"


@pytest.mark.parametrize("status", [302, 307, 403, 500])
def test_searxng_real_http_errors_and_redirects_fail_without_followup(
    status: int,
) -> None:
    worker = load_worker()

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        handler.send_header("Location", "/redirect-target")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    with (
        search_server(respond) as (endpoint, requests),
        pytest.raises(worker.ResearchAdapterError),
    ):
        worker.SearXNGClient(endpoint).query(worker.ResearchQuery("query", 1))
    assert len(requests) == 1


@pytest.mark.parametrize("body", [b"not JSON", b"{}", b'{"results":{}}'])
def test_searxng_real_malformed_json_or_shape_fails(body: bytes) -> None:
    worker = load_worker()

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with (
        search_server(respond) as (endpoint, _),
        pytest.raises(worker.ResearchAdapterError),
    ):
        worker.SearXNGClient(endpoint).query(worker.ResearchQuery("query", 1))


@pytest.mark.parametrize("compressed", [False, True])
def test_searxng_real_body_and_decompression_size_are_bounded(compressed: bool) -> None:
    worker = load_worker()
    data = b" " * (worker.MAX_ADAPTER_RESPONSE_BYTES + 1)
    body = gzip.compress(data) if compressed else data

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        if compressed:
            handler.send_header("Content-Encoding", "gzip")
        handler.end_headers()
        handler.wfile.write(body)

    with (
        search_server(respond) as (endpoint, _),
        pytest.raises(worker.ResearchAdapterError, match="size limit"),
    ):
        worker.SearXNGClient(endpoint).query(worker.ResearchQuery("query", 1))


@pytest.mark.parametrize("stage", ["headers", "body", "chunk_header"])
def test_searxng_real_dripping_transport_cannot_extend_absolute_deadline(
    stage: str,
) -> None:
    worker = load_worker()
    stopped = threading.Event()

    def respond(handler: BaseHTTPRequestHandler) -> None:
        if stage == "headers":
            handler.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        else:
            handler.send_response(200)
            if stage == "body":
                handler.send_header("Content-Length", "10000")
            else:
                handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
        while not stopped.wait(0.01):
            handler.wfile.write(b" " if stage != "chunk_header" else b"f")
            handler.wfile.flush()

    try:
        with search_server(respond) as (endpoint, _):
            started = time.monotonic()
            with pytest.raises(worker.ResearchAdapterError):
                worker.SearXNGClient(endpoint, timeout_seconds=0.12).query(
                    worker.ResearchQuery("q", 1)
                )
            assert time.monotonic() - started < 0.8
    finally:
        stopped.set()


def test_provider_selection_defaults_to_existing_adapter_and_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    monkeypatch.delenv("MONGARS_RESEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("MONGARS_RESEARCH_ADAPTER_URL", "https://example.org/query")
    monkeypatch.setenv("MONGARS_RESEARCH_ADAPTER_TOKEN", "adapter-secret")
    assert isinstance(worker.configured_provider(), worker.ResearchAdapterClient)
    monkeypatch.setenv("MONGARS_RESEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("MONGARS_SEARXNG_URL", "http://127.0.0.1:8080/search")
    monkeypatch.delenv("MONGARS_RESEARCH_ADAPTER_TOKEN")
    assert isinstance(worker.configured_provider(), worker.SearXNGClient)
    monkeypatch.setenv("MONGARS_RESEARCH_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="provider"):
        worker.configured_provider()


def test_searxng_accepts_maximum_unicode_query_and_empty_results() -> None:
    worker = load_worker()
    query = "🦋" * worker.MAX_QUERY_CHARACTERS
    with search_server(json_response({"results": []})) as (endpoint, requests):
        result = worker.execute(worker.SearXNGClient(endpoint), claimed_job({"query": query}))
    assert result == {"content_trust": "untrusted", "results": []}
    assert parse_qs(requests[0]["body"].decode())["q"] == [query]


@pytest.mark.parametrize("field", ["endpoint", "headers"])
def test_searxng_job_cannot_override_transport(field: str) -> None:
    worker = load_worker()
    with (
        search_server(json_response({"results": []})) as (endpoint, requests),
        pytest.raises((TypeError, ValueError)),
    ):
        worker.execute(
            worker.SearXNGClient(endpoint), claimed_job({"query": "q", field: "override"})
        )
    assert requests == []
