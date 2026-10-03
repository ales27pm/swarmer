from __future__ import annotations

import copy
import hashlib
import json

import aiosqlite
import pytest

from app.models import MemoryCreate
from app.services.episode_memory import EpisodeMemoryService
from app.services.memory_normalization import MemoryNormalizationError, canonical_text_sha256
from app.services.strategy_retrieval import StrategyRetrieval
from tests.test_memory_canonical_store import ReviewedNormalizer, enabled


async def _source(tmp_path, monkeypatch, *, summary=None):
    state = await enabled(tmp_path, ReviewedNormalizer())
    request = MemoryCreate(content="Ne pas envoyer automatiquement.", summary=summary)
    memory = await state.create_memory(request, "phone")
    item = copy.deepcopy(memory)
    item["score"] = 1.0
    item["presentation"] = {
        "mode": "original",
        "language": "fr",
        "temporary": True,
        "content": request.content,
        "summary": request.summary,
        "canonical_sha256": canonical_text_sha256(memory["content"]),
        "summary_sha256": (
            canonical_text_sha256(memory["summary"]) if memory["summary"] is not None else None
        ),
        "source_revision": memory["updated_at"],
        "validation_status": "source_preserved",
        "grants_authority": False,
        **{
            key: memory["metadata"][key]
            for key in ("source_id", "source_sha256", "canonical_receipt_id")
        },
    }

    async def search(*args, **kwargs):
        return [item]

    monkeypatch.setattr(state, "search_memory", search)
    retrieval = StrategyRetrieval(
        state.db_path,
        EpisodeMemoryService(state.db_path),
        canonical_memory=state,
        max_success_hints=0,
        max_failure_hints=0,
    )
    return state, item, retrieval


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [None, "Une possibilité, pas une certitude."])
async def test_strategy_accepts_bound_original_french_with_unchanged_canonical_record(
    tmp_path, monkeypatch, summary
):
    state, item, retrieval = await _source(tmp_path, monkeypatch, summary=summary)
    hints = await retrieval.retrieve("Dois-je envoyer automatiquement ?")
    assert len(hints.memory) == 1
    hint = hints.memory[0]
    assert hint.text == (summary or "Ne pas envoyer automatiquement.")
    assert hint.source_id == item["id"]
    assert hint.canonical_receipt_id == item["metadata"]["canonical_receipt_id"]
    assert hint.presentation_language == "fr"
    assert (await state.get_memory(item["id"]))["content"] == "Do not send automatically."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", None),
        ("mode", "source"),
        ("validation_status", "model_reviewed"),
        ("language", "en"),
        ("temporary", False),
        ("grants_authority", True),
        ("source_id", "msrc_foreign"),
        ("source_sha256", "0" * 64),
        ("canonical_receipt_id", "receipt_foreign"),
        ("canonical_sha256", "0" * 64),
        ("summary_sha256", "0" * 64),
        ("source_revision", "earlier-revision"),
        ("content", "Envoyer automatiquement."),
        ("content", ""),
        ("content", " \n"),
        ("content", None),
        ("summary", None),
        ("summary", ""),
        ("summary", "Résumé falsifié."),
    ],
)
async def test_strategy_rejects_forged_original_presentation(tmp_path, monkeypatch, field, value):
    _, item, retrieval = await _source(
        tmp_path, monkeypatch, summary="Une possibilité, pas une certitude."
    )
    item["presentation"][field] = value
    with pytest.raises(MemoryNormalizationError, match="strategy_memory_presentation_invalid"):
        await retrieval.retrieve("Dois-je envoyer automatiquement ?")


@pytest.mark.asyncio
async def test_strategy_rejects_original_summary_not_present_in_canonical_record(
    tmp_path, monkeypatch
):
    _, item, retrieval = await _source(tmp_path, monkeypatch)
    item["presentation"]["summary"] = "Résumé ajouté."
    with pytest.raises(MemoryNormalizationError, match="strategy_memory_presentation_invalid"):
        await retrieval.retrieve("Dois-je envoyer automatiquement ?")


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", ["content", "summary"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", "msrc_foreign:content"),
        ("source_sha256", "0" * 64),
        ("canonical_sha256", "0" * 64),
        ("source_language", "en"),
        ("source_revalidated", False),
        ("grants_authority", True),
    ],
)
async def test_strategy_rejects_unbound_original_units(tmp_path, monkeypatch, unit, field, value):
    _, item, retrieval = await _source(
        tmp_path, monkeypatch, summary="Une possibilité, pas une certitude."
    )
    item["metadata"][unit][field] = value
    with pytest.raises(MemoryNormalizationError, match="strategy_memory_presentation_invalid"):
        await retrieval.retrieve("Dois-je envoyer automatiquement ?")


@pytest.mark.asyncio
async def test_final_strategy_snapshot_rejects_coherent_forged_source_and_metadata(
    tmp_path, monkeypatch
):
    _, item, retrieval = await _source(tmp_path, monkeypatch)
    forged = "Envoyer automatiquement."
    source_hash = hashlib.sha256(
        json.dumps(
            {"content": forged, "summary": None},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    item["presentation"]["content"] = forged
    item["presentation"]["source_sha256"] = source_hash
    item["metadata"]["source_sha256"] = source_hash
    item["metadata"]["content"]["source_sha256"] = canonical_text_sha256(forged)
    assert (await retrieval.retrieve("Dois-je envoyer automatiquement ?")).memory == ()


@pytest.mark.asyncio
async def test_final_strategy_snapshot_rejects_changed_receipt_after_original_search(
    tmp_path, monkeypatch
):
    state, item, retrieval = await _source(tmp_path, monkeypatch)
    metadata = copy.deepcopy(item["metadata"])
    metadata["canonical_receipt_id"] = "newer-receipt"
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE memory_items SET metadata_json=? WHERE id=?",
            (json.dumps(metadata), item["id"]),
        )
        await db.commit()
    assert (await retrieval.retrieve("Dois-je envoyer automatiquement ?")).memory == ()


@pytest.mark.asyncio
async def test_strategy_keeps_legacy_reviewed_translation_contract(tmp_path, monkeypatch):
    _, item, retrieval = await _source(tmp_path, monkeypatch)
    presentation = item["presentation"]
    for field in ("mode", "source_id", "source_sha256", "canonical_receipt_id"):
        del presentation[field]
    presentation["validation_status"] = "model_reviewed"
    presentation["content"] = "Ne jamais envoyer automatiquement."
    hints = await retrieval.retrieve("Dois-je envoyer automatiquement ?")
    assert hints.memory[0].text == "Ne jamais envoyer automatiquement."


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["content", "summary", "source_id", "canonical_receipt_id"])
async def test_strategy_rejects_incomplete_original_contract(tmp_path, monkeypatch, missing):
    _, item, retrieval = await _source(tmp_path, monkeypatch)
    del item["presentation"][missing]
    with pytest.raises(MemoryNormalizationError, match="strategy_memory_presentation_invalid"):
        await retrieval.retrieve("Dois-je envoyer automatiquement ?")


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_metadata", [{}, {"source_language": "fr"}])
async def test_strategy_requires_null_summary_metadata_for_null_original_summary(
    tmp_path, monkeypatch, summary_metadata
):
    _, item, retrieval = await _source(tmp_path, monkeypatch)
    item["metadata"]["summary"] = summary_metadata
    with pytest.raises(MemoryNormalizationError, match="strategy_memory_presentation_invalid"):
        await retrieval.retrieve("Dois-je envoyer automatiquement ?")
