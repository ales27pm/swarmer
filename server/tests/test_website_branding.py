from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from app.services import website_branding
from app.services.website_branding import BrandingError, InfographicArtistClient


@pytest.fixture
def mcp_server() -> Iterator[tuple[str, list[dict[str, Any]], dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    config: dict[str, Any] = {"sse": False, "tool_error": False, "uri_media": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"payload": payload, "headers": dict(self.headers)})
            method = payload["method"]
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {
                    "protocolVersion": config.get("protocol", "2025-03-26"),
                    "capabilities": {"tools": {}},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "generate_brand_directions",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "promise": {"type": "string"},
                                    "sector": {"type": "string"},
                                },
                                "required": ["name", "promise", "sector"],
                            },
                        },
                        {
                            "name": "search_design_systems",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                        },
                        {
                            "name": "critique_brand_image",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "image": {
                                        "type": "string",
                                        **({"format": "uri"} if config["uri_media"] else {}),
                                    },
                                    "reference": {"type": "string", "format": "uri"},
                                },
                            },
                        },
                    ]
                }
            else:
                result = {
                    "isError": config["tool_error"],
                    "content": [{"type": "text", "text": "Trois pistes de composition"}],
                    "structuredContent": {
                        "view": "directions",
                        "data": {"routes": ["Editorial", "Studio", "Catalogue"]},
                    },
                }
            body = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode()
            if config["sse"] and method == "tools/call":
                body = b"event: message\ndata: " + body + b"\n\n"
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/event-stream"
                if config["sse"] and method == "tools/call"
                else "application/json",
            )
            if method == "initialize":
                self.send_header("Mcp-Session-Id", "fixture-session")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", calls, config
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize("protocol", ["2025-03-26", "2025-06-18"])
async def test_real_http_mcp_initialize_session_discovery_and_call(
    mcp_server: Any, sse: bool, protocol: str
) -> None:
    endpoint, calls, config = mcp_server
    config["sse"] = sse
    config["protocol"] = protocol
    result = await InfographicArtistClient(endpoint, "fixture-secret").call(
        "generate_brand_directions",
        {"name": "Atelier", "promise": "Rendre les services compréhensibles", "sector": "Services"},
    )
    assert result["provider"] == "infographic_artist"
    assert result["status"] == "succeeded"
    assert result["result"]["structuredContent"]["data"]["routes"] == [
        "Editorial",
        "Studio",
        "Catalogue",
    ]
    assert [call["payload"]["method"] for call in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert calls[-1]["headers"]["Mcp-Session-Id"] == "fixture-session"
    assert calls[-1]["headers"]["MCP-Protocol-Version"] == protocol
    assert calls[-1]["headers"]["Authorization"] == "Bearer fixture-secret"


async def test_native_client_never_forwards_server_local_image_path(
    mcp_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dns_queries: list[str] = []

    def resolver(host: str, port: int, **_: Any) -> list[Any]:
        dns_queries.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(website_branding, "getaddrinfo", resolver, raising=False)
    endpoint, calls, config = mcp_server
    client = InfographicArtistClient(endpoint)
    with pytest.raises(BrandingError, match="branding_media_transport_unavailable"):
        await client.call("critique_brand_image", {"image": "/srv/swarmer/capture.png"})
    assert all(call["payload"]["method"] != "tools/call" for call in calls)
    config["uri_media"] = True
    with pytest.raises(BrandingError, match="branding_requires_public_media_url"):
        await client.call("critique_brand_image", {"image": "/srv/swarmer/capture.png"})
    assert not dns_queries
    result = await client.call(
        "critique_brand_image", {"image": "https://approved.example/capture.png"}
    )
    assert result["status"] == "succeeded"
    assert dns_queries == ["approved.example"]


@pytest.mark.parametrize("protocol", ["2024-11-05", "2099-01-01"])
async def test_unsupported_mcp_protocol_stops_after_initialize(
    mcp_server: Any, protocol: str
) -> None:
    endpoint, calls, config = mcp_server
    config["protocol"] = protocol
    with pytest.raises(BrandingError, match="unsupported_mcp_protocol"):
        await InfographicArtistClient(endpoint).call("search_design_systems", {})
    assert [call["payload"]["method"] for call in calls] == ["initialize"]


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["93.184.216.34", "10.0.0.2"],
        ["169.254.169.254"],
        ["::1"],
        ["fc00::1"],
        ["224.0.0.1"],
        ["::ffff:93.184.216.34"],
        [],
    ],
)
@pytest.mark.parametrize("argument", ["image", "reference"])
async def test_critique_rejects_nonpublic_dns_answers_before_forwarding(
    mcp_server: Any,
    monkeypatch: pytest.MonkeyPatch,
    addresses: list[str],
    argument: str,
) -> None:
    def resolver(host: str, port: int, **_: Any) -> list[Any]:
        answers = addresses if host == "media.example" else ["93.184.216.34"]
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in answers
        ]

    monkeypatch.setattr(website_branding, "getaddrinfo", resolver, raising=False)
    endpoint, calls, config = mcp_server
    config["uri_media"] = True
    arguments = {"image": "https://approved.example/image.png"}
    arguments[argument] = "https://media.example/image.png"
    with pytest.raises(BrandingError, match="branding_requires_public_media_url"):
        await InfographicArtistClient(endpoint).call("critique_brand_image", arguments)
    assert all(call["payload"]["method"] != "tools/call" for call in calls)


async def test_unsupported_critique_contract_does_not_resolve_dns(
    mcp_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolver(*_: Any, **__: Any) -> list[Any]:
        pytest.fail("unsupported media contracts must fail before DNS resolution")

    monkeypatch.setattr(website_branding, "getaddrinfo", resolver, raising=False)
    endpoint, calls, _ = mcp_server
    with pytest.raises(BrandingError, match="branding_media_transport_unavailable"):
        await InfographicArtistClient(endpoint).call(
            "critique_brand_image", {"image": "https://media.example/image.png"}
        )
    assert all(call["payload"]["method"] != "tools/call" for call in calls)


async def test_critique_public_dns_is_checked_once_per_host(
    mcp_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dns_queries: list[str] = []

    def resolver(host: str, port: int, **_: Any) -> list[Any]:
        dns_queries.append(host)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", port, 0, 0)),
        ]

    monkeypatch.setattr(website_branding, "getaddrinfo", resolver, raising=False)
    endpoint, calls, config = mcp_server
    config["uri_media"] = True
    arguments = {
        "image": "https://approved.example/image.png",
        "reference": "https://approved.example/reference.png",
    }
    result = await InfographicArtistClient(endpoint).call("critique_brand_image", arguments)
    assert result["status"] == "succeeded"
    assert dns_queries == ["approved.example"]
    assert calls[-1]["payload"]["params"]["arguments"] == arguments


async def test_critique_dns_uses_wall_deadline_without_blocking_event_loop(
    mcp_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolving = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    dns_queries: list[str] = []

    def resolver(host: str, port: int, **_: Any) -> list[Any]:
        dns_queries.append(host)
        resolving.set()
        try:
            release.wait(timeout=2)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        finally:
            finished.set()

    monkeypatch.setattr(website_branding, "getaddrinfo", resolver, raising=False)
    endpoint, calls, config = mcp_server
    config["uri_media"] = True
    task = asyncio.create_task(
        InfographicArtistClient(endpoint, timeout_seconds=0.2).call(
            "critique_brand_image", {"image": "https://media.example/image.png"}
        )
    )
    try:
        for _ in range(100):
            if resolving.is_set() or task.done():
                break
            await asyncio.sleep(0.005)
        assert resolving.is_set(), "The media DNS preflight must run before forwarding"
        before = time.monotonic()
        await asyncio.sleep(0.01)
        assert time.monotonic() - before < 0.1
        with pytest.raises(BrandingError, match="branding_timeout"):
            await task
        assert not finished.is_set()
    finally:
        release.set()
        if resolving.is_set():
            assert await asyncio.to_thread(finished.wait, 1)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert dns_queries == ["media.example"]
    assert all(call["payload"]["method"] != "tools/call" for call in calls)


async def test_missing_provider_and_tool_failure_never_fake_success(mcp_server: Any) -> None:
    with pytest.raises(BrandingError, match="infographic_artist_not_configured"):
        await InfographicArtistClient(None).call("search_design_systems", {"query": "composition"})
    endpoint, _, config = mcp_server
    config["tool_error"] = True
    with pytest.raises(BrandingError, match="branding_tool_failed"):
        await InfographicArtistClient(endpoint).call(
            "search_design_systems", {"query": "composition"}
        )


async def test_endpoint_redirects_and_remote_error_text_do_not_leak_secrets() -> None:
    async def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://attacker.example"}, text="SECRET")

    with pytest.raises(BrandingError, match="^branding_http_error$"):
        await InfographicArtistClient(
            "https://provider.example/mcp", transport=httpx.MockTransport(redirect)
        ).call("search_design_systems", {})
    with pytest.raises(BrandingError, match="invalid_branding_endpoint"):
        await InfographicArtistClient("http://provider.example/mcp").call(
            "search_design_systems", {}
        )


async def test_oversize_response_is_bounded() -> None:
    async def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))

    with pytest.raises(BrandingError, match="branding_response_byte_limit"):
        await InfographicArtistClient(
            "https://provider.example/mcp", transport=httpx.MockTransport(response)
        ).call("search_design_systems", {})


async def test_schema_drift_fails_before_call(mcp_server: Any) -> None:
    endpoint, calls, _ = mcp_server
    with pytest.raises(BrandingError, match="branding_contract_mismatch"):
        await InfographicArtistClient(endpoint).call(
            "search_design_systems", {"query": "composition", "kind": "studio"}
        )
    assert all(call["payload"]["method"] != "tools/call" for call in calls)


async def test_wall_deadline_includes_initialize_and_no_retry_occurs() -> None:
    calls = 0

    async def delayed(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={})

    with pytest.raises(BrandingError, match="branding_timeout"):
        await InfographicArtistClient(
            "https://provider.example/mcp",
            timeout_seconds=0.1,
            transport=httpx.MockTransport(delayed),
        ).call("search_design_systems", {})
    assert calls == 1
