from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from app.services import website_browser
from app.services.website_assets import (
    AssetLimits,
    AssetReference,
    PublicResourceSession,
    ResourceLimits,
    discover_asset_references,
    download_website_assets,
    store_evidence,
)
from app.services.website_browser import (
    BrowserCaptureLimits,
    BrowserRuntimeConfig,
    browser_runtime_capability,
    capture_rendered_page,
    extract_rendered_inventory,
)
from app.services.website_dossier import CaptureError, FetchResponse

BASE = "https://fixture.example"
DIGEST = "a" * 64


class LocalTransport:
    """Test-only HTTP bridge; production's PublicHttpFetcher is never weakened."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.urls: list[str] = []

    def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
        assert urlsplit(url).hostname == "fixture.example"
        self.urls.append(url)
        parsed = urlsplit(url)
        target = f"http://127.0.0.1:{self.port}" + parsed.path
        if parsed.query:
            target += "?" + parsed.query
        with httpx.stream(
            "GET", target, trust_env=False, timeout=max(0.1, deadline - time.monotonic())
        ) as response:
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    break
            return FetchResponse(
                url, response.status_code, dict(response.headers), body[: max_bytes + 1]
            )


@pytest.fixture
def website_fixture() -> Iterator[
    tuple[LocalTransport, dict[str, tuple[int, dict[str, str], bytes]], list[str]]
]:
    pages: dict[str, tuple[int, dict[str, str], bytes]] = {
        "/robots.txt": (
            200,
            {"content-type": "text/plain"},
            b"User-agent: *\nDisallow: /private\n",
        ),
        "/": (
            200,
            {"content-type": "text/html"},
            b"""<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Source title</title><link rel="stylesheet" href="/style.css">
<h1 id="heading">Before JavaScript</h1><div id="data"></div>
<img src="/small.png" srcset="/small.png 1x, /large.png 2x">
<script src="/app.js"></script>""",
        ),
        "/app.js": (
            200,
            {"content-type": "application/javascript"},
            b"""
document.querySelector('#heading').textContent='Rendered by JavaScript';
document.title='Rendered title';
fetch('/data.json').then(r=>r.json()).then(v=>{document.querySelector('#data').textContent=v.label});
""",
        ),
        "/data.json": (
            200,
            {"content-type": "application/json"},
            b'{"label":"Visible fetched content"}',
        ),
        "/style.css": (
            200,
            {"content-type": "text/css"},
            b"body{background:#fff;color:#000}h1{font-size:40px}@media(max-width:500px){h1{font-size:24px}}",
        ),
        "/document.pdf": (
            200,
            {"content-type": "application/pdf"},
            b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n",
        ),
        "/unsafe.svg": (200, {"content-type": "image/svg+xml"}, b'<svg onload="alert(1)"/>'),
        "/fake.png": (
            200,
            {"content-type": "image/png"},
            b"<html><script>alert(1)</script></html>",
        ),
    }
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            status, headers, body = pages.get(
                self.path, (404, {"content-type": "text/plain"}, b"missing")
            )
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield LocalTransport(server.server_port), pages, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def raster_bytes() -> bytes:
    image = pytest.importorskip("PIL.Image")
    output = io.BytesIO()
    image.new("RGB", (20, 10), color=(20, 80, 180)).save(output, "PNG")
    return output.getvalue()


def reference(path: str, *, kind: str = "image") -> AssetReference:
    return AssetReference(
        url=BASE + path,
        source_url=BASE + "/",
        page_sha256=DIGEST,
        source_locator="img@1:0",
        kind=kind,
    )


def test_discover_srcset_pdf_and_skip_inline_or_private_assets() -> None:
    references, truncated = discover_asset_references(
        '<img src="/one.png" srcset="/small.png 1x, /large.png 2x">'
        '<source srcset="data:image/png;base64,AAABBB 1x, /other.png 2x">'
        '<img src="http://127.0.0.1/private"><a href="/prices.pdf#page=1">Prices</a>'
        '<a href="http://[invalid">Malformed</a>',
        source_url=BASE + "/",
        page_sha256=DIGEST,
    )
    assert not truncated
    assert {item.url for item in references} == {
        BASE + "/one.png",
        BASE + "/small.png",
        BASE + "/large.png",
        BASE + "/other.png",
        BASE + "/prices.pdf",
    }
    assert all(item.source_url == BASE + "/" and item.page_sha256 == DIGEST for item in references)
    assert next(item for item in references if item.kind == "document").url.endswith(".pdf")


def test_real_http_binary_download_has_hash_provenance_and_inert_pdf(
    website_fixture: Any,
    tmp_path: Path,
) -> None:
    transport, pages, requests = website_fixture
    png = raster_bytes()
    pages["/small.png"] = (200, {"content-type": "image/png"}, png)
    result = download_website_assets(
        [reference("/small.png"), reference("/document.pdf", kind="document")],
        source_url=BASE + "/",
        output_dir=tmp_path,
        fetcher=transport,
    )
    assert result.status == "completed"
    image, pdf = result.assets
    assert image.sha256 == hashlib.sha256(png).hexdigest()
    assert Path(image.local_path).read_bytes() == png
    assert image.local_path.endswith(".bin")
    assert image.preview_media_type == "image/png"
    preview = Path(image.preview_local_path).read_bytes()
    assert hashlib.sha256(preview).hexdigest() == image.preview_sha256
    assert image.page_source_url == BASE + "/" and image.page_sha256 == DIGEST
    assert pdf.media_type == "application/pdf" and pdf.preview_local_path is None
    assert requests == ["/robots.txt", "/small.png", "/document.pdf"]


@pytest.mark.parametrize(
    "path, reason",
    [
        ("/unsafe.svg", "unsupported_media_type"),
        ("/fake.png", "invalid_raster_image"),
        ("/private/logo.png", "robots_disallowed"),
    ],
)
def test_rejected_assets_never_create_preview(
    website_fixture: Any, tmp_path: Path, path: str, reason: str
) -> None:
    transport, _, requests = website_fixture
    pytest.importorskip("PIL.Image")
    result = download_website_assets(
        [reference(path)], source_url=BASE, output_dir=tmp_path, fetcher=transport
    )
    assert not result.assets and result.status == "partial"
    assert result.issues[0].reason == reason
    assert not list(tmp_path.iterdir())
    if "/private" in path:
        assert path not in requests


@pytest.mark.parametrize(
    "target,reason",
    [
        ("/private/image.png", "robots_disallowed"),
        ("https://elsewhere.example/image.png", "outside_origin"),
        ("http://fixture.example/image.png", "tls_downgrade"),
        ("http://169.254.169.254/latest", "non_public_address"),
    ],
)
def test_redirect_destinations_are_guarded_before_fetch(
    website_fixture: Any, target: str, reason: str
) -> None:
    transport, pages, requests = website_fixture
    pages["/redirect"] = (302, {"location": target}, b"")
    session = PublicResourceSession(BASE, limits=ResourceLimits(), fetcher=transport)
    with pytest.raises((CaptureError, ValueError), match=reason):
        session.get(BASE + "/redirect")
    assert requests == ["/robots.txt", "/redirect"]


def test_robots_failure_and_request_budget_fail_closed(website_fixture: Any) -> None:
    transport, pages, requests = website_fixture
    pages["/robots.txt"] = (503, {"content-type": "text/plain"}, b"unavailable")
    with pytest.raises(CaptureError, match="robots_unavailable"):
        PublicResourceSession(BASE, limits=ResourceLimits(), fetcher=transport).get(BASE)
    assert requests == ["/robots.txt"]
    pages["/robots.txt"] = (404, {"content-type": "text/plain"}, b"not found")
    with pytest.raises(CaptureError, match="request_limit"):
        PublicResourceSession(BASE, limits=ResourceLimits(max_requests=1), fetcher=transport).get(
            BASE
        )


def test_https_page_cannot_downgrade_asset_even_when_initial_source_was_http(
    website_fixture: Any,
) -> None:
    transport, _, requests = website_fixture
    session = PublicResourceSession(
        "http://fixture.example/", limits=ResourceLimits(), fetcher=transport
    )
    with pytest.raises(CaptureError, match="tls_downgrade"):
        session.get("http://fixture.example/image.png", origin="https://fixture.example/upgraded")
    assert requests == []


def test_binary_byte_and_pixel_budgets(website_fixture: Any, tmp_path: Path) -> None:
    transport, pages, _ = website_fixture
    pages["/large.png"] = (200, {"content-type": "image/png"}, b"a" * 2_000)
    result = download_website_assets(
        [reference("/large.png")],
        source_url=BASE,
        output_dir=tmp_path,
        fetcher=transport,
        limits=AssetLimits(max_resource_bytes=1_024),
    )
    assert result.issues[0].reason == "response_byte_limit"
    pages["/large.png"] = (200, {"content-type": "image/png"}, raster_bytes())
    result = download_website_assets(
        [reference("/large.png")],
        source_url=BASE,
        output_dir=tmp_path,
        fetcher=transport,
        limits=AssetLimits(max_image_pixels=100),
    )
    assert result.issues[0].reason == "image_pixel_limit"


def test_evidence_storage_refuses_symlink_overwrite(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    body = b"evidence"
    (tmp_path / (hashlib.sha256(body).hexdigest() + ".bin")).symlink_to(victim)
    with pytest.raises(CaptureError, match="artifact_path_conflict"):
        store_evidence(tmp_path, body, suffix=".bin")
    assert victim.read_bytes() == b"untouched"


async def test_browser_disabled_or_isolation_missing_never_fetches(
    website_fixture: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _, requests = website_fixture
    disabled = await capture_rendered_page(BASE, output_dir=tmp_path, fetcher=transport)
    assert disabled.status == "unavailable" and disabled.reason == "browser_disabled"
    monkeypatch.setattr(website_browser.platform, "system", lambda: "Darwin")
    unavailable = await capture_rendered_page(
        BASE, output_dir=tmp_path, fetcher=transport, config=BrowserRuntimeConfig(enabled=True)
    )
    assert unavailable.status == "unavailable"
    assert unavailable.reason in {"network_isolation_unavailable", "playwright_not_installed"}
    assert not requests


@pytest.fixture
def real_chromium(monkeypatch: pytest.MonkeyPatch) -> str:
    pytest.importorskip("playwright.async_api")
    executable = os.environ.get("SWARMER_TEST_CHROMIUM")
    if not executable or not Path(executable).is_file():
        pytest.skip(
            "Set SWARMER_TEST_CHROMIUM to an existing test Chromium binary; no installation performed"
        )

    async def fixture_launcher(path: str, directory: Path) -> Path:
        del directory
        assert path == executable
        return Path(path)

    # ONLY the fixture replaces OS setup on developer Macs. Actual Chromium,
    # JavaScript, HTTP transport, routes, screenshots and policy remain real.
    # This does not qualify the production Linux namespace itself.
    monkeypatch.setattr(website_browser, "_prepare_browser_launcher", fixture_launcher)
    return executable


async def test_real_browser_executes_js_fetches_data_and_captures_both_viewports(
    website_fixture: Any,
    tmp_path: Path,
    real_chromium: str,
) -> None:
    transport, pages, _ = website_fixture
    pages["/small.png"] = pages["/large.png"] = (200, {"content-type": "image/png"}, raster_bytes())
    result = await capture_rendered_page(
        BASE,
        output_dir=tmp_path,
        fetcher=transport,
        config=BrowserRuntimeConfig(enabled=True, chromium_executable=real_chromium),
        limits=BrowserCaptureLimits(settle_ms=250, max_seconds=30),
    )
    assert result.status == "captured", result.model_dump()
    assert result.javascript_executed
    assert len(result.viewports) == 2
    for view, expected_size in zip(result.viewports, [(1280, 900), (390, 844)], strict=True):
        assert view.title == "Rendered title"
        assert "Rendered by JavaScript" in view.text and "Visible fetched content" in view.text
        assert "Before JavaScript" not in view.text
        screenshot = view.screenshot
        body = Path(screenshot.local_path).read_bytes()
        assert hashlib.sha256(body).hexdigest() == screenshot.sha256
        image = pytest.importorskip("PIL.Image")
        with image.open(io.BytesIO(body)) as decoded:
            assert decoded.size == expected_size
        assert {item.url for item in view.asset_references} >= {
            BASE + "/small.png",
            BASE + "/large.png",
        }
    inventory = extract_rendered_inventory([result])
    assert any(item.text == "Rendered by JavaScript" for item in inventory)
    assert all(item.source_locator.startswith("rendered_dom:") for item in inventory)
    Path(result.viewports[0].dom_local_path).write_bytes(b"tampered")
    with pytest.raises(CaptureError, match="invalid_dom_evidence"):
        extract_rendered_inventory([result])


async def test_real_browser_blocks_private_network_post_websocket_worker_and_svg(
    website_fixture: Any,
    tmp_path: Path,
    real_chromium: str,
) -> None:
    transport, pages, requests = website_fixture
    pages["/app.js"] = (
        200,
        {"content-type": "application/javascript"},
        b"""
document.querySelector('#heading').textContent='Policy fixture rendered';
fetch('/write', {method:'POST', body:'never-submit'}).catch(()=>{});
fetch('/private/secret').catch(()=>{});
fetch('http://127.0.0.1/private').catch(()=>{});
fetch('https://fixture.example/redirect').catch(()=>{});
try { new WebSocket('wss://fixture.example/ws') } catch(e) {}
try { new Worker('/worker.js') } catch(e) {}
navigator.serviceWorker?.register('/service-worker.js').catch(()=>{});
const img=new Image();img.src='/unsafe.svg';document.body.append(img);
""",
    )
    pages["/redirect"] = (302, {"location": "http://169.254.169.254/latest/meta-data"}, b"")
    result = await capture_rendered_page(
        BASE,
        output_dir=tmp_path,
        fetcher=transport,
        config=BrowserRuntimeConfig(enabled=True, chromium_executable=real_chromium),
        limits=BrowserCaptureLimits(settle_ms=250, max_seconds=30),
    )
    assert result.status == "partial" and len(result.viewports) == 2, result.model_dump()
    reasons = {issue.reason for issue in result.issues}
    assert "non_get_blocked" in reasons and "robots_disallowed" in reasons
    assert "unsupported_resource_media_type" in reasons
    assert all(
        path not in requests
        for path in ["/write", "/private/secret", "/worker.js", "/service-worker.js", "/ws"]
    )
    assert all(urlsplit(url).hostname == "fixture.example" for url in transport.urls)


async def test_capability_does_not_claim_configured_browser_is_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(website_browser.platform, "system", lambda: "Darwin")
    assert (await browser_runtime_capability()) == {
        "status": "unavailable",
        "reason": "browser_disabled",
    }
    capability = await browser_runtime_capability(BrowserRuntimeConfig(enabled=True))
    assert capability["status"] == "unavailable"
