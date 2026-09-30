"""Versioned research collection receipts; successful fetches are not semantic proof."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.writing_contracts import checked_research_url

RESEARCH_COLLECT_SKILL = "research.collect"
MAX_COLLECT_RESULT_BYTES = 524_288


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResearchCollectPayload(_StrictModel):
    focus: str = Field(min_length=1, max_length=2_000)
    queries: list[str] = Field(min_length=1, max_length=4)
    max_results_per_query: int = Field(default=3, ge=1, le=5)
    max_pages: int = Field(default=4, ge=1, le=6)
    source_urls: list[str] = Field(default_factory=list, max_length=6)
    required_domains: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("source_urls")
    @classmethod
    def checked_source_urls(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate source URLs")
        for url in value:
            checked_research_url(url)
            parsed = urlsplit(url)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
            if parsed.scheme != "https" or len(host) > 253 or host.endswith(".") or "\\" in url:
                raise ValueError("source URLs must be canonical public HTTPS URLs")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if all(c in "0123456789." for c in host) or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part)
                    for part in host.split(".")
                ):
                    raise ValueError("invalid source hostname") from None
            else:
                if (
                    not address.is_global
                    or address.is_multicast
                    or address.is_reserved
                    or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped)
                ):
                    raise ValueError("source URL must identify a public address")
                host = f"[{address.compressed}]" if address.version == 6 else address.compressed
            canonical = urlunsplit(
                (
                    "https",
                    host,
                    quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                    quote(parsed.query, safe="%:@!$&'()*+,;=/?-._~"),
                    "",
                )
            )
            if url != canonical:
                raise ValueError("source URLs must be canonical public HTTPS URLs")
        return value

    @field_validator("required_domains")
    @classmethod
    def checked_domains(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            len(domain) > 253
            or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", domain
            )
            for domain in value
        ):
            raise ValueError("required domains must be distinct lowercase hostnames")
        return value

    @field_validator("focus")
    @classmethod
    def checked_focus(cls, value: str) -> str:
        if not value.strip() or "\0" in value:
            raise ValueError("invalid research focus")
        value.encode("utf-8")
        return value.strip()

    @field_validator("queries")
    @classmethod
    def checked_queries(cls, value: list[str]) -> list[str]:
        for item in value:
            item.encode("utf-8")
        result = [item.strip() for item in value]
        if any(not 1 <= len(item) <= 2_000 or "\0" in item for item in result):
            raise ValueError("invalid research query")
        if len({item.casefold() for item in result}) != len(result):
            raise ValueError("research queries must be distinct")
        return result


def research_collect_payload_schema() -> dict[str, Any]:
    return ResearchCollectPayload.model_json_schema()


def _url(value: str) -> str:
    # Collection receipts may retain a failed result URL up to2048 chars; citation
    # projection later applies the smaller1000-character writing contract.
    if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("invalid research URL")
    value.encode("utf-8")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("invalid research URL")
    _ = parsed.port
    return value


def _date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid research timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("research timestamp needs an offset")
    return value


class ResearchSearchReceipt(_StrictModel):
    query: str = Field(min_length=1, max_length=2_000)
    status: Literal["completed", "failed"]
    searched_at: str = Field(max_length=64)
    result_urls: list[str] = Field(max_length=5)
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z_]{0,63}$")

    _check_date = field_validator("searched_at")(_date)

    @field_validator("result_urls")
    @classmethod
    def checked_urls(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate search URLs")
        return [_url(item) for item in value]

    @model_validator(mode="after")
    def status_consistent(self) -> ResearchSearchReceipt:
        if self.status == "failed" and (not self.error_code or self.result_urls):
            raise ValueError("failed search has invalid evidence")
        if self.status == "completed" and self.error_code is not None:
            raise ValueError("completed search has an error")
        return self


class ResearchSearchResult(_StrictModel):
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2_048)
    snippet: str = Field(max_length=4_000)
    query_indices: list[int] = Field(max_length=4)

    _check_url = field_validator("url")(_url)

    @field_validator("query_indices")
    @classmethod
    def indices(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(not 0 <= index <= 3 for index in value):
            raise ValueError("invalid query indices")
        return value


class ResearchPageReceipt(_StrictModel):
    requested_url: str = Field(min_length=1, max_length=2_048)
    status: Literal["read", "failed"]
    fetched_at: str = Field(max_length=64)
    final_url: str | None = Field(default=None, max_length=2_048)
    http_status: int | None = Field(default=None, ge=100, le=599)
    content_type: Literal["text/html", "application/xhtml+xml", "text/plain"] | None = None
    text: str | None = Field(default=None, min_length=1, max_length=12_000)
    content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    body_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    truncated: bool | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z_]{0,63}$")

    _check_url = field_validator("requested_url")(_url)
    _check_date = field_validator("fetched_at")(_date)

    @model_validator(mode="after")
    def consistent_page(self) -> ResearchPageReceipt:
        if self.final_url is not None:
            _url(self.final_url)
        if self.status == "read":
            if (
                not self.final_url
                or self.http_status != 200
                or self.content_type is None
                or not self.text
                or "\0" in self.text
                or self.content_sha256 is None
                or self.body_sha256 is None
                or self.truncated is None
                or self.error_code is not None
            ):
                raise ValueError("read page lacks evidence")
            # Re-check literal hosts and HTTPS. DNS attestations require the worker's
            # pinned fetcher; this parser cannot prove a past network operation.
            checked_research_url(
                urlsplit(self.final_url).scheme + "://" + urlsplit(self.final_url).netloc + "/"
            )
            if urlsplit(self.final_url).scheme != "https" or urlsplit(self.final_url).port not in {
                None,
                443,
            }:
                raise ValueError("read page must use public HTTPS")
            if hashlib.sha256(self.text.encode()).hexdigest() != self.content_sha256:
                raise ValueError("page text hash does not match")
        elif (
            not self.error_code
            or self.text is not None
            or self.content_sha256 is not None
            or self.body_sha256 is not None
            or self.truncated is not None
            or self.content_type is not None
        ):
            raise ValueError("failed page must not claim read evidence")
        return self


class ResearchCoverage(_StrictModel):
    assessment: Literal["source_presence_only"]
    required_domains: list[str] = Field(max_length=6)
    read_domains: list[str] = Field(max_length=6)
    missing_domains: list[str] = Field(max_length=6)
    queries_without_read_pages: list[int] = Field(max_length=4)


class ResearchCollectResult(_StrictModel):
    schema_version: Literal["1.0", "1.1"]
    content_trust: Literal["untrusted"]
    collection_status: Literal["complete", "partial", "no_evidence"]
    searches: list[ResearchSearchReceipt] = Field(min_length=1, max_length=4)
    results: list[ResearchSearchResult] = Field(max_length=26)
    pages: list[ResearchPageReceipt] = Field(max_length=6)
    source_urls: list[str] | None = Field(default=None, max_length=6)
    coverage: ResearchCoverage | None = None

    @model_validator(mode="after")
    def consistent_collection(self) -> ResearchCollectResult:
        if self.schema_version == "1.0":
            if self.source_urls is not None or self.coverage is not None or len(self.results) > 20:
                raise ValueError("legacy collection cannot claim explicit sources or coverage")
        elif self.source_urls is None or self.coverage is None:
            raise ValueError("collection lacks coverage receipt")
        direct_urls = self.source_urls or []
        ResearchCollectPayload.checked_source_urls(direct_urls)
        urls = [item.url for item in self.results]
        page_urls = [page.requested_url for page in self.pages]
        if len(set(urls)) != len(urls) or len(set(page_urls)) != len(page_urls):
            raise ValueError("duplicate collection sources")
        if not set(page_urls) <= set(urls):
            raise ValueError("page was not selected from this collection")
        for item in self.results:
            actual = [i for i, search in enumerate(self.searches) if item.url in search.result_urls]
            if sorted(item.query_indices) != actual:
                raise ValueError("source query provenance mismatch")
            if not actual and (item.snippet or item.title != item.url[:300]):
                raise ValueError("direct source cannot claim search metadata")
        if set(urls) != {url for search in self.searches for url in search.result_urls} | set(
            direct_urls
        ):
            raise ValueError("search result provenance mismatch")
        if self.coverage is not None:
            domains = ResearchCollectPayload.checked_domains(self.coverage.required_domains)
            read_urls = {page.requested_url for page in self.pages if page.status == "read"}
            read_domains = sorted(
                {
                    str(urlsplit(page.final_url or "").hostname)
                    for page in self.pages
                    if page.status == "read"
                }
            )
            missing = [
                domain
                for domain in domains
                if not any(host == domain or host.endswith("." + domain) for host in read_domains)
            ]
            unserved = [
                i
                for i, search in enumerate(self.searches)
                if not read_urls.intersection(search.result_urls)
            ]
            if (
                self.coverage.read_domains != read_domains
                or self.coverage.missing_domains != missing
                or self.coverage.queries_without_read_pages != unserved
            ):
                raise ValueError("collection coverage does not match page evidence")
        read_count = sum(page.status == "read" for page in self.pages)
        expected = (
            "complete"
            if self.pages
            and read_count == len(self.pages)
            and all(search.status == "completed" for search in self.searches)
            else "partial"
        )
        if not self.results and not read_count:
            expected = "no_evidence"
        if self.collection_status != expected:
            raise ValueError("collection status does not match its evidence")
        return self


def validate_research_collect_result(value: Any) -> dict[str, Any]:
    encoded = json.dumps(value, allow_nan=False, ensure_ascii=False).encode()
    if len(encoded) > MAX_COLLECT_RESULT_BYTES:
        raise ValueError("research collection result exceeds its bound")
    return ResearchCollectResult.model_validate(value).model_dump(exclude_none=True)


def valid_research_collect_result(value: Any) -> bool:
    try:
        validate_research_collect_result(value)
    except (ValueError, TypeError, UnicodeError):
        return False
    return True


def valid_research_collect_receipt(value: Any, payload: Any) -> bool:
    """Check bounded execution evidence against the exact admitted request."""
    try:
        request = ResearchCollectPayload.model_validate(payload)
        result = validate_research_collect_result(value)
        if [search["query"] for search in result["searches"]] != request.queries:
            return False
        if any(
            len(search["result_urls"]) > request.max_results_per_query
            for search in result["searches"]
        ):
            return False
        if result.get("source_urls", []) != request.source_urls:
            return False
        if result.get("coverage", {}).get("required_domains", []) != request.required_domains:
            return False
        expected: list[str] = list(request.source_urls)
        for rank in range(request.max_results_per_query):
            for search in result["searches"]:
                urls = search["result_urls"]
                if rank < len(urls) and urls[rank] not in expected:
                    expected.append(urls[rank])
        return [page["requested_url"] for page in result["pages"]] == expected[: request.max_pages]
    except (ValueError, TypeError, UnicodeError):
        return False


def project_research_collect_sources(value: Any, worker_job_id: str) -> list[dict[str, Any]]:
    """Project real page excerpts first, preserving exact URL and hash identities."""
    result = validate_research_collect_result(value)
    by_url = {item["url"]: item for item in result["results"]}
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in result["pages"]:
        if page["status"] != "read":
            continue
        url = page["final_url"]
        try:
            checked_research_url(url)
            checked_research_url(page["requested_url"])
        except ValueError:
            continue
        if url in seen:
            continue
        item = by_url[page["requested_url"]]
        excerpt = page["text"][:4_000]
        sources.append(
            {
                "content_trust": "untrusted",
                "worker_job_id": worker_job_id,
                "title": item["title"][:240],
                "url": url,
                "snippet": item["snippet"][:700],
                "evidence": {
                    "kind": "page_excerpt",
                    "requested_url": page["requested_url"],
                    "final_url": url,
                    "fetched_at": page["fetched_at"],
                    "content_sha256": page["content_sha256"],
                    "body_sha256": page["body_sha256"],
                    "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                    "text": excerpt,
                    "truncated": page["truncated"] or len(excerpt) < len(page["text"]),
                },
            }
        )
        seen.add(url)
        seen.add(page["requested_url"])
    ordered_urls: list[str] = []
    for rank in range(5):
        for search in result["searches"]:
            urls = search["result_urls"]
            if rank < len(urls) and urls[rank] not in ordered_urls:
                ordered_urls.append(urls[rank])
    for source_url in ordered_urls:
        item = by_url[source_url]
        if len(sources) >= 6:
            break
        try:
            checked_research_url(item["url"])
        except ValueError:
            continue
        if item["url"] in seen:
            continue
        sources.append(
            {
                "content_trust": "untrusted",
                "worker_job_id": worker_job_id,
                "title": item["title"][:240],
                "url": item["url"],
                "snippet": item["snippet"][:700],
            }
        )
        seen.add(item["url"])
    return sources[:6]
