from __future__ import annotations

import hashlib
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from app.services.website_dossier import FetchResponse, capture_website, normalize_public_url
from app.services.website_dossier_contracts import CaptureLimits

BASE = "https://company.example"


def test_public_fetcher_reads_connection_close_body_without_closed_socket_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.website_dossier as module

    body = b"User-agent: *\nAllow: /\n"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Only this test injects a local resolver result; production DNS checks stay.
    monkeypatch.setattr(
        module,
        "_resolve",
        lambda *a: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", server.server_address)
        ],
    )
    try:
        result = module.PublicHttpFetcher().get(
            "http://company.example/robots.txt", max_bytes=1024, deadline=time.monotonic() + 3
        )
        assert result.status == 200 and result.body == body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class FixtureFetcher:
    def __init__(self, pages: dict[str, tuple[int, str, bytes]]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
        del deadline
        self.calls.append(url)
        status, content_type, body = self.pages.get(url, (404, "text/plain", b"missing"))
        return FetchResponse(url, status, {"content-type": content_type}, body[: max_bytes + 1])


def fixture_fetcher() -> FixtureFetcher:
    return FixtureFetcher(
        {
            BASE + "/robots.txt": (
                200,
                "text/plain",
                b"User-agent: *\nDisallow: /private\nSitemap: https://company.example/sitemap.xml",
            ),
            BASE + "/sitemap.xml": (
                200,
                "application/xml",
                b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://company.example/services</loc></url></urlset>',
            ),
            BASE + "/": (
                200,
                "text/html; charset=utf-8",
                b'<html lang="fr"><head><title>Atelier Exemple</title><meta name="description" content="Services locaux"><script type="application/ld+json">{"@type":"LocalBusiness","name":"Atelier Exemple"}</script></head><body><h1>Atelier Exemple</h1><p>Ouvert lundi de 9 h a 17 h.</p><a href="/services#prix">Services</a><a href="/private">Interne</a><img src="/logo.png" alt="Logo"><a href="/tarifs.pdf">Tarifs publies</a><form action="/contact" method="post"><input name="email" value="ignore-this"><button>Envoyer</button></form></body></html>',
            ),
            BASE + "/services": (
                200,
                "text/html",
                b'<html><body><h1>Services</h1><p>Consultation : 80 $.</p><a href="/">Accueil</a></body></html>',
            ),
        }
    )


def test_capture_preserves_content_provenance_and_honest_coverage() -> None:
    fetcher = fixture_fetcher()
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert dossier.coverage.extracted == 2
    assert dossier.coverage.blocked == 1
    assert dossier.rendering == "not_available"
    assert dossier.asset_downloads == "not_performed"
    assert dossier.migration_audit == "not_run"
    assert BASE + "/private" not in fetcher.calls
    assert BASE + "/contact" not in fetcher.calls
    assert BASE + "/logo.png" not in fetcher.calls
    assert BASE + "/tarifs.pdf" not in fetcher.calls
    home = next(page for page in dossier.pages if page.final_url == BASE + "/")
    assert home.title == "Atelier Exemple"
    assert home.language == "fr"
    assert home.metadata["description"] == "Services locaux"
    assert home.structured_data == [{"@type": "LocalBusiness", "name": "Atelier Exemple"}]
    assert home.forms[0].method == "post"
    assert home.forms[0].field_names == ["email"]
    assert "ignore-this" not in dossier.model_dump_json()
    assert home.html_sha256 == hashlib.sha256(fetcher.pages[BASE + "/"][2]).hexdigest()
    assert any(item.text == "Consultation : 80 $." for item in dossier.inventory)
    assert any(
        item.kind == "image" and item.original_url == BASE + "/logo.png"
        for item in dossier.inventory
    )
    assert any(
        item.kind == "document" and item.original_url == BASE + "/tarifs.pdf"
        for item in dossier.inventory
    )
    assert all(
        item.disposition == "unassigned" and item.destination_url is None
        for item in dossier.inventory
    )
    assert all(
        item.source_url and item.page_sha256 and item.source_locator for item in dossier.inventory
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/",
        "https://user:password@example.org/",
        "https://example.org:8443/",
        "https://example.org/\\path",
    ],
)
def test_non_public_or_credential_urls_fail_before_transport(url: str) -> None:
    fetcher = FixtureFetcher({})
    with pytest.raises(ValueError):
        capture_website(url, fetcher=fetcher)
    assert fetcher.calls == []


def test_normalization_preserves_query_order_but_removes_fragment() -> None:
    assert (
        normalize_public_url("https://EXAMPLE.org:443/a?x=1&x=2#part")
        == "https://example.org/a?x=1&x=2"
    )


def test_page_limit_retains_discovered_but_unvisited_urls() -> None:
    fetcher = fixture_fetcher()
    dossier = capture_website(BASE + "/", fetcher=fetcher, limits=CaptureLimits(max_pages=1))
    assert len(dossier.pages) == 1
    assert dossier.coverage.status == "partial"
    assert any(
        entry.url == BASE + "/services" and entry.state == "not_visited"
        for entry in dossier.coverage.entries
    )
    assert BASE + "/services" not in fetcher.calls


def test_robots_unavailable_is_reported_without_scanning_pages() -> None:
    fetcher = FixtureFetcher({BASE + "/robots.txt": (503, "text/plain", b"Unavailable")})
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert not dossier.pages
    assert dossier.coverage.status == "partial"
    assert dossier.coverage.entries[0].reason == "robots_unavailable"
    assert fetcher.calls == [BASE + "/robots.txt"]


def test_sitemap_failure_remains_visible_in_partial_coverage() -> None:
    fetcher = fixture_fetcher()
    fetcher.pages[BASE + "/sitemap.xml"] = (503, "text/plain", b"Unavailable")
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert dossier.coverage.status == "partial"
    assert any(
        issue.url == BASE + "/sitemap.xml" and issue.reason == "http_status"
        for issue in dossier.coverage.discovery_issues
    )


def test_redirect_to_robot_blocked_path_is_not_requested() -> None:
    class RedirectFetcher(FixtureFetcher):
        def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
            if url == BASE + "/services":
                self.calls.append(url)
                return FetchResponse(url, 302, {"location": "/private"}, b"")
            return super().get(url, max_bytes=max_bytes, deadline=deadline)

    fetcher = RedirectFetcher(fixture_fetcher().pages)
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert BASE + "/private" not in fetcher.calls
    assert any(
        entry.url == BASE + "/services" and entry.reason == "robots_disallowed"
        for entry in dossier.coverage.entries
    )


def test_unwrapped_body_text_is_also_in_the_migration_inventory() -> None:
    fetcher = FixtureFetcher(
        {
            BASE + "/": (
                200,
                "text/html",
                b"<html><body><h1>Studio</h1><div>Ouvert le mardi.</div><p>Tarif 40 $.</p></body></html>",
            )
        }
    )
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert any(item.text == "Ouvert le mardi." for item in dossier.inventory)


def test_real_http_fixture_produces_dossier_without_form_submission() -> None:
    requests: list[tuple[str, str]] = []
    pages = fixture_fetcher().pages

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(("GET", self.path))
            status, content_type, body = pages.get(
                BASE + self.path, (404, "text/plain", b"missing")
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class LocalHttpFixture:
        def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
            del deadline
            # Test-only transport; public URL policy stays enabled in production.
            response = httpx.get(
                f"http://127.0.0.1:{server.server_port}" + url.removeprefix(BASE), trust_env=False
            )
            return FetchResponse(
                url, response.status_code, dict(response.headers), response.content[: max_bytes + 1]
            )

    try:
        dossier = capture_website(BASE + "/", fetcher=LocalHttpFixture())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert dossier.coverage.extracted == 2
    assert {method for method, _ in requests} == {"GET"}
    assert ("GET", "/private") not in requests
    assert ("GET", "/contact") not in requests


@pytest.mark.parametrize(
    "location", ["https://other.example/", "http://127.0.0.1/", "http://company.example/"]
)
def test_redirect_never_leaves_origin_or_downgrades_tls(location: str) -> None:
    class RedirectFetcher(FixtureFetcher):
        def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
            if url == BASE + "/":
                self.calls.append(url)
                return FetchResponse(url, 302, {"location": location}, b"")
            return super().get(url, max_bytes=max_bytes, deadline=deadline)

    fetcher = RedirectFetcher({})
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert len(dossier.pages) == 0
    assert dossier.coverage.blocked == 1
    assert location not in fetcher.calls


@pytest.mark.parametrize("downgrade", [False, True])
def test_http_seed_allows_https_upgrade_but_blocks_later_http_redirect(downgrade: bool) -> None:
    initial = "http://company.example/"
    secure = BASE + "/secure"
    insecure = "http://company.example/insecure"

    class RedirectFetcher(FixtureFetcher):
        def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
            location = (
                secure if url == initial else insecure if url == secure and downgrade else None
            )
            if location is not None:
                self.calls.append(url)
                return FetchResponse(url, 302, {"location": location}, b"")
            return super().get(url, max_bytes=max_bytes, deadline=deadline)

    fetcher = RedirectFetcher(
        {
            secure: (200, "text/html", b"<p>Contenu HTTPS.</p>"),
            insecure: (200, "text/html", b"<p>Contenu HTTP.</p>"),
        }
    )
    dossier = capture_website(initial, fetcher=fetcher)
    assert secure in fetcher.calls
    assert insecure not in fetcher.calls
    if downgrade:
        assert not dossier.pages
        assert dossier.coverage.entries[0].state == "blocked"
        assert dossier.coverage.entries[0].reason == "tls_downgrade"
    else:
        assert dossier.pages[0].final_url == secure
        assert dossier.coverage.extracted == 1


def test_https_page_does_not_fetch_http_href_after_http_seed_upgrade() -> None:
    initial = "http://company.example/"
    secure = BASE + "/secure"
    insecure = "http://company.example/insecure"

    class RedirectFetcher(FixtureFetcher):
        def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
            if url == initial:
                self.calls.append(url)
                return FetchResponse(url, 302, {"location": secure}, b"")
            return super().get(url, max_bytes=max_bytes, deadline=deadline)

    fetcher = RedirectFetcher(
        {
            secure: (
                200,
                "text/html",
                f'<p>Contenu HTTPS.</p><a href="{insecure}">Ancien lien</a>'.encode(),
            ),
            insecure: (200, "text/html", b"<p>Contenu HTTP.</p>"),
        }
    )
    dossier = capture_website(initial, fetcher=fetcher)
    assert insecure not in fetcher.calls
    assert any(item.original_url == insecure for item in dossier.inventory)
    entry = next(entry for entry in dossier.coverage.entries if entry.url == insecure)
    assert entry.state == "excluded"
    assert entry.reason == "tls_downgrade"
    assert entry.discovered_from == secure
    assert dossier.coverage.status == "partial"


def test_response_and_text_limits_are_explicit() -> None:
    fetcher = FixtureFetcher({BASE + "/": (200, "text/html", b"<p>" + b"x" * 2_000 + b"</p>")})
    too_large = capture_website(
        BASE + "/", fetcher=fetcher, limits=CaptureLimits(max_page_bytes=1_024)
    )
    assert too_large.coverage.entries[0].reason == "response_byte_limit"
    assert not too_large.pages
    truncated = capture_website(
        BASE + "/", fetcher=fetcher, limits=CaptureLimits(max_text_characters=100)
    )
    assert "text" in truncated.pages[0].truncated_fields
    assert truncated.coverage.status == "partial"
    assert len(truncated.pages[0].text) <= 100


def test_discovery_is_bounded_and_robots_sitemap_entity_content_is_not_expanded() -> None:
    fetcher = fixture_fetcher()
    fetcher.pages[BASE + "/sitemap.xml"] = (
        200,
        "application/xml",
        b'<!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]><urlset><url><loc>&x;</loc></url></urlset>',
    )
    dossier = capture_website(
        BASE + "/", fetcher=fetcher, limits=CaptureLimits(max_discovered_urls=2)
    )
    assert dossier.coverage.discovered == 2
    assert dossier.coverage.discovery_limited
    assert dossier.coverage.discovery_issues[0].reason == "unsafe_sitemap"


def test_malformed_links_do_not_discard_source_content() -> None:
    fetcher = FixtureFetcher(
        {
            BASE + "/": (
                200,
                "text/html",
                b'<p>Adresse publique.</p><a href="https://[oops/">Lien invalide</a>',
            )
        }
    )
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert dossier.coverage.extracted == 1
    assert "invalid_links" in dossier.pages[0].truncated_fields
    assert any(item.text == "Adresse publique." for item in dossier.inventory)


def test_network_adapter_rejects_any_private_dns_answer_before_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import time

    from app.services.website_dossier import CaptureError, PublicHttpFetcher

    calls: list[str] = []

    def resolver(*_: Any, **__: Any) -> list[Any]:
        calls.append("dns")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ]

    def no_socket(*_: Any, **__: Any) -> None:
        pytest.fail("Socket must not be opened for a mixed public/private DNS answer")

    monkeypatch.setattr(socket, "socket", no_socket)
    with pytest.raises(CaptureError, match="non_public_address"):
        PublicHttpFetcher(resolver=resolver).get(
            BASE, max_bytes=1_024, deadline=time.monotonic() + 2
        )
    assert calls == ["dns"]


def test_network_adapter_pins_ip_and_uses_only_get_without_cookie_or_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import socket
    import time

    from app.services.website_dossier import PublicHttpFetcher

    connections: list[Any] = []
    methods: list[Any] = []
    dns: list[str] = []

    class FakeSocket:
        def settimeout(self, _: float) -> None:
            pass

        def connect(self, address: Any) -> None:
            connections.append(address)

        def getpeername(self) -> Any:
            return ("8.8.8.8", 80)

        def makefile(self, mode: str) -> io.BytesIO:
            assert mode == "rb"
            return io.BytesIO(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 13\r\nConnection: close\r\n\r\n<p>Source</p>"
            )

        def shutdown(self, _: int) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeConnection:
        sock: Any = None

        def __init__(self, host: str, port: int, **_: Any) -> None:
            assert (host, port) == ("company.example", 80)

        def request(self, method: str, path: str, *, headers: Any) -> None:
            methods.append((method, path, headers))

        def close(self) -> None:
            pass

    def resolver(*_: Any, **__: Any) -> Any:
        dns.append("resolved_once")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80))]

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setattr(socket, "socket", lambda *_: FakeSocket())
    monkeypatch.setattr("http.client.HTTPConnection", FakeConnection)
    result = PublicHttpFetcher(resolver=resolver).get(
        "http://company.example/about?x=1", max_bytes=1_024, deadline=time.monotonic() + 2
    )
    assert result.body == b"<p>Source</p>"
    assert dns == ["resolved_once"]
    assert connections == [("8.8.8.8", 80)]
    assert methods[0][:2] == ("GET", "/about?x=1")
    assert {name.lower() for name in methods[0][2]} == {
        "user-agent",
        "accept",
        "accept-encoding",
        "connection",
    }


def test_migration_inventory_cannot_claim_destination_or_exclusion_without_reason() -> None:
    from pydantic import ValidationError

    from app.services.website_dossier_contracts import SourceInventoryItem

    item = capture_website(BASE + "/", fetcher=fixture_fetcher()).inventory[0].model_dump()
    with pytest.raises(ValidationError):
        SourceInventoryItem.model_validate({**item, "destination_url": "https://new.example/"})
    with pytest.raises(ValidationError):
        SourceInventoryItem.model_validate({**item, "disposition": "excluded", "reason": None})


def test_bytes_consumed_by_failed_transport_still_reduce_capture_budget() -> None:
    from app.services.website_dossier import CaptureError

    class FailingBody(FixtureFetcher):
        def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
            if url == BASE + "/":
                self.calls.append(url)
                raise CaptureError("response_byte_limit", received_bytes=max_bytes + 1)
            return super().get(url, max_bytes=max_bytes, deadline=deadline)

    fetcher = FailingBody(fixture_fetcher().pages)
    dossier = capture_website(
        BASE + "/", fetcher=fetcher, limits=CaptureLimits(max_total_bytes=1_024)
    )
    assert dossier.coverage.response_bytes == 1_025
    assert BASE + "/services" not in fetcher.calls
    assert any(
        entry.url == BASE + "/services" and entry.reason == "total_byte_limit"
        for entry in dossier.coverage.entries
    )


def test_redirect_duplicates_preserve_one_source_inventory_per_final_page() -> None:
    class RedirectFetcher(FixtureFetcher):
        def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
            if url == BASE + "/":
                self.calls.append(url)
                return FetchResponse(url, 302, {"location": "/services"}, b"")
            return super().get(url, max_bytes=max_bytes, deadline=deadline)

    dossier = capture_website(BASE + "/", fetcher=RedirectFetcher(fixture_fetcher().pages))
    assert dossier.coverage.extracted == 1
    assert len(dossier.pages) == 1
    assert len({item.id for item in dossier.inventory}) == len(dossier.inventory)
    assert any(entry.reason == "redirect_duplicate" for entry in dossier.coverage.entries)


def test_html_base_preserves_actual_relative_asset_provenance() -> None:
    fetcher = FixtureFetcher(
        {
            BASE + "/": (
                200,
                "text/html",
                b'<html><head><base href="https://static.example/catalog/"></head><body><p>Notre brochure.</p><img src="logo.png" alt="Logo"><a href="brochure.pdf">Document</a></body></html>',
            )
        }
    )
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert any(
        item.original_url == "https://static.example/catalog/logo.png" for item in dossier.inventory
    )
    assert any(
        item.original_url == "https://static.example/catalog/brochure.pdf"
        for item in dossier.inventory
    )
    assert all(url.startswith(BASE) for url in fetcher.calls)


def test_utf16_sitemap_entity_declaration_is_not_expanded() -> None:
    fetcher = fixture_fetcher()
    xml = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY x "content">]><urlset><loc>&x;</loc></urlset>'
    fetcher.pages[BASE + "/sitemap.xml"] = (200, "application/xml", xml.encode("utf-16"))
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert any(issue.reason == "unsafe_sitemap" for issue in dossier.coverage.discovery_issues)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
@pytest.mark.parametrize(
    "declaration",
    [
        "<!DOCTYPE urlset>",
        '<!DOCTYPE urlset [<!ENTITY first "abc"><!ENTITY expanded "&first;&first;&first;">]>',
        '<!DOCTYPE urlset SYSTEM "https://company.example/external.dtd">',
        '<!DOCTYPE urlset [<!ENTITY local SYSTEM "file:///etc/passwd">]>',
        '<!DOCTYPE urlset [<!ENTITY remote SYSTEM "http://169.254.169.254/latest/meta-data/">]>',
    ],
)
def test_sitemap_parser_refuses_dtd_and_entities_for_all_supported_encodings(
    encoding: str,
    declaration: str,
) -> None:
    fetcher = fixture_fetcher()
    xml_encoding = "UTF-8" if encoding == "utf-8" else "UTF-16"
    payload = (
        f'<?xml version="1.0" encoding="{xml_encoding}"?>{declaration}'
        "<urlset><url><loc>https://company.example/from-unsafe-sitemap</loc></url></urlset>"
    ).encode(encoding)
    fetcher.pages[BASE + "/sitemap.xml"] = (200, "application/xml", payload)
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert any(issue.reason == "unsafe_sitemap" for issue in dossier.coverage.discovery_issues)
    assert dossier.coverage.status == "partial"
    assert not any(
        "from-unsafe-sitemap" in call or "external.dtd" in call for call in fetcher.calls
    )
    assert {call for call in fetcher.calls} == {
        BASE + "/robots.txt",
        BASE + "/sitemap.xml",
        BASE + "/",
        BASE + "/services",
    }


def test_benign_xml_comments_and_predefined_entities_are_not_rejected_as_dtd() -> None:
    fetcher = fixture_fetcher()
    # A marker in a comment is not a declaration; &amp; is a predefined XML entity.
    fetcher.pages[BASE + "/sitemap.xml"] = (
        200,
        "application/xml",
        b"""
        <urlset><!-- Example documentation: <!DOCTYPE and <!ENTITY are prohibited. -->
        <url><loc>https://company.example/from-sitemap?a=1&amp;b=2</loc></url></urlset>
    """,
    )
    fetcher.pages[BASE + "/from-sitemap?a=1&b=2"] = (200, "text/html", b"<p>Safe sitemap page</p>")
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert not dossier.coverage.discovery_issues
    assert any(page.final_url == BASE + "/from-sitemap?a=1&b=2" for page in dossier.pages)


@pytest.mark.parametrize(
    "payload",
    [
        b'<?xml version="1.0" encoding="unknown-encoding"?><urlset/>',
        b"<urlset><url></urlset>",
    ],
)
def test_malformed_or_unsupported_xml_is_a_bounded_discovery_issue(payload: bytes) -> None:
    fetcher = fixture_fetcher()
    fetcher.pages[BASE + "/sitemap.xml"] = (200, "application/xml", payload)
    dossier = capture_website(BASE + "/", fetcher=fetcher)
    assert any(issue.reason == "sitemap_unavailable" for issue in dossier.coverage.discovery_issues)
    assert dossier.coverage.extracted == 2
