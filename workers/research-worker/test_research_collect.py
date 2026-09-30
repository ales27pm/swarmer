from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

import pytest
import research_collect as collect_module
import research_worker as worker
from research_collect import (
    CollectionError,
    RawPage,
    collect,
    extract_text,
    public_page_url,
    read_page,
)


def result(url: str, title: str = "Documentation") -> dict[str, str]:
    return {"title": title, "url": url, "snippet": "Search excerpt, not a page read."}


def page(text: str = "Public documentation for a small application.") -> RawPage:
    return RawPage(
        200, {"content-type": "text/html; charset=utf-8"}, f"<main><p>{text}</p></main>".encode()
    )


def no_cancel() -> None:
    pass


def test_explicit_pages_survive_search_outage_with_honest_coverage() -> None:
    urls = ["https://docs.python.org/3/library/json.html", "https://sqlite.org/whentouse.html"]
    fetched: list[str] = []

    def unavailable(query: str, count: int) -> dict[str, Any]:
        raise CollectionError("search_unavailable")

    value = collect(
        {
            "focus": "persistence",
            "queries": ["JSON", "SQLite"],
            "source_urls": urls,
            "required_domains": ["docs.python.org", "sqlite.org"],
        },
        unavailable,
        lambda u, d: fetched.append(u) or page(),
        check_active=no_cancel,
        deadline=time.monotonic() + 10,
    )
    assert fetched == urls
    assert value["schema_version"] == "1.1"
    assert value["collection_status"] == "partial"
    assert value["coverage"]["missing_domains"] == []
    assert value["coverage"]["queries_without_read_pages"] == [0, 1]
    assert all(item["query_indices"] == [] for item in value["results"])
    assert all(item["snippet"] == "" for item in value["results"])


def test_read_coverage_does_not_count_search_snippets_or_deceptive_domains() -> None:
    value = collect(
        {
            "focus": "SQLite persistence",
            "queries": ["SQLite"],
            "required_domains": ["sqlite.org"],
            "max_pages": 1,
        },
        lambda q, n: {
            "content_trust": "untrusted",
            "results": [result("https://sqlite.org.evil.example/doc")],
        },
        lambda u, d: page(),
        check_active=no_cancel,
        deadline=time.monotonic() + 10,
    )
    assert value["coverage"]["missing_domains"] == ["sqlite.org"]


def test_explicit_and_searched_page_share_one_read_with_budget_gap_visible() -> None:
    direct = "https://docs.python.org/3/library/json.html"
    unread = "https://sqlite.org/whentouse.html"
    fetched: list[str] = []
    value = collect(
        {
            "focus": "persistence",
            "queries": ["JSON", "SQLite"],
            "source_urls": [direct],
            "required_domains": ["docs.python.org", "sqlite.org"],
            "max_pages": 1,
        },
        lambda q, n: {
            "content_trust": "untrusted",
            "results": [result(direct if q == "JSON" else unread)],
        },
        lambda u, d: fetched.append(u) or page(),
        check_active=no_cancel,
        deadline=time.monotonic() + 10,
    )
    assert fetched == [direct]
    assert value["results"][0]["query_indices"] == [0]
    assert value["results"][0]["snippet"] == "Search excerpt, not a page read."
    assert value["collection_status"] == "complete"
    assert value["coverage"]["missing_domains"] == ["sqlite.org"]
    assert value["coverage"]["queries_without_read_pages"] == [1]


def test_relevant_passage_beyond_handoff_prefix_survives_with_neighbor_context() -> None:
    body = (
        "<main>"
        + "<p>General unrelated introduction.</p>" * 220
        + "<p>Transaction concurrency permits one writer.</p>"
        + "<p>Readers may continue, except during certain locks.</p></main>"
    ).encode()
    text, truncated = extract_text(body, "text/html", "transaction concurrency")
    assert len(text) <= 4000 and truncated
    assert "Transaction concurrency permits one writer." in text
    assert "except during certain locks" in text


def test_collect_interleaves_queries_and_preserves_both_provenances() -> None:
    queries: list[str] = []
    fetched: list[str] = []

    def search(query: str, count: int) -> dict[str, Any]:
        queries.append(query)
        return {
            "content_trust": "untrusted",
            "results": [result(f"https://docs.example/{query}/{i}") for i in range(count)],
        }

    def fetch(url: str, deadline: float) -> RawPage:
        assert deadline - time.monotonic() <= 15
        fetched.append(url)
        return page()

    value = collect(
        {
            "focus": "persistence and updates",
            "queries": ["storage", "modifications"],
            "max_pages": 2,
        },
        search,
        fetch,
        check_active=no_cancel,
        deadline=time.monotonic() + 120,
    )
    assert queries == ["storage", "modifications"]
    assert fetched == ["https://docs.example/storage/0", "https://docs.example/modifications/0"]
    assert len(value["results"]) == 6 and len(value["pages"]) == 2
    assert value["collection_status"] == "complete"
    assert value["results"][0]["snippet"] == "Search excerpt, not a page read."
    evidence = value["pages"][0]
    assert evidence["text"] == "Public documentation for a small application."
    assert hashlib.sha256(evidence["text"].encode()).hexdigest() == evidence["content_sha256"]
    assert hashlib.sha256(page().body).hexdigest() == evidence["body_sha256"]
    assert evidence["fetched_at"].endswith("+00:00")


def test_shared_result_deduplicates_network_reads_but_preserves_queries() -> None:
    calls: list[str] = []

    def fetch(url: str, deadline: float) -> RawPage:
        calls.append(url)
        return page()

    value = collect(
        {"focus": "shared", "queries": ["one", "two"]},
        lambda q, n: {
            "content_trust": "untrusted",
            "results": [result("https://example.org/shared")],
        },
        fetch,
        check_active=no_cancel,
        deadline=time.monotonic() + 10,
    )
    assert calls == ["https://example.org/shared"]
    assert value["results"][0]["query_indices"] == [0, 1]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/docs",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://169.254.169.254/",
        "https://[::ffff:127.0.0.1]/",
        "https://user:pass@example.org/",
        "https://example.org:8443/",
        "https://example.org\\@127.0.0.1/",
        "https://example.local/",
        "https://localhost/",
        "https://1.2.3/",
        "https://internal/",
        "https://exa%6dple.org/",
        "https://example.org/\nheader",
    ],
)
def test_page_urls_fail_before_network(url: str) -> None:
    calls: list[str] = []
    receipt = read_page(
        url, "focus", lambda u, d: calls.append(u) or page(), time.monotonic() + 5, no_cancel
    )
    assert receipt["status"] == "failed" and calls == []
    assert "text" not in receipt


def test_redirect_revalidated_and_never_fetches_private_target() -> None:
    calls: list[str] = []

    def fetch(url: str, deadline: float) -> RawPage:
        calls.append(url)
        return RawPage(302, {"location": "https://169.254.169.254/latest/"}, b"")

    receipt = read_page("https://example.org/docs", "focus", fetch, time.monotonic() + 5, no_cancel)
    assert receipt["status"] == "failed"
    assert calls == ["https://example.org/docs"]


def test_final_url_follows_bounded_public_redirect_with_no_fragment() -> None:
    calls: list[str] = []

    def fetch(url: str, deadline: float) -> RawPage:
        calls.append(url)
        if len(calls) == 1:
            return RawPage(301, {"location": "/v2/doc?q=one#section"}, b"")
        return page()

    value = read_page("https://example.org/old", "focus", fetch, time.monotonic() + 5, no_cancel)
    assert value["requested_url"] == "https://example.org/old"
    assert value["final_url"] == "https://example.org/v2/doc?q=one"
    assert len(calls) == 2


def test_redirect_loop_stops_without_retry() -> None:
    count = 0

    def fetch(url: str, deadline: float) -> RawPage:
        nonlocal count
        count += 1
        return RawPage(302, {"location": url}, b"")

    value = read_page("https://example.org/docs", "focus", fetch, time.monotonic() + 5, no_cancel)
    assert value["error_code"] == "redirect_loop" and count == 1


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (RawPage(200, {"content-type": "application/pdf"}, b"%PDF"), "unsupported_content_type"),
        (
            RawPage(200, {"content-type": "text/html", "content-encoding": "gzip"}, b"abc"),
            "unsupported_encoding",
        ),
        (RawPage(200, {"content-type": "text/plain"}, b"a" * 1_048_577), "response_byte_limit"),
        (RawPage(200, {"content-type": "text/html"}, b"<script>hidden</script>"), "empty_text"),
        (RawPage(200, {"content-type": "text/plain"}, b"\xff"), "invalid_text_encoding"),
        (RawPage(401, {"content-type": "text/html"}, b"secret login"), "http_status"),
    ],
)
def test_unsupported_or_incomplete_pages_never_become_read(response: RawPage, reason: str) -> None:
    value = read_page(
        "https://example.org/docs", "focus", lambda u, d: response, time.monotonic() + 5, no_cancel
    )
    assert value["status"] == "failed" and value["error_code"] == reason
    assert "text" not in value and "body_sha256" not in value


def test_html_extracts_visible_prose_without_scripts_styles_and_hidden_elements() -> None:
    raw = b'<head><title>title</title></head><nav>navigation</nav><script>fetch("evil")</script><main><p>Visible &amp; useful.</p><div hidden>private</div><div style="display: none">invisible</div><p>Next paragraph.</p></main>'
    text, truncated = extract_text(raw, "text/html", "useful")
    assert text == "Visible & useful.\nNext paragraph." and not truncated


def test_focus_selects_real_relevant_paragraphs_within_bound() -> None:
    unrelated = [f"<p>Boilerplate line {i} " + "text " * 30 + "</p>" for i in range(200)]
    raw = ("".join(unrelated) + "<p>Transactions guarantee persistence for updates.</p>").encode()
    text, truncated = extract_text(raw, "text/html", "Transactions persistence updates")
    assert truncated and len(text) <= 12_000
    assert "Transactions guarantee persistence for updates." in text
    assert "Invented conclusion" not in text


def test_collection_reports_partial_search_and_page_failures() -> None:
    def query(q: str, n: int) -> dict[str, Any]:
        if q == "broken":
            raise CollectionError("search_unavailable")
        return {"content_trust": "untrusted", "results": [result("https://example.org/docs")]}

    value = collect(
        {"focus": "focus", "queries": ["broken", "works"]},
        query,
        lambda u, d: RawPage(404, {}, b"not found"),
        check_active=no_cancel,
        deadline=time.monotonic() + 5,
    )
    assert value["collection_status"] == "partial"
    assert value["searches"][0]["error_code"] == "search_unavailable"
    assert value["pages"][0]["error_code"] == "http_status"


def test_empty_search_is_no_evidence_not_deliverable_success() -> None:
    value = collect(
        {"focus": "focus", "queries": ["none"]},
        lambda q, n: {"content_trust": "untrusted", "results": []},
        lambda u, d: pytest.fail("No URL may be invented"),
        check_active=no_cancel,
        deadline=time.monotonic() + 5,
    )
    assert value["collection_status"] == "no_evidence" and value["pages"] == []


def test_cancel_after_search_prevents_all_further_queries_and_reads() -> None:
    cancelled = False
    calls: list[str] = []

    def check() -> None:
        if cancelled:
            raise worker.LeaseLost("cancelled")

    def query(q: str, n: int) -> dict[str, Any]:
        nonlocal cancelled
        calls.append(q)
        cancelled = True
        return {"content_trust": "untrusted", "results": [result("https://example.org/docs")]}

    with pytest.raises(worker.LeaseLost):
        collect(
            {"focus": "focus", "queries": ["one", "two"]},
            query,
            lambda u, d: pytest.fail("cancelled read"),
            check_active=check,
            deadline=time.monotonic() + 5,
        )
    assert calls == ["one"]


def test_deadline_prevents_network_without_reporting_full_success() -> None:
    value = collect(
        {"focus": "focus", "queries": ["one"]},
        lambda q, n: pytest.fail("expired search"),
        lambda u, d: pytest.fail("expired read"),
        check_active=no_cancel,
        deadline=time.monotonic() - 1,
    )
    assert value["collection_status"] == "no_evidence"
    assert value["searches"][0]["error_code"] == "deadline_exceeded"


def test_inflight_transport_aborted_when_lease_lost() -> None:
    cancelled = threading.Event()
    release = threading.Event()

    class Connection:
        sock = None
        closed = False

        def close(self) -> None:
            self.closed = True
            release.set()

    connection = Connection()

    def check() -> None:
        if cancelled.is_set():
            raise worker.LeaseLost("cancelled")

    def operation() -> None:
        cancelled.set()
        release.wait(2)

    token = worker._ACTIVE_CHECK.set(check)
    try:
        started = time.monotonic()
        with pytest.raises(worker.LeaseLost):
            worker._connection_operation(operation, connection, time.monotonic() + 5)
        assert time.monotonic() - started < 1 and connection.closed
    finally:
        worker._ACTIVE_CHECK.reset(token)
        release.set()


def test_collect_payload_contract_is_bounded_and_strict() -> None:
    for payload in [
        {"focus": "a", "queries": ["same", " SAME "]},
        {"focus": "a", "queries": ["x"], "max_pages": True},
        {"focus": "a", "queries": ["x"], "urls": ["https://evil.org/"]},
        {"focus": "a", "queries": [str(i) for i in range(5)]},
    ]:
        with pytest.raises(ValueError):
            collect_module.parse_collect_payload(payload)
    assert public_page_url("https://EXAMPLE.org:443/docs#section") == "https://example.org/docs"


def test_page_network_reuses_full_dns_validation_before_opening_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    original = worker._resolve_global_addresses

    def resolve(host: str, port: int, deadline: float) -> Any:
        return original(
            host,
            port,
            deadline,
            resolver=lambda *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
            ],
        )

    monkeypatch.setattr(worker, "_resolve_global_addresses", resolve)
    monkeypatch.setattr(
        worker, "_PinnedHTTPSConnection", lambda *a: pytest.fail("unsafe DNS must not connect")
    )
    with pytest.raises(CollectionError, match="network_rejected"):
        worker.public_page_request("https://example.org/docs", time.monotonic() + 1)


def test_page_transport_has_no_auth_cookies_proxy_and_bounds_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    class Sock:
        def settimeout(self, timeout: float) -> None:
            assert 0 < timeout <= 2

    class Connection:
        sock = Sock()

        def __init__(self, host: str, port: int, address: Any, deadline: float) -> None:
            calls.append((host, port, address))

        def connect(self) -> None:
            pass

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            calls.append((method, path, headers))

        def close(self) -> None:
            calls.append(("closed",))

    class Response:
        status = 200
        read = False

        def begin(self) -> None:
            pass

        def getheaders(self) -> list[tuple[str, str]]:
            return [("Content-Type", "text/plain"), ("Content-Length", "4")]

        def read1(self, maximum: int) -> bytes:
            assert maximum <= 16384
            if self.read:
                return b""
            self.read = True
            return b"read"

        def close(self) -> None:
            pass

    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    monkeypatch.setattr(worker, "_resolve_global_addresses", lambda *a: ["pinned-address"])
    monkeypatch.setattr(worker, "_PinnedHTTPSConnection", Connection)
    monkeypatch.setattr(worker.http.client, "HTTPResponse", lambda *a, **k: Response())
    value = worker.public_page_request("https://example.org/docs?q=test", time.monotonic() + 2)
    assert value.body == b"read"
    assert calls[0] == ("example.org", 443, "pinned-address")
    assert calls[1][0:2] == ("GET", "/docs?q=test")
    assert set(calls[1][2]) == {"User-Agent", "Accept", "Accept-Encoding", "Connection"}
    assert calls[-1] == ("closed",)


def test_extract_can_be_cancelled_while_parsing_bounded_html() -> None:
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1
        if calls > 2:
            raise worker.LeaseLost("cancelled")

    with pytest.raises(worker.LeaseLost):
        extract_text(
            b"<p>Some visible text.</p>" * 5000, "text/html", "visible", check_active=check
        )
    assert calls == 3


def test_cancellation_closes_pending_tls_handshake_and_never_adopts_late_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Sock:
        closed = False

        def settimeout(self, timeout: float) -> None:
            pass

        def connect(self, address: Any) -> None:
            pass

        def getpeername(self) -> tuple[str, int]:
            return ("1.1.1.1", 443)

        def shutdown(self, how: int) -> None:
            release.set()

        def close(self) -> None:
            self.closed = True
            release.set()

        def do_handshake(self) -> None:
            started.set()
            try:
                assert release.wait(2)
                if self.closed:
                    raise OSError("closed during handshake")
            finally:
                finished.set()

    raw, tls = Sock(), Sock()

    class Context:
        def wrap_socket(
            self, sock: Any, *, server_hostname: str, do_handshake_on_connect: bool
        ) -> Sock:
            assert sock is raw and server_hostname == "example.org"
            assert do_handshake_on_connect is False
            return tls

    address = worker.ResolvedAddress(
        socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("1.1.1.1", 443)
    )
    connection = worker._PinnedHTTPSConnection("example.org", 443, address, time.monotonic() + 3)
    connection._ssl_context = Context()  # type: ignore[assignment]
    monkeypatch.setattr(worker.socket, "socket", lambda *a: raw)

    def check() -> None:
        if started.is_set():
            raise worker.LeaseLost("cancelled")

    token = worker._ACTIVE_CHECK.set(check)
    try:
        with pytest.raises(worker.LeaseLost):
            worker._connection_operation(connection.connect, connection, time.monotonic() + 3)
        assert finished.wait(1)
        assert tls.closed and connection.sock is None
    finally:
        release.set()
        connection.close()
        worker._ACTIVE_CHECK.reset(token)


@pytest.mark.parametrize("field", ["focus", "queries"])
def test_payload_rejects_unpaired_unicode(field: str) -> None:
    payload: dict[str, Any] = {"focus": "focus", "queries": ["query"]}
    payload[field] = "\ud800" if field == "focus" else ["\ud800"]
    with pytest.raises(ValueError):
        collect_module.parse_collect_payload(payload)


def test_late_relevant_text_survives_downstream_first_4000_character_projection() -> None:
    raw = (
        "Boilerplate navigation " * 2000 + "Transactions guarantee persistence for updates."
    ).encode()
    text, truncated = extract_text(raw, "text/plain", "Transactions persistence updates")
    assert truncated
    assert "Transactions guarantee persistence for updates." in text[:4000]
