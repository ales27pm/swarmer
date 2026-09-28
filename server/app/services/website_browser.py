"""Optional real Chromium evidence, isolated from the host network by Linux namespaces.

The Python coordinator alone fetches permitted resources. JavaScript executes in
an ephemeral browser with no network interface. This is deliberately a restricted
rendering observation, not an interactive journey or production backend test.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import platform
import shlex
import shutil
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from app.services.website_assets import (
    AssetReference,
    PublicResourceSession,
    ResourceIssue,
    ResourceLimits,
    discover_asset_references,
    store_evidence,
)
from app.services.website_dossier import (
    CaptureError,
    FetchResponse,
    WebsiteFetcher,
    normalize_public_url,
)
from app.services.website_dossier_contracts import (
    CaptureLimits,
    Digest,
    DossierModel,
    SourceInventoryItem,
    UrlText,
)


class BrowserRuntimeConfig(DossierModel):
    enabled: bool = False
    chromium_executable: str | None = None


class BrowserCaptureLimits(ResourceLimits):
    settle_ms: int = Field(default=750, ge=0, le=3_000)
    max_dom_bytes: int = Field(default=1_048_576, ge=1_024, le=2_097_152)
    max_screenshot_bytes: int = Field(default=4_194_304, ge=1_024, le=8_388_608)
    max_text_characters: int = Field(default=32_000, ge=100, le=64_000)


class Screenshot(DossierModel):
    source_url: UrlText
    source_html_sha256: Digest
    dom_sha256: Digest
    viewport: Literal["desktop", "mobile"]
    width: int
    height: int
    sha256: Digest
    media_type: Literal["image/png"] = "image/png"
    local_path: str
    size_bytes: int
    captured_at: str
    full_page: Literal[False] = False


class RenderedViewport(DossierModel):
    viewport: Literal["desktop", "mobile"]
    source_url: UrlText
    dom_sha256: Digest
    dom_local_path: str
    dom_size_bytes: int
    title: str
    text: str
    text_truncated: bool
    screenshot: Screenshot
    asset_references: list[AssetReference]


class RenderedPage(DossierModel):
    source_url: UrlText
    final_url: UrlText | None = None
    status: Literal["unavailable", "captured", "partial", "failed"]
    reason: str | None = None
    renderer: Literal["playwright_chromium"] = "playwright_chromium"
    javascript_executed: bool = False
    network_isolation: Literal["linux_network_namespace", "unavailable"] = "unavailable"
    source_html_sha256: Digest | None = None
    viewports: list[RenderedViewport] = Field(default_factory=list)
    issues: list[ResourceIssue] = Field(default_factory=list)
    response_bytes: int = 0
    requests: int = 0
    restrictions: list[str] = Field(
        default_factory=lambda: [
            "same_hostname_only",
            "get_only",
            "no_cookies_or_credentials",
            "robots_enforced",
            "websocket_and_service_workers_blocked",
            "no_forms_or_user_interactions",
            "viewport_only",
            "restricted_render_not_full_browser_compatibility",
        ]
    )


class BrowserUnavailable(Exception):
    pass


async def _prepare_browser_launcher(executable: str, directory: Path) -> Path:
    """Require and verify OS isolation; there is no production bypass flag."""
    if platform.system() != "Linux" or not hasattr(os, "geteuid") or os.geteuid() == 0:
        raise BrowserUnavailable("network_isolation_unavailable")
    unshare, readlink = shutil.which("unshare"), shutil.which("readlink")
    if not unshare or not readlink or not Path(executable).is_file():
        raise BrowserUnavailable("browser_or_network_isolation_unavailable")
    try:
        parent_namespace = os.readlink("/proc/self/ns/net")
        probe = await asyncio.create_subprocess_exec(
            unshare,
            "--user",
            "--map-current-user",
            "--net",
            "--",
            readlink,
            "/proc/self/ns/net",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(probe.communicate(), timeout=5)
        except TimeoutError:
            probe.kill()
            await probe.wait()
            raise BrowserUnavailable("network_isolation_unavailable") from None
        if (
            probe.returncode != 0
            or output.decode().strip() == parent_namespace
            or not output.strip()
        ):
            raise BrowserUnavailable("network_isolation_unavailable")
    except OSError as exc:
        raise BrowserUnavailable("network_isolation_unavailable") from exc
    launcher = directory / "isolated-chromium"
    launcher.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(unshare)
        + " --user --map-current-user --net -- "
        + shlex.quote(executable)
        + ' "$@"\n'
    )
    launcher.chmod(0o700)
    return launcher


_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; media-src 'none'; object-src 'none'; frame-src 'none'; "
    "worker-src 'none'; base-uri 'self'; form-action 'none'"
)
_ALLOWED_TYPES = {
    "document": {"text/html", "application/xhtml+xml"},
    "script": {
        "text/javascript",
        "application/javascript",
        "application/ecmascript",
        "text/ecmascript",
    },
    "stylesheet": {"text/css"},
    "image": {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    },
    "font": {
        "font/woff",
        "font/woff2",
        "application/font-woff",
        "application/octet-stream",
        "font/ttf",
        "font/otf",
    },
    "fetch": {"application/json", "text/plain", "text/html"},
    "xhr": {"application/json", "text/plain", "text/html"},
}


async def _render(
    playwright: Any,
    *,
    source: str,
    output_dir: Path,
    limits: BrowserCaptureLimits,
    config: BrowserRuntimeConfig,
    session: PublicResourceSession,
    launcher_dir: Path,
) -> RenderedPage:
    from playwright.async_api import Error as PlaywrightError

    executable = config.chromium_executable or playwright.chromium.executable_path
    launcher = await _prepare_browser_launcher(executable, launcher_dir)
    result = RenderedPage(
        source_url=source, status="failed", network_isolation="linux_network_namespace"
    )
    try:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(launcher),
            chromium_sandbox=True,
            timeout=min(15_000, limits.max_seconds * 1_000),
            args=[
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-extensions",
                "--disable-sync",
                "--disable-breakpad",
            ],
        )
    except (PlaywrightError, OSError) as exc:
        raise BrowserUnavailable("isolated_browser_launch_failed") from exc
    try:
        document = await asyncio.to_thread(session.get, source)
        if document.status != 200:
            raise CaptureError("http_status")
        media = document.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media not in _ALLOWED_TYPES["document"]:
            raise CaptureError("not_html")
        result.final_url = document.url
        result.source_html_sha256 = hashlib.sha256(document.body).hexdigest()
        viewports: tuple[tuple[Literal["desktop", "mobile"], int, int, bool], ...] = (
            ("desktop", 1280, 900, False),
            ("mobile", 390, 844, True),
        )
        for viewport, width, height, is_mobile in viewports:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                screen={"width": width, "height": height},
                device_scale_factor=1,
                is_mobile=is_mobile,
                has_touch=is_mobile,
                java_script_enabled=True,
                service_workers="block",
                accept_downloads=False,
                permissions=[],
                locale="fr-CA",
                timezone_id="UTC",
            )
            response_cache: dict[str, FetchResponse] = {document.url: document}
            route_lock = asyncio.Lock()
            route_count = 0
            document_delivered = False

            def issue(url: str, reason: str) -> None:
                item = ResourceIssue(url=url[:2_048], reason=reason)
                if len(result.issues) < 100 and item not in result.issues:
                    result.issues.append(item)

            async def websocket(route: Any) -> None:
                issue(route.url, "websocket_blocked")
                await route.close()

            async def request_route(
                route: Any,
                *,
                lock: asyncio.Lock = route_lock,
                cache: dict[str, FetchResponse] = response_cache,
            ) -> None:
                nonlocal route_count, document_delivered
                async with lock:
                    request = route.request
                    route_count += 1
                    try:
                        if route_count > limits.max_requests:
                            raise CaptureError("request_limit")
                        if request.method != "GET":
                            raise CaptureError("non_get_blocked")
                        url = normalize_public_url(request.url)
                        resource_type = request.resource_type
                        if resource_type == "document":
                            if document_delivered or url != document.url:
                                raise CaptureError("navigation_blocked")
                            document_delivered = True
                        if resource_type not in _ALLOWED_TYPES:
                            raise CaptureError("resource_type_blocked")
                        response = cache.pop(url, None)
                        if response is None:
                            response = await asyncio.to_thread(
                                session.get, url, origin=document.url
                            )
                        if response.url != url:
                            cache[response.url] = response
                            await route.fulfill(status=302, headers={"location": response.url})
                            return
                        content_type = (
                            response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                        )
                        if response.status != 200:
                            raise CaptureError("http_status")
                        if content_type not in _ALLOWED_TYPES[resource_type]:
                            raise CaptureError("unsupported_resource_media_type")
                        # Ignore cookies, refresh, link/preload, CSP report targets,
                        # auth, redirects and all other server-controlled headers.
                        headers = {
                            "content-type": response.headers.get("content-type", content_type),
                            "x-content-type-options": "nosniff",
                            "cache-control": "no-store",
                        }
                        if resource_type == "document":
                            headers["content-security-policy"] = _CSP
                            headers["permissions-policy"] = (
                                "camera=(), microphone=(), geolocation=(), usb=(), serial=()"
                            )
                        await route.fulfill(status=200, headers=headers, body=response.body)
                    except (CaptureError, ValueError) as exc:
                        # Detailed transport reasons are bounded; never return response data.
                        issue(
                            request.url,
                            str(exc) if isinstance(exc, CaptureError) else "invalid_public_url",
                        )
                        await route.abort("blockedbyclient")

            try:
                await context.route("**/*", request_route)
                await context.route_web_socket("**/*", websocket)
                page = await context.new_page()
                page.on("pageerror", lambda _: issue(document.url, "page_script_error"))
                page.on(
                    "console",
                    lambda message: (
                        issue(document.url, "browser_console_error")
                        if message.type == "error"
                        else None
                    ),
                )
                page.on(
                    "requestfailed", lambda request: issue(request.url, "browser_request_failed")
                )
                await page.goto(
                    document.url, wait_until="domcontentloaded", timeout=limits.max_seconds * 1_000
                )
                await page.wait_for_timeout(limits.settle_ms)
                # Stop document loading; timers can continue. DOM and screenshot
                # are sequential observations, not an atomic frozen page state.
                await page.evaluate("window.stop()")
                dom = await page.evaluate(
                    "limit => {const s=document.documentElement.outerHTML; "
                    "return s.length > limit ? null : s}",
                    limits.max_dom_bytes,
                )
                if dom is None or len(dom.encode()) > limits.max_dom_bytes:
                    raise CaptureError("dom_byte_limit")
                body = dom.encode()
                dom_path, dom_digest = store_evidence(output_dir, body, suffix=".dom.bin")
                snapshot = await page.evaluate(
                    "limit => {const t=document.body?.innerText||''; return {title:document.title.slice(0,1000), "
                    "text:t.slice(0,limit), truncated:t.length>limit}}",
                    limits.max_text_characters,
                )
                screenshot = await page.screenshot(
                    type="png", full_page=False, animations="disabled", timeout=10_000
                )
                if len(screenshot) > limits.max_screenshot_bytes:
                    raise CaptureError("screenshot_byte_limit")
                path, digest = store_evidence(output_dir, screenshot, suffix=".png")
                references, truncated = discover_asset_references(
                    dom, source_url=document.url, page_sha256=dom_digest
                )
                if truncated:
                    issue(document.url, "asset_reference_limit")
                if snapshot["truncated"]:
                    issue(document.url, "text_limit")
                result.viewports.append(
                    RenderedViewport(
                        viewport=viewport,
                        source_url=document.url,
                        dom_sha256=dom_digest,
                        dom_local_path=dom_path,
                        dom_size_bytes=len(body),
                        title=snapshot["title"],
                        text=snapshot["text"],
                        text_truncated=snapshot["truncated"],
                        asset_references=references,
                        screenshot=Screenshot(
                            source_url=document.url,
                            source_html_sha256=result.source_html_sha256,
                            dom_sha256=dom_digest,
                            viewport=viewport,
                            width=width,
                            height=height,
                            sha256=digest,
                            local_path=path,
                            size_bytes=len(screenshot),
                            captured_at=datetime.now(UTC).isoformat(),
                        ),
                    )
                )
                result.javascript_executed = True
            finally:
                await context.close()
        result.status = "partial" if result.issues else "captured"
    except (CaptureError, ValueError) as exc:
        result.status = "partial" if result.viewports else "failed"
        result.reason = str(exc) if isinstance(exc, CaptureError) else "render_unavailable"
    except (PlaywrightError, OSError):
        result.status = "partial" if result.viewports else "failed"
        result.reason = "browser_render_failed"
    finally:
        await browser.close()
        result.requests, result.response_bytes = session.requests, session.response_bytes
    return result


async def capture_rendered_page(
    url: str,
    *,
    output_dir: Path,
    limits: BrowserCaptureLimits | None = None,
    config: BrowserRuntimeConfig | None = None,
    fetcher: WebsiteFetcher | None = None,
) -> RenderedPage:
    """Render two independent viewports; no browser installation or unsafe fallback.

    On non-Linux hosts, absent dependencies, or denied user/network namespaces,
    return unavailable before fetching source content. Browser capability is an
    explicit deployment option and cannot be enabled by page content or API input.
    """
    source = normalize_public_url(url)
    config, limits = config or BrowserRuntimeConfig(), limits or BrowserCaptureLimits()
    if not config.enabled:
        return RenderedPage(source_url=source, status="unavailable", reason="browser_disabled")
    if importlib.util.find_spec("playwright") is None:
        return RenderedPage(
            source_url=source, status="unavailable", reason="playwright_not_installed"
        )
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    session = PublicResourceSession(source, limits=limits, fetcher=fetcher)
    try:
        async with asyncio.timeout(limits.max_seconds):
            with tempfile.TemporaryDirectory(prefix="swarmer-browser-") as launcher_dir:
                async with async_playwright() as playwright:
                    return await _render(
                        playwright,
                        source=source,
                        output_dir=output_dir,
                        limits=limits,
                        config=config,
                        session=session,
                        launcher_dir=Path(launcher_dir),
                    )
    except BrowserUnavailable as exc:
        return RenderedPage(source_url=source, status="unavailable", reason=str(exc))
    except TimeoutError:
        return RenderedPage(
            source_url=source,
            status="failed",
            reason="duration_limit",
            requests=session.requests,
            response_bytes=session.response_bytes,
        )
    except (PlaywrightError, OSError):
        return RenderedPage(
            source_url=source,
            status="failed",
            reason="browser_runtime_failed",
            requests=session.requests,
            response_bytes=session.response_bytes,
        )


def extract_rendered_inventory(rendered_pages: Sequence[RenderedPage]) -> list[SourceInventoryItem]:
    """Read verified DOM evidence into additional, explicitly labelled source items.

    Does not relabel original source-HTML records, assert new pages were crawled,
    or infer facts. Callers retain these items alongside their rendering receipts.
    """
    from app.services.website_dossier import _parse_page

    if len(rendered_pages) > 30:
        raise ValueError("rendered_page_limit")
    items: list[SourceInventoryItem] = []
    seen: set[tuple[str, str, str, str, str | None]] = set()
    for rendered in rendered_pages:
        for viewport in rendered.viewports:
            path = Path(viewport.dom_local_path)
            if path.is_symlink() or path.stat().st_size > 2_097_152:
                raise CaptureError("invalid_dom_evidence")
            body = path.read_bytes()
            if (
                len(body) != viewport.dom_size_bytes
                or hashlib.sha256(body).hexdigest() != viewport.dom_sha256
            ):
                raise CaptureError("invalid_dom_evidence")
            _, inventory = _parse_page(
                FetchResponse(
                    viewport.source_url, 200, {"content-type": "text/html; charset=utf-8"}, body
                ),
                viewport.source_url,
                CaptureLimits(),
            )
            for item in inventory:
                key = (
                    item.source_url,
                    item.kind,
                    item.source_locator,
                    item.text,
                    item.original_url,
                )
                if key in seen:
                    continue
                seen.add(key)
                item.source_locator = f"rendered_dom:{viewport.viewport}:{item.source_locator}"[
                    :100
                ]
                item.id = (
                    "rendered_"
                    + hashlib.sha256(
                        f"{item.page_sha256}:{item.source_locator}:{item.kind}".encode()
                    ).hexdigest()[:24]
                )
                items.append(item)
    return items


async def browser_runtime_capability(config: BrowserRuntimeConfig | None = None) -> dict[str, str]:
    """Verify browser installation and OS isolation without fetching a website."""
    config = config or BrowserRuntimeConfig()
    if not config.enabled:
        return {"status": "unavailable", "reason": "browser_disabled"}
    if importlib.util.find_spec("playwright") is None:
        return {"status": "unavailable", "reason": "playwright_not_installed"}
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    try:
        async with asyncio.timeout(20):
            with tempfile.TemporaryDirectory(prefix="swarmer-browser-probe-") as directory:
                async with async_playwright() as playwright:
                    executable = config.chromium_executable or playwright.chromium.executable_path
                    launcher = await _prepare_browser_launcher(executable, Path(directory))
                    browser = await playwright.chromium.launch(
                        executable_path=str(launcher),
                        headless=True,
                        chromium_sandbox=True,
                        timeout=10_000,
                    )
                    await browser.close()
        return {"status": "available", "reason": "isolated_browser_verified"}
    except BrowserUnavailable as exc:
        return {"status": "unavailable", "reason": str(exc)}
    except (TimeoutError, PlaywrightError, OSError):
        return {"status": "unavailable", "reason": "isolated_browser_launch_failed"}
