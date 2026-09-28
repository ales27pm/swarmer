"""Bounded, GET-only website source capture, independent of models and workers."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Protocol, cast
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from pydantic import JsonValue

from app.services.website_dossier_contracts import (
    CapturedPage,
    CaptureLimits,
    CoverageEntry,
    DiscoveryIssue,
    DossierCoverage,
    ObservedForm,
    SourceInventoryItem,
    SourceLink,
    WebsiteDossier,
)

USER_AGENT = "monGARS-Dossier/1.0"
_DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".zip")
_DNS_SLOT = threading.BoundedSemaphore(1)


class CaptureError(ValueError):
    """A bounded, public reason; never includes response bodies or credentials."""

    def __init__(self, reason: str, *, received_bytes: int = 0) -> None:
        super().__init__(reason)
        self.received_bytes = received_bytes


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes


class WebsiteFetcher(Protocol):
    def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse: ...


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        address.is_global
        and not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
        and not (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped)
    )


def normalize_public_url(value: str) -> str:
    """Normalize identity without reordering query parameters or assuming ownership."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2_048
        or value != value.strip()
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("invalid_public_url")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid_public_url") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or len(host) > 253
        or parsed.username is not None
        or parsed.password is not None
        or "%" in host
        or host.endswith((".", ".local", ".localhost"))
        or host == "localhost"
        or parsed.netloc.endswith(":")
        or port not in {None, 80 if parsed.scheme == "http" else 443}
    ):
        raise ValueError("invalid_public_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if all(char in "0123456789." for char in host):
            raise ValueError("invalid_public_url") from None
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
            raise ValueError("invalid_public_url") from None
    else:
        if not _public_address(host):
            raise ValueError("non_public_address")
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="%:@!$&'()*+,;=/?-._~")
    result = urlunsplit((parsed.scheme, host, path, query, ""))
    if len(result) > 2_048:
        raise ValueError("invalid_public_url")
    return result


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CaptureError("duration_limit")
    return remaining


def _resolve(host: str, port: int, deadline: float, resolver: Callable[..., Any]) -> list[Any]:
    if not _DNS_SLOT.acquire(timeout=_remaining(deadline)):
        raise CaptureError("dns_timeout")
    result: queue.Queue[object] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result.put(resolver(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM))
        except (OSError, TypeError, ValueError) as exc:
            result.put(exc)
        finally:
            _DNS_SLOT.release()

    try:
        threading.Thread(target=run, name="website-dossier-dns", daemon=True).start()
    except RuntimeError as exc:
        _DNS_SLOT.release()
        raise CaptureError("dns_unavailable") from exc
    try:
        addresses = result.get(timeout=_remaining(deadline))
    except queue.Empty as exc:
        raise CaptureError("dns_timeout") from exc
    if not isinstance(addresses, list) or not addresses:
        raise CaptureError("dns_unavailable")
    for answer in addresses:
        if (
            not isinstance(answer, tuple)
            or len(answer) != 5
            or answer[0] not in {socket.AF_INET, socket.AF_INET6}
            or answer[1] != socket.SOCK_STREAM
            or not isinstance(answer[4], tuple)
            or len(answer[4]) not in {2, 4}
            or answer[4][1] != port
            or not _public_address(str(answer[4][0]))
            or (len(answer[4]) == 4 and answer[4][2:] != (0, 0))
        ):
            raise CaptureError("non_public_address")
    return addresses


class PublicHttpFetcher:
    """Connect only to a validated DNS answer, preserving TLS/SNI and no proxy/cookies."""

    def __init__(self, *, resolver: Callable[..., Any] = socket.getaddrinfo) -> None:
        self.resolver = resolver

    def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
        url = normalize_public_url(url)
        parsed = urlsplit(url)
        host = cast(str, parsed.hostname)
        port = 443 if parsed.scheme == "https" else 80
        addresses = _resolve(host, port, deadline, self.resolver)
        family, kind, protocol, _, address = addresses[0]
        connection = http.client.HTTPConnection(host, port, timeout=_remaining(deadline))
        sock = socket.socket(family, kind, protocol)

        # Closing the underlying socket also bounds a slow/dripping HTTP header or body.
        def abort() -> None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
            sock.close()

        timer = threading.Timer(_remaining(deadline), abort)
        timer.daemon = True
        timer.start()
        length = 0
        try:
            sock.settimeout(_remaining(deadline))
            sock.connect(address)
            if ipaddress.ip_address(sock.getpeername()[0]) != ipaddress.ip_address(address[0]):
                raise CaptureError("peer_mismatch")
            if parsed.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            connection.sock = sock
            connection.request(
                "GET",
                urlunsplit(("", "", parsed.path, parsed.query, "")),
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml,text/plain",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            headers = {name.lower(): value for name, value in response.getheaders()}
            if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                raise CaptureError("unsupported_encoding")
            if (
                headers.get("content-length", "").isdigit()
                and int(headers["content-length"]) > max_bytes
            ):
                raise CaptureError("response_byte_limit")
            chunks: list[bytes] = []
            length = 0
            while length <= max_bytes:
                sock.settimeout(_remaining(deadline))
                chunk = response.read1(min(8_192, max_bytes + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
            if length > max_bytes:
                raise CaptureError("response_byte_limit", received_bytes=length)
            _remaining(deadline)
            return FetchResponse(url, response.status, headers, b"".join(chunks))
        except (OSError, http.client.HTTPException) as exc:
            reason = "duration_limit" if time.monotonic() >= deadline else "http_unavailable"
            raise CaptureError(reason, received_bytes=length) from exc
        finally:
            timer.cancel()
            connection.close()
            sock.close()


def _same_site(url: str, source: str) -> bool:
    left, right = urlsplit(url), urlsplit(source)
    return left.hostname == right.hostname and (
        left.scheme == right.scheme or (right.scheme == "http" and left.scheme == "https")
    )


def _tls_downgrade(target: str, origin: str) -> bool:
    return urlsplit(origin).scheme == "https" and urlsplit(target).scheme == "http"


def _utc() -> str:
    return datetime.now(UTC).isoformat()


class _Budget:
    def __init__(self, fetcher: WebsiteFetcher, limits: CaptureLimits, source: str) -> None:
        self.fetcher, self.limits, self.source = fetcher, limits, source
        self.started = time.monotonic()
        self.deadline = self.started + limits.max_seconds
        self.bytes = 0

    def fetch(self, url: str, *, allowed: Callable[[str], bool] | None = None) -> FetchResponse:
        seen: set[str] = set()
        for hop in range(self.limits.max_redirects + 1):
            _remaining(self.deadline)
            url = normalize_public_url(url)
            if not _same_site(url, self.source):
                raise CaptureError("outside_origin")
            if allowed is not None and not allowed(url):
                raise CaptureError("robots_disallowed")
            if url in seen:
                raise CaptureError("redirect_loop")
            seen.add(url)
            maximum = min(self.limits.max_page_bytes, self.limits.max_total_bytes - self.bytes)
            if maximum <= 0:
                raise CaptureError("total_byte_limit")
            try:
                response = self.fetcher.get(url, max_bytes=maximum, deadline=self.deadline)
            except CaptureError as exc:
                self.bytes += exc.received_bytes
                raise
            except OSError as exc:
                raise CaptureError("http_unavailable") from exc
            _remaining(self.deadline)
            if response.url != url or not 100 <= response.status <= 599:
                raise CaptureError("invalid_transport_response")
            self.bytes += len(response.body)
            if len(response.body) > maximum:
                raise CaptureError("response_byte_limit")
            if response.status not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            if not location or hop == self.limits.max_redirects:
                raise CaptureError("redirect_limit")
            try:
                target = normalize_public_url(urljoin(url, location))
            except ValueError as exc:
                raise CaptureError("unsafe_redirect") from exc
            if _tls_downgrade(target, url):
                raise CaptureError("tls_downgrade")
            url = target
        raise CaptureError("redirect_limit")


class _PageParser(HTMLParser):
    BLOCKS = frozenset(
        {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "address", "td", "blockquote"}
    )

    def __init__(self, page_url: str, max_text: int) -> None:
        super().__init__(convert_charrefs=True)
        self.url, self.max_text = page_url, max_text
        self.base_url = page_url
        self._base_seen = False
        self.title = ""
        self.language = ""
        self.metadata: dict[str, str] = {}
        self.structured_data: list[JsonValue] = []
        self.links: list[SourceLink] = []
        self.forms: list[ObservedForm] = []
        self.blocks: list[tuple[str, str, str]] = []
        self.text_parts: list[str] = []
        self.truncated: set[str] = set()
        self._counts: dict[str, int] = {}
        self._head = False
        self._hidden: list[str] = []
        self._title = False
        self._json = False
        self._json_parts: list[str] = []
        self._block: tuple[str, str, list[str]] | None = None
        self._anchor: tuple[str, str, list[str]] | None = None
        self._form: ObservedForm | None = None
        self._text_length = 0

    def _link(self, raw: str, label: str, kind: Any, locator: str) -> None:
        try:
            url = urljoin(self.base_url, raw)
            scheme = urlsplit(url).scheme
        except ValueError:
            self.truncated.add("invalid_links")
            return
        if len(self.links) >= 300:
            self.truncated.add("links")
        elif raw and len(url) <= 2_048 and scheme in {"http", "https", "mailto", "tel"}:
            if len(label) > 2_000:
                self.truncated.add("link_text")
            self.links.append(SourceLink(url=url, text=label[:2_000], kind=kind, locator=locator))

    def _flush_block(self) -> None:
        if self._block is None:
            return
        tag, locator, parts = self._block
        text = " ".join(" ".join(parts).split())
        if text:
            if len(self.blocks) >= 200:
                self.truncated.add("inventory")
            else:
                if len(text) > 8_000:
                    self.truncated.add("inventory_text")
                self.blocks.append(
                    ("heading" if tag.startswith("h") else "text", locator, text[:8_000])
                )
        self._block = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = {key.lower(): value or "" for key, value in attrs}
        self._counts[tag] = self._counts.get(tag, 0) + 1
        locator = f"{tag}[{self._counts[tag]}]"
        if tag == "html":
            self.language = fields.get("lang", "")[:100]
        if tag == "head":
            self._head = True
        if tag == "title":
            self._title = True
        if tag in {"script", "style", "noscript"}:
            self._hidden.append(tag)
            if tag == "script" and fields.get("type", "").lower() == "application/ld+json":
                self._json = True
                self._json_parts = []
            return
        if self._hidden:
            return
        if tag == "base" and "href" in fields and not self._base_seen:
            self._base_seen = True
            try:
                base_url = urljoin(self.url, fields["href"])
                if len(base_url) > 2_048 or urlsplit(base_url).scheme not in {"http", "https"}:
                    raise ValueError("invalid_base")
                self.base_url = base_url
            except ValueError:
                self.truncated.add("invalid_links")
        if tag == "meta":
            name = fields.get("name") or fields.get("property")
            if name and fields.get("content"):
                if len(self.metadata) >= 50:
                    self.truncated.add("metadata")
                else:
                    if len(name) > 100 or len(fields["content"]) > 2_000:
                        self.truncated.add("metadata")
                    self.metadata[name[:100]] = fields["content"][:2_000]
        if tag == "link" and fields.get("rel") == "canonical":
            self.metadata["canonical"] = urljoin(self.base_url, fields.get("href", ""))[:2_000]
        if self._head:
            return
        if tag in self.BLOCKS:
            self._flush_block()
            self._block = tag, locator, []
        if tag == "a":
            self._anchor = fields.get("href", ""), locator, []
        if tag == "img":
            self._link(fields.get("src", ""), fields.get("alt", ""), "image", locator)
        if tag == "form":
            if len(self.forms) < 30:
                self._form = ObservedForm(
                    action=(
                        urljoin(self.base_url, fields["action"])
                        if fields.get("action")
                        else self.url
                    )[:2_048],
                    method=fields.get("method", "get").lower()[:20],
                    field_names=[],
                )
                self.forms.append(self._form)
            else:
                self.truncated.add("forms")
        if tag in {"input", "select", "textarea"} and self._form and fields.get("name"):
            if len(self._form.field_names) < 100:
                self._form.field_names.append(fields["name"][:200])
            else:
                self.truncated.add("form_fields")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._json:
            raw = "".join(self._json_parts)
            if len(raw) <= 16_000 and len(self.structured_data) < 20:
                try:
                    value = json.loads(
                        raw,
                        parse_constant=lambda _: (_ for _ in ()).throw(
                            ValueError("invalid_constant")
                        ),
                    )
                    if isinstance(value, (dict, list)):
                        self.structured_data.append(value)
                except (ValueError, RecursionError):
                    self.truncated.add("invalid_structured_data")
            else:
                self.truncated.add("structured_data")
            self._json = False
        if tag in self._hidden:
            self._hidden.remove(tag)
        if tag == "head":
            self._head = False
        if tag == "title":
            self._title = False
        if self._hidden:
            return
        if self._block and self._block[0] == tag:
            self._flush_block()
        if tag == "a" and self._anchor:
            href, locator, parts = self._anchor
            try:
                parsed = urlsplit(href)
                kind = (
                    "contact"
                    if parsed.scheme in {"mailto", "tel"}
                    else (
                        "document"
                        if parsed.path.lower().endswith(_DOCUMENT_SUFFIXES)
                        else "navigation"
                    )
                )
                self._link(href, " ".join(" ".join(parts).split()), kind, locator)
            except ValueError:
                self.truncated.add("invalid_links")
            self._anchor = None
        if tag == "form":
            self._form = None

    def handle_data(self, data: str) -> None:
        if self._json:
            self._json_parts.append(data)
        if self._hidden:
            return
        if self._title:
            self.title += data
        if self._head:
            return
        value = " ".join(data.split())
        if not value:
            return
        available = self.max_text - self._text_length
        if len(value) + 1 > available:
            self.truncated.add("text")
        value = value[: max(0, available - 1)]
        if value:
            self.text_parts.append(value)
            self._text_length += len(value) + 1
            if self._block:
                self._block[2].append(value)
            elif len(self.blocks) < 200:
                line, offset = self.getpos()
                self.blocks.append(("text", f"text@{line}:{offset}", value[:8_000]))
                if len(value) > 8_000:
                    self.truncated.add("inventory_text")
            else:
                self.truncated.add("inventory")
            if self._anchor:
                self._anchor[2].append(value)


def _parse_page(
    response: FetchResponse, requested_url: str, limits: CaptureLimits
) -> tuple[CapturedPage, list[SourceInventoryItem]]:
    content_type = response.headers.get("content-type", "").lower()
    charset = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9._-]+)", content_type)
    encoding = charset.group(1) if charset else "utf-8"
    encoding_error = False
    try:
        html = response.body.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError):
        html = response.body.decode("utf-8", errors="replace")
        encoding_error = True
    parser = _PageParser(response.url, limits.max_text_characters)
    parser.feed(html)
    parser.close()
    parser._flush_block()
    if encoding_error:
        parser.truncated.add("encoding")
    if len(parser.title) > 1_000:
        parser.truncated.add("title")
    digest = hashlib.sha256(response.body).hexdigest()
    page = CapturedPage(
        requested_url=requested_url,
        final_url=response.url,
        fetched_at=_utc(),
        http_status=response.status,
        html_sha256=digest,
        response_bytes=len(response.body),
        title=parser.title.strip()[:1_000],
        language=parser.language,
        text="\n".join(parser.text_parts),
        headings=[text[:2_000] for kind, _, text in parser.blocks if kind == "heading"],
        metadata=parser.metadata,
        structured_data=parser.structured_data,
        links=parser.links,
        forms=parser.forms,
        truncated_fields=sorted(parser.truncated),
    )
    items = [
        SourceInventoryItem(
            id="source_"
            + hashlib.sha256(f"{response.url}\0{digest}\0{locator}\0{kind}".encode()).hexdigest()[
                :24
            ],
            source_url=response.url,
            page_sha256=digest,
            source_locator=locator,
            kind=cast(Any, kind),
            text=text,
            original_url=url,
        )
        for kind, locator, text, url in (
            [(kind, locator, text, None) for kind, locator, text in parser.blocks]
            + [(link.kind, link.locator, link.text, link.url) for link in parser.links]
        )
    ]
    return page, items


def capture_website(
    url: str, *, fetcher: WebsiteFetcher | None = None, limits: CaptureLimits | None = None
) -> WebsiteDossier:
    """Capture source HTML into a dossier. This does not render, publish or infer facts."""
    source = normalize_public_url(url)
    limits = limits or CaptureLimits()
    budget = _Budget(fetcher or PublicHttpFetcher(), limits, source)
    entries: dict[str, CoverageEntry] = {
        source: CoverageEntry(url=source, discovered_from=None, state="not_visited")
    }
    pending: deque[str] = deque([source])
    pages: list[CapturedPage] = []
    inventory: list[SourceInventoryItem] = []
    discovery_limited = False
    discovery_issues: list[DiscoveryIssue] = []
    visited = 0

    def discover(raw: str, origin: str, *, asset: bool = False) -> None:
        nonlocal discovery_limited
        try:
            target = normalize_public_url(urljoin(origin, raw))
        except ValueError:
            return
        if target in entries:
            return
        if len(entries) >= limits.max_discovered_urls:
            discovery_limited = True
            return
        reason = (
            "asset_reference"
            if asset
            else (
                "outside_origin"
                if not _same_site(target, source)
                else ("tls_downgrade" if _tls_downgrade(target, origin) else None)
            )
        )
        entries[target] = CoverageEntry(
            url=target,
            discovered_from=origin,
            state="excluded" if reason else "not_visited",
            reason=reason,
        )
        if reason is None:
            pending.append(target)

    robots_url = urljoin(source, "/robots.txt")
    robot = RobotFileParser(robots_url)
    try:
        response = budget.fetch(robots_url)
        if response.status == 404:
            robot.parse([])
        elif response.status == 200:
            robot.parse(response.body.decode("utf-8", errors="replace").splitlines())
        else:
            raise CaptureError("robots_unavailable")
    except (CaptureError, ValueError):
        entries[source].state = "blocked"
        entries[source].reason = "robots_unavailable"
        pending.clear()
    else:
        candidates = robot.site_maps() or [urljoin(source, "/sitemap.xml")]
        sitemaps = deque(candidates[: limits.max_sitemaps])
        if len(candidates) > limits.max_sitemaps:
            discovery_limited = True
        seen_sitemaps: set[str] = set()
        while sitemaps and len(seen_sitemaps) < limits.max_sitemaps:
            candidate = sitemaps.popleft()
            sitemap = robots_url
            try:
                sitemap = normalize_public_url(candidate)
                if sitemap in seen_sitemaps or not _same_site(sitemap, source):
                    continue
                seen_sitemaps.add(sitemap)
                response = budget.fetch(sitemap)
                if response.status == 404:
                    continue
                if response.status != 200:
                    discovery_issues.append(DiscoveryIssue(url=sitemap, reason="http_status"))
                    continue
                xml_markers = response.body.replace(b"\0", b"").upper()
                if b"<!DOCTYPE" in xml_markers or b"<!ENTITY" in xml_markers:
                    discovery_issues.append(DiscoveryIssue(url=sitemap, reason="unsafe_sitemap"))
                    continue
                root = ElementTree.fromstring(response.body)
                is_index = root.tag.rsplit("}", 1)[-1] == "sitemapindex"
                for element in root.iter():
                    if element.tag.rsplit("}", 1)[-1] == "loc" and element.text:
                        if is_index:
                            if len(sitemaps) < limits.max_sitemaps:
                                sitemaps.append(element.text.strip())
                            else:
                                discovery_limited = True
                        else:
                            discover(element.text.strip(), response.url)
            except (CaptureError, ValueError, ElementTree.ParseError):
                discovery_issues.append(DiscoveryIssue(url=sitemap, reason="sitemap_unavailable"))
                continue
        if sitemaps:
            discovery_limited = True

    while pending and visited < limits.max_pages:
        target = pending.popleft()
        entry = entries[target]
        if not robot.can_fetch(USER_AGENT, target):
            entry.state, entry.reason = "blocked", "robots_disallowed"
            continue
        if time.monotonic() >= budget.deadline or budget.bytes >= limits.max_total_bytes:
            entry.reason = (
                "duration_limit" if time.monotonic() >= budget.deadline else "total_byte_limit"
            )
            break
        visited += 1
        try:
            response = budget.fetch(
                target, allowed=lambda address: robot.can_fetch(USER_AGENT, address)
            )
            entry.final_url, entry.http_status = response.url, response.status
            if any(page.final_url == response.url for page in pages):
                entry.state, entry.reason = "excluded", "redirect_duplicate"
                continue
            if response.status != 200:
                entry.state, entry.reason = "failed", "http_status"
                continue
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                entry.state, entry.reason = "excluded", "not_html"
                continue
            page, items = _parse_page(response, target, limits)
            pages.append(page)
            inventory.extend(items)
            entry.state = "extracted"
            for link in page.links:
                if link.kind != "contact":
                    discover(link.url, response.url, asset=link.kind in {"image", "document"})
        except (CaptureError, ValueError) as exc:
            entry.state = (
                "blocked"
                if str(exc)
                in {
                    "outside_origin",
                    "unsafe_redirect",
                    "non_public_address",
                    "robots_disallowed",
                    "tls_downgrade",
                }
                else "failed"
            )
            entry.reason = str(exc) if isinstance(exc, CaptureError) else "invalid_source"
    for entry in entries.values():
        if entry.state == "not_visited" and entry.reason is None:
            entry.reason = "page_limit" if visited >= limits.max_pages else "capture_limit"
    states = [entry.state for entry in entries.values()]
    partial = (
        bool(discovery_issues)
        or discovery_limited
        or any(state in {"blocked", "failed", "not_visited"} for state in states)
        or any(entry.reason == "tls_downgrade" for entry in entries.values())
        or any(page.truncated_fields for page in pages)
    )
    return WebsiteDossier(
        source_url=source,
        captured_at=_utc(),
        pages=pages,
        inventory=inventory,
        coverage=DossierCoverage(
            status="partial" if partial else "bounded_scope_exhausted",
            entries=list(entries.values()),
            discovered=len(entries),
            visited=visited,
            extracted=states.count("extracted"),
            blocked=states.count("blocked"),
            failed=states.count("failed"),
            excluded=states.count("excluded"),
            not_visited=states.count("not_visited"),
            discovery_limited=discovery_limited,
            discovery_issues=discovery_issues,
            response_bytes=budget.bytes,
            elapsed_ms=int((time.monotonic() - budget.started) * 1_000),
            limits=limits,
        ),
    )
