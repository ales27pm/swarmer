"""Exact original display is provenance-bound and never a second translation."""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from app.models import MemoryCreate, MemorySearch
from app.services import memory_search_presentation as display
from app.services.memory_normalization import MemoryNormalizationError, canonical_text_sha256
from app.services.state_service import StateService
from tests.test_memory_query_normalization import FrenchPresenter, QueryNormalizer, row_counts

FRENCH = "Conserver exactement 30 ms et le fichier `rapport.csv`."
ENGLISH = "Keep exactly 30 ms and the file `rapport.csv`."
SUMMARY = "Le cache pourrait expliquer le délai."
ENGLISH_SUMMARY = "The cache might explain the delay."


class OriginalNormalizer(QueryNormalizer):
    async def normalize(self, source, *, recheck_source=None, model_executor=None):
        result = await super().normalize(source, recheck_source=recheck_source)
        if source.kind == "query":
            return result
        translations = {FRENCH: ENGLISH, SUMMARY: ENGLISH_SUMMARY}
        text = translations.get(source.text, source.text)
        return result.model_copy(
            update={
                "canonical_text": text,
                "canonical_sha256": canonical_text_sha256(text),
                "source_language": "fr" if source.text in translations else "en",
            }
        )


async def seeded(tmp_path, *, summary=None):
    state = StateService(
        tmp_path / "state.db",
        canonical_language="en",
        memory_normalizer=OriginalNormalizer(english="file"),
    )
    state.memory_presenter = FrenchPresenter()
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content=FRENCH, summary=summary), "fixture")
    return state, item


async def finalize(state, items, *, french=True):
    return await display.finalize_memory_search(
        state.db_path,
        items,
        french=french,
        get_presenter=lambda: state.memory_presenter,
        gate=state.memory_normalization_gate,
        timeout_seconds=2,
        assert_current=lambda: None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [None, SUMMARY])
async def test_exact_original_needs_no_presenter_and_does_not_write(tmp_path, summary):
    state, item = await seeded(tmp_path, summary=summary)
    state.memory_presenter = None
    before = await row_counts(state)
    result = (await state.search_memory(MemorySearch(query="le fichier")))[0]
    assert result["content"] == ENGLISH and result["summary"] == item["summary"]
    assert result["presentation"] == {
        "mode": "original",
        "language": "fr",
        "content": FRENCH,
        "summary": summary,
        "canonical_sha256": canonical_text_sha256(ENGLISH),
        "summary_sha256": canonical_text_sha256(item["summary"]) if summary else None,
        "source_revision": item["updated_at"],
        "validation_status": "source_preserved",
        "temporary": True,
        "grants_authority": False,
        "source_id": item["metadata"]["source_id"],
        "source_sha256": item["metadata"]["source_sha256"],
        "canonical_receipt_id": item["metadata"]["canonical_receipt_id"],
    }
    assert await row_counts(state) == before
    assert await state.get_memory(item["id"]) == item


@pytest.mark.asyncio
async def test_mixed_results_translate_only_english_items_in_original_order(tmp_path):
    state, original = await seeded(tmp_path)
    english = await state.create_memory(MemoryCreate(content="Keep another file."), "fixture")
    shown = await finalize(state, [english, original])
    assert [row["id"] for row in shown] == [english["id"], original["id"]]
    assert [row.memory_id for row in state.memory_presenter.calls[0].items] == [english["id"]]
    assert shown[0]["presentation"]["validation_status"] == "model_reviewed"
    assert "mode" not in shown[0]["presentation"]
    assert shown[1]["presentation"]["content"] == FRENCH


@pytest.mark.asyncio
async def test_english_summary_keeps_whole_item_on_reviewed_translation_path(tmp_path):
    state, item = await seeded(tmp_path, summary="Keep this summary.")
    shown = (await finalize(state, [item]))[0]
    assert len(state.memory_presenter.calls) == 1
    assert shown["presentation"]["validation_status"] == "model_reviewed"
    assert "mode" not in shown["presentation"]


@pytest.mark.asyncio
async def test_english_query_still_returns_canonical_text_without_presentation(tmp_path):
    state, item = await seeded(tmp_path)
    assert await finalize(state, [item], french=False) == [item]
    assert state.memory_presenter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "scope", "revision", "receipt", "metadata"])
async def test_original_changed_while_english_translation_waits_is_never_returned(tmp_path, change):
    state, original = await seeded(tmp_path)
    english = await state.create_memory(MemoryCreate(content="Keep another file."), "fixture")

    async def mutate():
        async with aiosqlite.connect(state.db_path) as db:
            if change == "source":
                await db.execute(
                    "UPDATE memory_source_journal SET content='changed' WHERE id=?",
                    (original["metadata"]["source_id"],),
                )
            elif change == "receipt":
                await db.execute(
                    "UPDATE memory_canonical_receipts SET status='failed' WHERE memory_id=?",
                    (original["id"],),
                )
            elif change == "metadata":
                metadata = original["metadata"] | {"source_sha256": "0" * 64}
                await db.execute(
                    "UPDATE memory_items SET metadata_json=? WHERE id=?",
                    (json.dumps(metadata), original["id"]),
                )
            else:
                field = "scope" if change == "scope" else "updated_at"
                await db.execute(
                    f"UPDATE memory_items SET {field}='changed' WHERE id=?", (original["id"],)
                )
            await db.commit()

    state.memory_presenter.change = mutate
    with pytest.raises(MemoryNormalizationError) as caught:
        await finalize(state, [original, english])
    assert caught.value.category == "source_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["source_id", "source_sha256", "canonical_receipt_id", "unit_hash", "journal"]
)
async def test_forged_source_links_or_hashes_do_not_return_an_original(tmp_path, change):
    state, item = await seeded(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        metadata = item["metadata"]
        if change == "journal":
            await db.execute(
                "UPDATE memory_source_journal SET summary='injected' WHERE id=?",
                (metadata["source_id"],),
            )
        else:
            if change == "unit_hash":
                metadata["content"]["source_sha256"] = "0" * 64
            else:
                metadata[change] = "0" * 64
            await db.execute(
                "UPDATE memory_items SET metadata_json=? WHERE id=?",
                (json.dumps(metadata), item["id"]),
            )
        await db.commit()
    fresh = await state.get_memory(item["id"])
    with pytest.raises(MemoryNormalizationError) as caught:
        await finalize(state, [fresh])
    assert caught.value.category == "unavailable"
    assert state.memory_presenter.calls == []


@pytest.mark.asyncio
async def test_original_output_counts_utf8_bytes_and_total_batch(tmp_path, monkeypatch):
    state, item = await seeded(tmp_path, summary=SUMMARY)
    total = len((FRENCH + SUMMARY).encode("utf-8"))
    monkeypatch.setattr(display, "MAX_PRESENTATION_OUTPUT_BYTES", total - 1)
    with pytest.raises(MemoryNormalizationError, match="presentation_output_budget_exceeded"):
        await finalize(state, [item])
    assert state.memory_presenter.calls == []


@pytest.mark.asyncio
async def test_original_rechecks_after_initial_read_before_return(tmp_path, monkeypatch):
    state, item = await seeded(tmp_path)
    previous = display._recheck

    async def check(*args, **kwargs):
        value = await previous(*args, **kwargs)
        if kwargs["initial"]:
            await state.delete_memory(item["id"], "fixture")
        return value

    monkeypatch.setattr(display, "_recheck", check)
    with pytest.raises(MemoryNormalizationError) as caught:
        await finalize(state, [item])
    assert caught.value.category == "source_conflict"
    assert state.memory_presenter.calls == []


@pytest.mark.asyncio
async def test_original_does_not_claim_or_wait_for_model_gate(tmp_path):
    state, item = await seeded(tmp_path)
    async with state.memory_normalization_gate:
        result = await asyncio.wait_for(finalize(state, [item]), 1)
        assert result[0]["presentation"]["content"] == FRENCH
    assert state.memory_presenter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", ["original", "mixed_original", "mixed_english"])
async def test_duplicate_items_are_rejected_before_any_presentation(tmp_path, duplicate):
    state, original = await seeded(tmp_path)
    english = await state.create_memory(MemoryCreate(content="Keep another file."), "fixture")
    items = {
        "original": [original, original],
        "mixed_original": [original, english, original],
        "mixed_english": [english, original, english],
    }[duplicate]
    with pytest.raises(MemoryNormalizationError, match="duplicate_presentation_memory"):
        await finalize(state, items)
    assert state.memory_presenter.calls == []


@pytest.mark.asyncio
async def test_mixed_output_limit_counts_original_and_translation_together(tmp_path, monkeypatch):
    state, original = await seeded(tmp_path)
    english = await state.create_memory(MemoryCreate(content="Keep another file."), "fixture")
    total = len((FRENCH + "Conservation des clients.").encode("utf-8"))
    monkeypatch.setattr(display, "MAX_PRESENTATION_OUTPUT_BYTES", total - 1)
    with pytest.raises(MemoryNormalizationError, match="presentation_output_budget_exceeded"):
        await finalize(state, [original, english])
    assert len(state.memory_presenter.calls) == 1


@pytest.mark.asyncio
async def test_mixed_input_limit_is_not_reset_for_each_partition(tmp_path, monkeypatch):
    state, original = await seeded(tmp_path)
    english = await state.create_memory(MemoryCreate(content="Keep another file."), "fixture")
    total = len((original["content"] + english["content"]).encode("utf-8"))
    monkeypatch.setattr(display, "MAX_PRESENTATION_SOURCE_BYTES", total - 1)
    with pytest.raises(MemoryNormalizationError, match="presentation_source_budget_exceeded"):
        await finalize(state, [original, english])
    assert state.memory_presenter.calls == []
