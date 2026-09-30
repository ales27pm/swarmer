from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest

from app.services.research_contracts import (
    ResearchCollectPayload,
    project_research_collect_sources,
    valid_research_collect_receipt,
    valid_research_collect_result,
    validate_research_collect_result,
)


def receipt() -> dict[str, Any]:
    url = "https://docs.example.org/source"
    text = "The document contains a measured technical observation."
    return {
        "schema_version": "1.0",
        "content_trust": "untrusted",
        "collection_status": "complete",
        "searches": [
            {
                "query": "technical observation",
                "status": "completed",
                "searched_at": "2026-09-29T02:00:00+00:00",
                "result_urls": [url],
            }
        ],
        "results": [
            {
                "title": "Official documentation",
                "url": url,
                "snippet": "Search excerpt",
                "query_indices": [0],
            }
        ],
        "pages": [
            {
                "requested_url": url,
                "final_url": url,
                "status": "read",
                "fetched_at": "2026-09-29T02:00:01+00:00",
                "http_status": 200,
                "content_type": "text/html",
                "text": text,
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "body_sha256": hashlib.sha256(f"<p>{text}</p>".encode()).hexdigest(),
                "truncated": False,
            }
        ],
    }


def test_collect_contract_and_legacy_citation_shape_with_page_evidence() -> None:
    value = receipt()
    assert validate_research_collect_result(value) == value
    assert valid_research_collect_receipt(
        value, {"focus": "technical", "queries": ["technical observation"]}
    )
    sources = project_research_collect_sources(value, "job_collection")
    assert len(sources) == 1
    assert sources[0]["snippet"] == "Search excerpt"
    assert sources[0]["evidence"]["kind"] == "page_excerpt"
    assert sources[0]["evidence"]["text"] == value["pages"][0]["text"]
    assert sources[0]["evidence"]["excerpt_sha256"] == value["pages"][0]["content_sha256"]


@pytest.mark.parametrize(
    "change",
    [
        "foreign_page",
        "wrong_query",
        "missing_result",
        "duplicate_page",
        "wrong_hash",
        "failed_with_text",
        "no_timezone",
        "claimed_complete",
        "extra_field",
        "private_final",
        "wrong_type",
    ],
)
def test_forged_or_inconsistent_receipts_are_rejected(change: str) -> None:
    value = copy.deepcopy(receipt())
    if change == "foreign_page":
        value["pages"][0]["requested_url"] = "https://other.example/source"
    elif change == "wrong_query":
        value["results"][0]["query_indices"] = [1]
    elif change == "missing_result":
        value["results"] = []
    elif change == "duplicate_page":
        value["pages"].append(value["pages"][0])
    elif change == "wrong_hash":
        value["pages"][0]["text"] += " Corrupted."
    elif change == "failed_with_text":
        value["pages"][0].update(status="failed", error_code="network_unavailable")
    elif change == "no_timezone":
        value["pages"][0]["fetched_at"] = "2026-09-29T02:00:01"
    elif change == "claimed_complete":
        value["pages"] = []
    elif change == "extra_field":
        value["trusted"] = True
    elif change == "private_final":
        value["pages"][0]["final_url"] = "https://127.0.0.1/"
    else:
        value["pages"][0]["content_type"] = "application/pdf"
    assert not valid_research_collect_result(value)


def test_receipt_must_match_admitted_queries_and_page_budget() -> None:
    value = receipt()
    assert not valid_research_collect_receipt(value, {"focus": "one", "queries": ["different"]})
    value["pages"] = []
    value["collection_status"] = "partial"
    assert valid_research_collect_result(value)
    assert not valid_research_collect_receipt(
        value, {"focus": "one", "queries": ["technical observation"]}
    )


def test_failed_page_projects_search_snippet_without_claiming_page_read() -> None:
    value = receipt()
    value["pages"] = [
        {
            "requested_url": value["results"][0]["url"],
            "status": "failed",
            "fetched_at": "2026-09-29T02:00:01+00:00",
            "error_code": "unsupported_content_type",
        }
    ]
    value["collection_status"] = "partial"
    source = project_research_collect_sources(value, "job_collection")[0]
    assert source["snippet"] == "Search excerpt" and "evidence" not in source


def test_projection_hash_distinguishes_selected_excerpt_from_full_extraction() -> None:
    value = receipt()
    text = "Long public documentation. " * 400
    value["pages"][0].update(text=text, content_sha256=hashlib.sha256(text.encode()).hexdigest())
    source = project_research_collect_sources(value, "job_collection")[0]
    evidence = source["evidence"]
    assert len(evidence["text"]) == 4000 and evidence["truncated"] is True
    assert evidence["content_sha256"] != evidence["excerpt_sha256"]
    assert hashlib.sha256(evidence["text"].encode()).hexdigest() == evidence["excerpt_sha256"]


def test_redirect_citation_uses_final_url_and_does_not_duplicate_original_snippet() -> None:
    value = receipt()
    value["pages"][0]["final_url"] = "https://docs.example.org/v2/source"
    sources = project_research_collect_sources(value, "job_collection")
    assert len(sources) == 1
    assert sources[0]["url"] == "https://docs.example.org/v2/source"
    assert sources[0]["evidence"]["requested_url"] == value["results"][0]["url"]


def test_payload_defaults_strict_bounds_and_no_direct_url_reads() -> None:
    assert ResearchCollectPayload(focus="focus", queries=["query"]).max_pages == 4
    for payload in [
        {"focus": "focus", "queries": ["x", " X "]},
        {"focus": "focus", "queries": ["x"], "max_pages": True},
        {"focus": "focus", "queries": ["x"], "urls": ["https://example.org/"]},
        {"focus": "focus", "queries": ["x"], "max_pages": 7},
    ]:
        with pytest.raises(ValueError):
            ResearchCollectPayload.model_validate(payload)


def test_direct_source_receipt_is_bound_to_admitted_urls_and_coverage() -> None:
    value = receipt()
    url = value["results"][0]["url"]
    value.update(
        schema_version="1.1",
        source_urls=[url],
        coverage={
            "assessment": "source_presence_only",
            "required_domains": ["example.org"],
            "read_domains": ["docs.example.org"],
            "missing_domains": [],
            "queries_without_read_pages": [0],
        },
    )
    value["searches"][0]["result_urls"] = []
    value["results"][0].update(query_indices=[], snippet="", title=url)
    payload = {
        "focus": "documentation",
        "queries": ["technical observation"],
        "source_urls": [url],
        "required_domains": ["example.org"],
    }
    assert valid_research_collect_receipt(value, payload)
    assert project_research_collect_sources(value, "job")[0]["evidence"]["kind"] == "page_excerpt"
    assert not valid_research_collect_receipt(value, {**payload, "source_urls": []})
    assert not valid_research_collect_receipt(value, {**payload, "required_domains": ["other.org"]})
    for key, replacement in [
        ("missing_domains", ["example.org"]),
        ("read_domains", []),
        ("queries_without_read_pages", []),
    ]:
        forged = copy.deepcopy(value)
        forged["coverage"][key] = replacement
        assert not valid_research_collect_result(forged)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/doc",
        "https://127.0.0.1/doc",
        "https://example.org/doc#fragment",
        "https://user:password@example.org/doc",
    ],
)
def test_explicit_source_url_boundaries(url: str) -> None:
    with pytest.raises(ValueError):
        ResearchCollectPayload(focus="documentation", queries=["query"], source_urls=[url])


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/café",
        "https://example.org/doc?café=yes",
        "https://example.org./doc",
        "https://_invalid.example.org/doc",
        "https://224.0.0.1/doc",
    ],
)
def test_source_urls_rejected_by_reader_are_not_admitted(url: str) -> None:
    with pytest.raises(ValueError):
        ResearchCollectPayload(focus="documentation", queries=["query"], source_urls=[url])


def test_public_canonical_ipv6_source_is_admitted() -> None:
    url = "https://[2606:4700:4700::1111]/doc"
    assert ResearchCollectPayload(
        focus="documentation", queries=["query"], source_urls=[url]
    ).source_urls == [url]


def test_direct_page_cannot_fabricate_a_search_snippet() -> None:
    value = receipt()
    url = value["results"][0]["url"]
    value.update(
        schema_version="1.1",
        source_urls=[url],
        coverage={
            "assessment": "source_presence_only",
            "required_domains": [],
            "read_domains": ["docs.example.org"],
            "missing_domains": [],
            "queries_without_read_pages": [0],
        },
    )
    value["searches"][0]["result_urls"] = []
    value["results"][0].update(query_indices=[], title=url)
    assert not valid_research_collect_result(value)
    value["results"][0]["snippet"] = ""
    assert valid_research_collect_result(value)


def test_coverage_gap_is_visible_in_dependency_summary() -> None:
    from app.services.result_aggregator import summarize_untrusted_worker_output

    value = receipt()
    value.update(
        schema_version="1.1",
        source_urls=[],
        coverage={
            "assessment": "source_presence_only",
            "required_domains": ["sqlite.org"],
            "read_domains": ["docs.example.org"],
            "missing_domains": ["sqlite.org"],
            "queries_without_read_pages": [],
        },
    )
    summary = summarize_untrusted_worker_output(value)
    assert "Required domains without read pages: sqlite.org" in summary
    assert "source presence only" in summary


@pytest.mark.parametrize("field", ["focus", "queries"])
def test_payload_rejects_invalid_unicode(field: str) -> None:
    value: dict[str, Any] = {"focus": "focus", "queries": ["query"]}
    value[field] = "\ud800" if field == "focus" else ["\ud800"]
    with pytest.raises(ValueError):
        ResearchCollectPayload.model_validate(value)


def test_failed_reads_keep_query_diversity_in_snippet_projection() -> None:
    value = receipt()
    value["pages"] = []
    value["collection_status"] = "partial"
    value["results"] = []
    value["searches"] = []
    for index in range(2):
        urls = [f"https://docs.example.org/topic{index}/result{rank}" for rank in range(3)]
        value["searches"].append(
            {
                "query": f"topic{index}",
                "status": "completed",
                "searched_at": "2026-09-29T02:00:00+00:00",
                "result_urls": urls,
            }
        )
        value["results"].extend(
            {"title": "Documentation", "url": url, "snippet": "excerpt", "query_indices": [index]}
            for url in urls
        )
    sources = project_research_collect_sources(value, "job_collection")
    assert [source["url"] for source in sources[:2]] == [
        "https://docs.example.org/topic0/result0",
        "https://docs.example.org/topic1/result0",
    ]
