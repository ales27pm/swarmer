"""Bounded evidence collection; search snippets and fetched excerpts remain distinct."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

COLLECT_SKILL = "research.collect"
MAX_PAGE_BYTES = 1_048_576
MAX_PAGE_CHARACTERS = 4_000  # Also the writer handoff bound; select before that boundary.
MAX_COLLECTION_SECONDS = 120
MAX_PAGE_SECONDS = 15
MAX_REDIRECTS = 3


class CollectionError(ValueError):
    """A public bounded reason code; response bodies never become error messages."""


class RawPage:
    def __init__(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_collect_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {
        "focus",
        "queries",
        "max_results_per_query",
        "max_pages",
        "source_urls",
        "required_domains",
    }:
        raise ValueError("research collection payload has unsupported fields")
    focus, queries = value.get("focus"), value.get("queries")
    if not isinstance(focus, str) or not 1 <= len(focus.strip()) <= 2_000 or "\0" in focus:
        raise ValueError("research focus must contain 1 to 2000 characters")
    if not isinstance(queries, list) or not 1 <= len(queries) <= 4:
        raise ValueError("research collection requires 1 to 4 complementary queries")
    focus.encode("utf-8")
    normalized: list[str] = []
    for query in queries:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2_000 or "\0" in query:
            raise ValueError("research query must contain 1 to 2000 characters")
        query.encode("utf-8")
        if query.strip().casefold() in {item.casefold() for item in normalized}:
            raise ValueError("research queries must be distinct")
        normalized.append(query.strip())
    result: dict[str, Any] = {"focus": focus.strip(), "queries": normalized}
    source_urls = value.get("source_urls", [])
    if not isinstance(source_urls, list) or len(source_urls) > 6:
        raise ValueError("source URLs must be a bounded list")
    if any(
        not isinstance(url, str) or len(url) > 1000 or public_page_url(url) != url
        for url in source_urls
    ):
        raise ValueError("source URLs must be canonical public HTTPS URLs")
    if len(set(source_urls)) != len(source_urls):
        raise ValueError("duplicate source URLs")
    domains = value.get("required_domains", [])
    if (
        not isinstance(domains, list)
        or len(domains) > 6
        or any(
            not isinstance(domain, str)
            or len(domain) > 253
            or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", domain
            )
            for domain in domains
        )
        or len(set(domains)) != len(domains)
    ):
        raise ValueError("required domains must be distinct lowercase hostnames")
    result.update(source_urls=source_urls, required_domains=domains)
    for field, default, maximum in (("max_results_per_query", 3, 5), ("max_pages", 4, 6)):
        number = value.get(field, default)
        if type(number) is not int or not 1 <= number <= maximum:
            raise ValueError(f"{field} is outside its bounds")
        result[field] = number
    return result


def public_page_url(value: str) -> str:
    """Only credential-free public HTTPS/443; DNS is separately checked at connection."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2_048
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        or "\\" in value
    ):
        raise CollectionError("invalid_url")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise CollectionError("invalid_url") from exc
    if (
        parsed.scheme != "https"
        or not host
        or len(host) > 253
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.netloc.endswith(":")
        or "%" in host
        or host.endswith((".", ".local", ".localhost", ".internal"))
        or host == "localhost"
    ):
        raise CollectionError("invalid_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            "." not in host
            or all(c in "0123456789." for c in host)
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part)
                for part in host.split(".")
            )
        ):
            raise CollectionError("invalid_url") from None
    else:
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped)
        ):
            raise CollectionError("non_public_address")
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    normalized = urlunsplit(
        (
            "https",
            host,
            quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
            quote(parsed.query, safe="%:@!$&'()*+,;=/?-._~"),
            "",
        )
    )
    if len(normalized) > 2_048:
        raise CollectionError("invalid_url")
    return normalized


class VisibleText(HTMLParser):
    """Extract text only: never execute scripts, follow links or fetch resources."""

    _HIDDEN: ClassVar[set[str]] = {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "head",
        "nav",
        "footer",
    }
    _BLOCK: ClassVar[set[str]] = {
        "p",
        "div",
        "section",
        "article",
        "main",
        "li",
        "br",
        "pre",
        "h1",
        "h2",
        "h3",
        "tr",
    }
    _VOID: ClassVar[set[str]] = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        inherited = self.stack[-1][1] if self.stack else False
        style = re.sub(r"\s+", "", attr.get("style") or "").lower()
        hidden = (
            inherited
            or tag in self._HIDDEN
            or "hidden" in attr
            or ((attr.get("aria-hidden") or "").lower() == "true")
            or "display:none" in style
            or "visibility:hidden" in style
        )
        if tag not in self._VOID:
            self.stack.append((tag, hidden))
        if tag in self._BLOCK and not hidden:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in self._BLOCK and not (self.stack and self.stack[-1][1]):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not (self.stack and self.stack[-1][1]):
            # HTML source wrapping is not a paragraph boundary. Preserve code
            # formatting inside pre, while keeping prose conditions together.
            self.parts.append(
                data if any(tag == "pre" for tag, _ in self.stack) else re.sub(r"\s+", " ", data)
            )


def extract_text(
    body: bytes,
    content_type: str,
    focus: str,
    *,
    check_active: Callable[[], None] = lambda: None,
) -> tuple[str, bool]:
    """Select actual document paragraphs; focus orders selection, never generates content."""
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime not in {"text/html", "application/xhtml+xml", "text/plain"}:
        raise CollectionError("unsupported_content_type")
    charset = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", content_type, re.IGNORECASE)
    encoding = charset.group(1).lower() if charset else "utf-8"
    if encoding not in {"utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "windows-1252"}:
        raise CollectionError("unsupported_charset")
    try:
        decoded = body.decode(encoding, errors="strict")
    except UnicodeError as exc:
        raise CollectionError("invalid_text_encoding") from exc
    if "\0" in decoded:
        raise CollectionError("invalid_text_encoding")
    if mime != "text/plain":
        parser = VisibleText()
        for offset in range(0, len(decoded), 4096):
            check_active()
            parser.feed(decoded[offset : offset + 4096])
        parser.close()
        decoded = "".join(parser.parts)
    check_active()
    lines = [" ".join(line.split()) for line in decoded.splitlines()]
    lines = [line for line in lines if line]
    complete = "\n".join(lines)
    if not complete:
        raise CollectionError("empty_text")
    if len(complete) <= MAX_PAGE_CHARACTERS:
        return complete, False
    # Subdivide very long paragraphs so relevant text near their end remains
    # reachable. Rank real passages first; never synthesize a conclusion. Source
    # order is intentionally not claimed for this bounded excerpt bundle.
    passages: list[str] = []
    for line in lines:
        while len(line) > 1_200:
            boundary = line.rfind(" ", 0, 1_200)
            boundary = boundary if boundary > 600 else 1_200
            passages.append(line[:boundary])
            line = line[boundary:].lstrip()
        if line:
            passages.append(line)
    tokens = {word.casefold() for word in re.findall(r"[\w-]{4,}", focus)}
    ranks = sorted(
        range(len(passages)),
        key=lambda i: (
            -len(tokens.intersection(re.findall(r"[\w-]{4,}", passages[i].casefold()))),
            i,
        ),
    )
    selected: list[str] = []
    selected_indices: set[int] = set()
    remaining = MAX_PAGE_CHARACTERS
    for index in ranks:
        check_active()
        if remaining < 1:
            break
        # Keep adjacent qualifications with a matching passage, then fill the
        # remaining budget. These are extracted windows, never generated facts.
        for neighbor in range(max(0, index - 1), min(len(passages), index + 2)):
            if neighbor in selected_indices or remaining < 1:
                continue
            text = passages[neighbor][:remaining]
            selected.append(text)
            selected_indices.add(neighbor)
            remaining -= len(text) + 1
    return "\n".join(selected), True


def read_page(
    requested_url: str,
    focus: str,
    fetch: Callable[[str, float], RawPage],
    deadline: float,
    check_active: Callable[[], None],
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "requested_url": requested_url,
        "status": "failed",
        "fetched_at": utc_now(),
    }
    try:
        current = public_page_url(requested_url)
        seen: set[str] = set()
        for _ in range(MAX_REDIRECTS + 1):
            check_active()
            if time.monotonic() >= deadline:
                raise CollectionError("deadline_exceeded")
            if current in seen:
                raise CollectionError("redirect_loop")
            seen.add(current)
            response = fetch(current, deadline)
            check_active()
            if 300 <= response.status < 400:
                location = response.headers.get("location")
                if not location:
                    raise CollectionError("invalid_redirect")
                current = public_page_url(urljoin(current, location))
                continue
            receipt["final_url"] = current
            receipt["http_status"] = response.status
            if response.status != 200:
                raise CollectionError("http_status")
            if len(response.body) > MAX_PAGE_BYTES:
                raise CollectionError("response_byte_limit")
            if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                raise CollectionError("unsupported_encoding")
            content_type = response.headers.get("content-type", "")

            def active_page() -> None:
                check_active()
                if time.monotonic() >= deadline:
                    raise CollectionError("deadline_exceeded")

            text, truncated = extract_text(
                response.body, content_type, focus, check_active=active_page
            )
            check_active()
            if time.monotonic() >= deadline:
                raise CollectionError("deadline_exceeded")
            receipt.update(
                {
                    "status": "read",
                    "content_type": content_type.split(";", 1)[0].strip().lower(),
                    "text": text,
                    "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "body_sha256": hashlib.sha256(response.body).hexdigest(),
                    "truncated": truncated,
                }
            )
            return receipt
        raise CollectionError("redirect_limit")
    except CollectionError as exc:
        receipt["error_code"] = str(exc)
        return receipt


def collect(
    payload: Any,
    query: Callable[[str, int], dict[str, Any]],
    fetch: Callable[[str, float], RawPage],
    *,
    check_active: Callable[[], None],
    deadline: float,
) -> dict[str, Any]:
    request = parse_collect_payload(payload)
    searches: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    for index, search in enumerate(request["queries"]):
        check_active()
        entry: dict[str, Any] = {
            "query": search,
            "status": "failed",
            "searched_at": utc_now(),
            "result_urls": [],
        }
        try:
            if time.monotonic() >= deadline:
                raise CollectionError("deadline_exceeded")
            found = query(search, request["max_results_per_query"])
            check_active()
            entries = found.get("results")
            if found.get("content_trust") != "untrusted" or not isinstance(entries, list):
                raise CollectionError("invalid_search_result")
            if len(entries) > request["max_results_per_query"]:
                raise CollectionError("invalid_search_result")
            for item in entries:
                if not isinstance(item, dict) or set(item) != {"title", "url", "snippet"}:
                    raise CollectionError("invalid_search_result")
                for key, maximum in (("title", 300), ("url", 2048), ("snippet", 4000)):
                    if not isinstance(item[key], str) or len(item[key]) > maximum:
                        raise CollectionError("invalid_search_result")
                if not item["title"].strip() or not item["url"].strip():
                    raise CollectionError("invalid_search_result")
            for item in entries:
                url = item["url"]
                if url not in entry["result_urls"]:
                    entry["result_urls"].append(url)
                if url not in results:
                    results[url] = {**item, "query_indices": []}
                if index not in results[url]["query_indices"]:
                    results[url]["query_indices"].append(index)
            entry["status"] = "completed"
        except CollectionError as exc:
            entry["error_code"] = str(exc)
        searches.append(entry)
    # Interleave query result rankings so one query cannot monopolize the page budget.
    selected: list[str] = list(request["source_urls"])
    for url in request["source_urls"]:
        if url not in results:
            results[url] = {"title": url[:300], "url": url, "snippet": "", "query_indices": []}
    for rank in range(request["max_results_per_query"]):
        for search in searches:
            urls = search["result_urls"]
            if rank < len(urls) and urls[rank] not in selected:
                selected.append(urls[rank])
    pages = [
        read_page(
            url,
            request["focus"]
            + " "
            + " ".join(request["queries"][i] for i in results[url]["query_indices"]),
            fetch,
            min(deadline, time.monotonic() + MAX_PAGE_SECONDS),
            check_active,
        )
        for url in selected[: request["max_pages"]]
    ]
    read_count = sum(page["status"] == "read" for page in pages)
    status = (
        "complete"
        if pages
        and read_count == len(pages)
        and all(search["status"] == "completed" for search in searches)
        else "partial"
    )
    if not results and not read_count:
        status = "no_evidence"
    check_active()
    read_urls = {page["requested_url"] for page in pages if page["status"] == "read"}
    read_domains = sorted(
        {urlsplit(page["final_url"]).hostname for page in pages if page["status"] == "read"}
    )
    return {
        "schema_version": "1.1",
        "content_trust": "untrusted",
        "collection_status": status,
        "searches": searches,
        "results": list(results.values()),
        "pages": pages,
        "source_urls": request["source_urls"],
        "coverage": {
            "assessment": "source_presence_only",
            "required_domains": request["required_domains"],
            "read_domains": read_domains,
            "missing_domains": [
                domain
                for domain in request["required_domains"]
                if not any(host == domain or host.endswith("." + domain) for host in read_domains)
            ],
            "queries_without_read_pages": [
                i
                for i, search in enumerate(searches)
                if not read_urls.intersection(search["result_urls"])
            ],
        },
    }
