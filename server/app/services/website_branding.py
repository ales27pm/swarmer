"""Native bounded MCP HTTP adapter for Infographic Artist, without fake fallbacks."""

from __future__ import annotations

import asyncio
import json
import time
from socket import getaddrinfo
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.website_dossier import CaptureError, _resolve, normalize_public_url

ToolName = Literal["search_design_systems", "generate_brand_directions", "critique_brand_image"]
MAX_RESPONSE_BYTES = 1024 * 1024


class BrandingError(ValueError):
    """Stable public reason; never includes secrets or third-party response text."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Search(_Args):
    query: str | None = Field(default=None, max_length=4000)
    kind: str | None = Field(default=None, max_length=100)
    limit: int | None = Field(default=None, ge=1, le=30)


class _Directions(_Args):
    name: str = Field(min_length=1, max_length=200)
    promise: str = Field(min_length=1, max_length=8000)
    sector: str = Field(min_length=1, max_length=1000)
    audience: str | None = Field(default=None, max_length=2000)
    must_avoid: list[str] | None = Field(default=None, max_length=30)
    risk_tolerance: str | None = Field(default=None, max_length=500)
    traits: list[str] | None = Field(default=None, max_length=30)


class _Critique(_Args):
    image: str = Field(min_length=1, max_length=2048)
    reference: str | None = Field(default=None, max_length=2048)
    context: str | None = Field(default=None, max_length=8000)


class InfographicArtistClient:
    """One initialize/list/call session per request, using server-advertised tool contracts.

    Endpoint is administrator configuration, never a URL from a website or an end user.
    Critique only supports a server explicitly advertising image/reference `format: uri`;
    ChatGPT's local-file upload bridge is not assumed to exist in this native client.
    """

    def __init__(
        self,
        endpoint: str | None,
        bearer_token: str | None = None,
        *,
        timeout_seconds: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.endpoint = endpoint
        self.bearer_token = bearer_token
        self.timeout_seconds = min(max(timeout_seconds, 0.1), 60)
        self.transport = transport

    async def call(self, tool_name: ToolName, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.endpoint:
            raise BrandingError("infographic_artist_not_configured")
        self._validate_endpoint()
        models: dict[str, type[_Args]] = {
            "search_design_systems": _Search,
            "generate_brand_directions": _Directions,
            "critique_brand_image": _Critique,
        }
        if tool_name not in models:
            raise BrandingError("unsupported_branding_tool")
        try:
            validated = models[tool_name].model_validate(arguments).model_dump(exclude_none=True)
        except ValidationError as exc:
            raise BrandingError("invalid_branding_arguments") from exc
        if len(json.dumps(validated).encode()) > 48 * 1024:
            raise BrandingError("branding_request_byte_limit")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.bearer_token:
            headers["Authorization"] = "Bearer " + self.bearer_token
        deadline = time.monotonic() + self.timeout_seconds
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    initialized = await self._request(
                        client,
                        headers,
                        "initialize",
                        {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "swarmer-website-studio", "version": "1.0"},
                        },
                        1,
                    )
                    version = initialized.get("protocolVersion")
                    if version not in {"2025-03-26", "2025-06-18"}:
                        raise BrandingError("unsupported_mcp_protocol")
                    headers["MCP-Protocol-Version"] = version
                    await self._request(client, headers, "notifications/initialized", {}, None)
                    discovered: dict[str, Any] | None = None
                    cursor: str | None = None
                    for page in range(5):
                        listing = await self._request(
                            client,
                            headers,
                            "tools/list",
                            {"cursor": cursor} if cursor else {},
                            2 + page,
                        )
                        advertised = listing.get("tools")
                        if not isinstance(advertised, list) or len(advertised) > 500:
                            raise BrandingError("invalid_mcp_tool_catalog")
                        discovered = next(
                            (
                                t
                                for t in advertised
                                if isinstance(t, dict) and t.get("name") == tool_name
                            ),
                            None,
                        )
                        if discovered:
                            break
                        cursor = listing.get("nextCursor")
                        if not cursor:
                            break
                        if not isinstance(cursor, str) or len(cursor) > 2048:
                            raise BrandingError("invalid_mcp_tool_catalog")
                    if discovered is None:
                        raise BrandingError("branding_tool_not_available")
                    self._check_advertised_contract(tool_name, validated, discovered)
                    if tool_name == "critique_brand_image":
                        await self._check_public_media(validated, deadline)
                    result = await self._request(
                        client,
                        headers,
                        "tools/call",
                        {"name": tool_name, "arguments": validated},
                        10,
                    )
                    if result.get("isError"):
                        raise BrandingError("branding_tool_failed")
                    if not isinstance(result.get("content"), list):
                        raise BrandingError("invalid_branding_result")
                    return {
                        "provider": "infographic_artist",
                        "tool": tool_name,
                        "status": "succeeded",
                        "result": result,
                    }
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise BrandingError("branding_timeout") from exc
        except (httpx.HTTPError, UnicodeError) as exc:
            raise BrandingError("branding_transport_failed") from exc

    def _validate_endpoint(self) -> None:
        try:
            parsed = urlsplit(self.endpoint or "")
            _ = parsed.port  # Access validates malformed/out-of-range configured ports.
            loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            valid = parsed.scheme == "https" or (parsed.scheme == "http" and loopback)
            if (
                not valid
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or any(ord(c) < 32 or ord(c) == 127 for c in self.endpoint or "")
            ):
                raise ValueError
        except ValueError as exc:
            raise BrandingError("invalid_branding_endpoint") from exc

    @staticmethod
    def _check_advertised_contract(
        tool_name: str, arguments: dict[str, Any], tool: dict[str, Any]
    ) -> None:
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise BrandingError("unsupported_branding_contract")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise BrandingError("unsupported_branding_contract")
        if any(key not in properties for key in arguments) or any(
            key not in arguments for key in required
        ):
            raise BrandingError("branding_contract_mismatch")
        if tool_name == "critique_brand_image":
            for key in ("image", "reference"):
                if key not in arguments:
                    continue
                prop = properties.get(key)
                if not isinstance(prop, dict) or prop.get("format") not in {"uri", "uri-reference"}:
                    raise BrandingError("branding_media_transport_unavailable")

    @staticmethod
    async def _check_public_media(arguments: dict[str, Any], deadline: float) -> None:
        # This checks current local DNS only. The remote provider must independently
        # constrain its own resolution, redirects and connection peer.
        hosts: set[str] = set()
        try:
            for key in ("image", "reference"):
                if key not in arguments:
                    continue
                parsed = urlsplit(normalize_public_url(arguments[key]))
                if parsed.scheme != "https" or not parsed.hostname:
                    raise ValueError
                hosts.add(parsed.hostname)
        except ValueError as exc:
            raise BrandingError("branding_requires_public_media_url") from exc
        for host in sorted(hosts):
            try:
                # Reuse the bounded DNS worker and reject every nonglobal/malformed
                # answer; never run a blocking resolver on the event loop.
                await asyncio.to_thread(_resolve, host, 443, deadline, getaddrinfo)
            except CaptureError as exc:
                if str(exc) in {"dns_timeout", "duration_limit"}:
                    raise BrandingError("branding_timeout") from exc
                raise BrandingError("branding_requires_public_media_url") from exc

    async def _request(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        method: str,
        params: dict[str, Any],
        request_id: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            payload["id"] = request_id
        async with client.stream(
            "POST", cast(str, self.endpoint), json=payload, headers=headers
        ) as response:
            if response.status_code not in {200, 202, 204}:
                raise BrandingError("branding_http_error")
            if method == "initialize" and response.headers.get("mcp-session-id"):
                session = response.headers["mcp-session-id"]
                if len(session) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in session):
                    raise BrandingError("invalid_mcp_session")
                headers["Mcp-Session-Id"] = session
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise BrandingError("branding_response_byte_limit")
                # SSE streams may stay open; return once the matching response event is complete.
                if response.headers.get("content-type", "").startswith("text/event-stream"):
                    for event in bytes(body).replace(b"\r\n", b"\n").split(b"\n\n")[:-1]:
                        data = b"\n".join(
                            line[5:].lstrip()
                            for line in event.split(b"\n")
                            if line.startswith(b"data:")
                        )
                        if not data:
                            continue
                        decoded = self._decode(data)
                        if decoded.get("id") == request_id and request_id is not None:
                            return self._result(decoded, request_id)
            if request_id is None:
                return {}
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                raise BrandingError("missing_mcp_response")
            return self._result(self._decode(bytes(body)), request_id)

    @staticmethod
    def _decode(body: bytes) -> dict[str, Any]:
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise BrandingError("invalid_mcp_response") from exc
        if not isinstance(parsed, dict):
            raise BrandingError("invalid_mcp_response")
        return parsed

    @staticmethod
    def _result(response: dict[str, Any], request_id: int) -> dict[str, Any]:
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise BrandingError("invalid_mcp_response")
        if response.get("error") is not None:
            raise BrandingError("branding_rpc_error")
        if not isinstance(response.get("result"), dict):
            raise BrandingError("invalid_mcp_result")
        return cast(dict[str, Any], response["result"])
