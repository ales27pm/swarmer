"""Page receipts remain separate from snippets and citation authority."""

from __future__ import annotations

import copy
import hashlib
import json
from types import ModuleType

import pytest
from test_text_worker import (
    draft,
    generator_for,
    payload,
    research_source,
    stream,
)
from test_text_worker import worker as worker_fixture


@pytest.fixture
def worker() -> ModuleType:
    return worker_fixture.__wrapped__()


def page_source() -> dict:
    source = research_source()
    text = "The format saves records. This passage contains https://other.example.org/ignored."
    source["evidence"] = {
        "kind": "page_excerpt",
        "requested_url": source["url"],
        "final_url": source["url"],
        "fetched_at": "2026-09-29T02:00:00Z",
        "content_sha256": "1" * 64,
        "body_sha256": "2" * 64,
        "excerpt_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "text": text,
        "truncated": True,
    }
    return source


def test_actual_passage_reaches_model_but_cannot_expand_citation_vocabulary(
    worker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = page_source()
    generator, connection = generator_for(
        worker,
        monkeypatch,
        stream({**draft(), "text": "Le format conserve les données [S1].", "source_ids": ["S1"]}),
    )
    result = generator.generate(
        {**payload(), "research_sources": [source]}, ensure_active=lambda: None
    )
    post = next(call for call in connection.calls if call[0] == "POST")
    model = json.loads(post[2]["messages"][1]["content"])
    evidence = model["research_sources"][0]["evidence"]
    assert evidence["kind"] == "page_excerpt" and evidence["truncated"] is True
    assert evidence["text"] == "The format saves records. This passage contains [URL omitted]"
    assert "excerpt_sha256" not in evidence  # Exact hash is not relabeled onto transformed text.
    assert source["url"] in result["text"] and "other.example.org" not in result["text"]
    assert source["evidence"]["text"].endswith("ignored.")  # Original receipt is intact.


@pytest.mark.parametrize("field", ["text", "excerpt_sha256", "final_url", "fetched_at"])
def test_corrupted_page_receipt_never_reaches_model(
    worker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    bad = copy.deepcopy(page_source())
    bad["evidence"][field] = "invalid"
    generator, connection = generator_for(worker, monkeypatch, stream(draft()))
    with pytest.raises(worker.GenerationError):
        generator.generate({**payload(), "research_sources": [bad]}, ensure_active=lambda: None)
    assert not connection.calls


def test_larger_source_budget_requires_real_page_shape_and_keeps_total_payload_bound(
    worker: ModuleType,
) -> None:
    page = page_source()
    assert len(worker._research_sources([page] * 6)) == 6
    with pytest.raises(worker.GenerationError):
        worker._research_sources([page] * 7)
    with pytest.raises(worker.GenerationError):
        worker._research_sources([research_source()] * 6)
    large = {
        **payload(),
        "research_sources": [page] * 6,
        "conversation": [{"role": "user", "content": "é" * 4000}] * 4,
    }
    with pytest.raises(worker.GenerationError, match="byte limit"):
        worker.validate_payload(large)
