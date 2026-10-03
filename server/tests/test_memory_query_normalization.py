from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import aiosqlite
import pytest

from app.models import MemoryCreate, MemorySearch, MemoryUpdate
from app.services.memory_normalization import (
    POLICY_SHA256,
    POLICY_VERSION,
    MemoryNormalizationError,
    MemoryNormalizationResult,
    canonical_text_sha256,
    normalization_identity,
)
from app.services.memory_presentation import (
    PRESENTATION_POLICY_SHA256,
    PRESENTATION_POLICY_VERSION,
    MemoryPresentationResult,
    MemoryPresentedItem,
)
from app.services.state_service import StateService
from tests.test_memory_indexing_regressions import LocalProvider


class QueryNormalizer:
    normalization_signature = hashlib.sha256(b"query-fixture").hexdigest()

    def __init__(self, english: str = "customer retention") -> None:
        self.english = english
        self.calls = []
        self.failure = None
        self.entered = asyncio.Event()
        self.release = None
        self.change = None
        self.source_language = "fr"

    async def normalize(self, source, *, recheck_source=None):
        self.calls.append(source)
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.change:
            await self.change()
        if self.failure:
            raise self.failure
        if recheck_source and not await recheck_source(source):
            raise MemoryNormalizationError("source_conflict", "source_changed")
        return MemoryNormalizationResult(
            canonical_text=self.english if source.kind == "query" else source.text,
            canonical_sha256=canonical_text_sha256(
                self.english if source.kind == "query" else source.text
            ),
            scope=source.scope,
            kind=source.kind,
            applicability_sha256=source.applicability_sha256,
            source_id=source.source_id,
            source_version=source.source_version,
            source_sha256=source.source_sha256,
            source_language=self.source_language if source.kind == "query" else "en",
            language_origin="model_reviewed",
            expected_memory_revision=source.expected_memory_revision,
            normalization_policy_version=POLICY_VERSION,
            normalization_policy_sha256=POLICY_SHA256,
            normalization_signature=self.normalization_signature,
            translator_model="fixture-translator",
            translator_revision=None,
            reviewer_model="fixture-reviewer",
            reviewer_revision=None,
            source_revalidated=recheck_source is not None,
            literal_count=0,
            deduplication_identity=normalization_identity(source, self.normalization_signature),
        )


class FrenchPresenter:
    presentation_signature = hashlib.sha256(b"presenter-fixture").hexdigest()

    def __init__(self):
        self.calls = []
        self.change = None
        self.failure = None
        self.release = None
        self.entered = asyncio.Event()
        self.tamper = None

    async def present(self, batch, *, recheck_sources=None):
        self.calls.append(batch)
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.change:
            await self.change()
        if self.failure:
            raise self.failure
        if recheck_sources and not await recheck_sources(batch):
            raise MemoryNormalizationError("source_conflict", "source_changed")
        result = MemoryPresentationResult(
            items=[
                MemoryPresentedItem(
                    memory_id=item.memory_id,
                    scope=item.scope,
                    source_revision=item.source_revision,
                    canonical_sha256=item.canonical_sha256,
                    summary_sha256=item.summary_sha256,
                    display_text="Conservation des clients.",
                    display_summary="Résumé français." if item.summary is not None else None,
                )
                for item in batch.items
            ],
            source_batch_sha256=hashlib.sha256(
                json.dumps(
                    batch.model_dump(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest(),
            presentation_signature=self.presentation_signature,
            presentation_policy_version=PRESENTATION_POLICY_VERSION,
            presentation_policy_sha256=PRESENTATION_POLICY_SHA256,
            translator_model="fixture",
            translator_revision=None,
            reviewer_model="fixture-review",
            reviewer_revision=None,
            sources_revalidated=recheck_sources is not None,
        )
        if self.tamper:
            result = self.tamper(result)
        return result


async def seeded(tmp_path: Path, provider=None):
    state = StateService(
        tmp_path / "state.db",
        provider,
        canonical_language="en",
        memory_normalizer=QueryNormalizer(),
        memory_normalization_timeout_seconds=2,
    )
    state.memory_presenter = FrenchPresenter()
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content="customer retention"), "phone")
    state.memory_normalizer.calls.clear()
    state.memory_normalizer.entered.clear()
    return state, item


async def row_counts(state):
    async with aiosqlite.connect(state.db_path) as db:
        tables = [
            row[0]
            for row in await (
                await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ).fetchall()
        ]
        return {
            name: (await (await db.execute(f'SELECT COUNT(*) FROM "{name}"')).fetchone())[0]
            for name in tables
        }


@pytest.mark.asyncio
async def test_french_query_finds_english_without_any_durable_write(tmp_path: Path) -> None:
    state, item = await seeded(tmp_path)
    before = await row_counts(state)
    result = await state.search_memory(MemorySearch(query="conservation des clients"))
    assert [(row["id"], row["search_kind"]) for row in result] == [(item["id"], "lexical")]
    assert result[0]["score"] == 1.0
    assert len(state.memory_normalizer.calls) == 1
    assert await row_counts(state) == before


@pytest.mark.asyncio
async def test_query_preserves_original_literal_channel_and_uses_single_bilingual_batch(
    tmp_path: Path,
) -> None:
    provider = LocalProvider()
    state, item = await seeded(tmp_path, provider)
    state.embedding_service = None
    literal = await state.create_memory(MemoryCreate(content="CRM_KEEP_30D"), "phone")
    state.embedding_service = provider
    state.memory_normalizer.calls.clear()
    provider.calls.clear()
    found = await state.search_memory(MemorySearch(query="CRM_KEEP_30D"))
    assert {row["id"] for row in found} == {item["id"], literal["id"]}
    assert provider.calls == [["CRM_KEEP_30D", "customer retention"]]
    assert len({row["id"] for row in found}) == len(found)
    assert next(row for row in found if row["id"] == literal["id"])["search_kind"] == "lexical"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "uncertain", "invalid"])
async def test_query_normalization_failure_is_not_empty_search(
    tmp_path: Path, failure: str
) -> None:
    state, _ = await seeded(tmp_path)
    state.memory_normalizer.failure = MemoryNormalizationError(failure, "fixture")
    with pytest.raises(MemoryNormalizationError) as rejected:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert rejected.value.category == failure


@pytest.mark.asyncio
async def test_absent_query_normalizer_is_explicit_unavailable(tmp_path: Path) -> None:
    state, _ = await seeded(tmp_path)
    state.memory_normalizer = None
    with pytest.raises(MemoryNormalizationError) as rejected:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert rejected.value.category == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "signature"])
async def test_query_provider_change_during_translation_is_rejected(
    tmp_path: Path, change: str
) -> None:
    state, _ = await seeded(tmp_path)
    normalizer = state.memory_normalizer

    async def mutate():
        if change == "replace":
            state.memory_normalizer = QueryNormalizer()
        else:
            normalizer.normalization_signature = "a" * 64

    normalizer.change = mutate
    with pytest.raises(MemoryNormalizationError) as rejected:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert rejected.value.category == "source_conflict"


@pytest.mark.asyncio
async def test_busy_or_timed_out_query_normalizer_never_queues_or_retries(tmp_path: Path) -> None:
    state, _ = await seeded(tmp_path)
    await state.memory_normalization_gate.acquire()
    with pytest.raises(MemoryNormalizationError) as rejected:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert rejected.value.category == "unavailable" and state.memory_normalizer.calls == []
    state.memory_normalization_gate.release()
    state.memory_normalization_timeout_seconds = 0.01
    state.memory_normalizer.release = asyncio.Event()
    with pytest.raises(MemoryNormalizationError) as rejected:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert rejected.value.category == "unavailable" and len(state.memory_normalizer.calls) == 1
    assert not state.memory_normalization_gate.locked()


@pytest.mark.asyncio
async def test_deleted_memory_during_query_translation_is_not_returned(tmp_path: Path) -> None:
    state, item = await seeded(tmp_path)

    async def remove():
        await state.delete_memory(item["id"], "phone")

    state.memory_normalizer.change = remove
    assert await state.search_memory(MemorySearch(query="conservation des clients")) == []
    assert len(state.memory_normalizer.calls) == 1


@pytest.mark.asyncio
async def test_legacy_query_does_not_start_normalizer(tmp_path: Path) -> None:
    state, item = await seeded(tmp_path)
    state.canonical_language = "legacy"
    assert (await state.search_memory(MemorySearch(query="customer")))[0]["id"] == item["id"]
    assert state.memory_normalizer.calls == []


@pytest.mark.asyncio
async def test_translation_does_not_expand_requested_scope_or_kind(tmp_path: Path) -> None:
    state, item = await seeded(tmp_path)
    state.canonical_language = "legacy"
    await state.create_memory(MemoryCreate(content="customer retention", scope="another"), "phone")
    await state.create_memory(MemoryCreate(content="customer retention", kind="another"), "phone")
    state.canonical_language = "en"
    found = await state.search_memory(
        MemorySearch(query="conservation des clients", scope="general", kind="fact")
    )
    assert [row["id"] for row in found] == [item["id"]]


@pytest.mark.asyncio
async def test_query_rechecks_normalizer_after_embedding(tmp_path: Path) -> None:
    state, _ = await seeded(tmp_path)

    class SwitchingEmbedding(LocalProvider):
        async def embed(self, texts):
            vectors = await super().embed(texts)
            state.memory_normalizer = QueryNormalizer()
            return vectors

    provider = SwitchingEmbedding()
    state.embedding_service = provider
    with pytest.raises(MemoryNormalizationError) as rejected:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert rejected.value.category == "source_conflict"
    assert provider.calls == [["conservation des clients", "customer retention"]]


@pytest.mark.asyncio
async def test_cancellation_releases_normalizer_gate(tmp_path: Path) -> None:
    state, _ = await seeded(tmp_path)
    provider = state.memory_normalizer
    provider.release = asyncio.Event()
    pending = asyncio.create_task(
        state.search_memory(MemorySearch(query="conservation des clients"))
    )
    await asyncio.wait_for(provider.entered.wait(), 1)
    assert state.memory_normalization_gate.locked()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not state.memory_normalization_gate.locked()
    assert len(provider.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("translation", ["", " " * 8, "x" * 16001])
async def test_malformed_canonical_query_is_explicitly_rejected(
    tmp_path: Path, translation: str
) -> None:
    state, _ = await seeded(tmp_path)
    state.memory_normalizer.english = translation
    with pytest.raises(MemoryNormalizationError) as rejected:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert rejected.value.category == "invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [(), ("",), "general", (None,), ("x" * 101,), ("general",) * 17])
async def test_invalid_internal_scope_is_empty_without_model_call(tmp_path: Path, scopes) -> None:
    state, _ = await seeded(tmp_path)
    assert (
        await state.search_memory(
            MemorySearch(query="conservation des clients"), allowed_scopes=scopes
        )
        == []
    )
    assert state.memory_normalizer.calls == []


@pytest.mark.asyncio
async def test_internal_scope_and_sensitivity_apply_before_candidate_limit(tmp_path: Path) -> None:
    state, item = await seeded(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            """INSERT INTO memory_items(id,scope,kind,content,summary,sensitivity,confidence,pinned,metadata_json,created_at,updated_at)
            VALUES(?,?,'fact','customer retention',NULL,?,1,1,'{}','2030-01-01','2030-01-01')""",
            [(f"foreign_{i}", "project:other", "normal") for i in range(501)]
            + [(f"sensitive_{i}", "general", "private") for i in range(501)],
        )
        await db.commit()
    found = await state.search_memory(
        MemorySearch(query="conservation des clients"),
        allowed_scopes=("general", "project:current"),
        required_sensitivity="normal",
    )
    assert [row["id"] for row in found] == [item["id"]]
    assert len(state.memory_normalizer.calls) == 1
    state.memory_normalizer.calls.clear()
    assert (
        await state.search_memory(
            MemorySearch(query="conservation des clients", scope="project:other"),
            allowed_scopes=("general",),
        )
        == []
    )
    assert state.memory_normalizer.calls == []


@pytest.mark.asyncio
async def test_french_presentation_preserves_canonical_fields_and_is_ephemeral(tmp_path):
    state, item = await seeded(tmp_path)
    before = await row_counts(state)
    result = (await state.search_memory(MemorySearch(query="conservation des clients")))[0]
    assert result["content"] == item["content"] == "customer retention"
    assert result["presentation"] == {
        "language": "fr",
        "content": "Conservation des clients.",
        "summary": None,
        "canonical_sha256": canonical_text_sha256(item["content"]),
        "summary_sha256": None,
        "source_revision": item["updated_at"],
        "validation_status": "model_reviewed",
        "temporary": True,
        "grants_authority": False,
    }
    assert len(state.memory_presenter.calls) == 1
    assert await row_counts(state) == before
    assert (await state.get_memory(item["id"])) == item


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["delete", "content", "scope", "sensitivity", "receipt", "source"]
)
async def test_presentation_rejects_source_changed_during_model_call(tmp_path, change):
    state, item = await seeded(tmp_path)

    async def mutate():
        async with aiosqlite.connect(state.db_path) as db:
            if change == "delete":
                await db.execute("DELETE FROM memory_items WHERE id=?", (item["id"],))
            elif change in {"content", "scope", "sensitivity"}:
                await db.execute(
                    f"UPDATE memory_items SET {change}='changed' WHERE id=?", (item["id"],)
                )
            elif change == "receipt":
                await db.execute(
                    "UPDATE memory_canonical_receipts SET result_json='{}' WHERE memory_id=?",
                    (item["id"],),
                )
            else:
                await db.execute("DELETE FROM memory_source_journal")
            await db.commit()

    state.memory_presenter.change = mutate
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert caught.value.category == "source_conflict"
    assert len(state.memory_presenter.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "signature", "query_provider"])
async def test_presentation_rechecks_provider_and_normalizer(tmp_path, change):
    state, _ = await seeded(tmp_path)

    async def mutate():
        if change == "replace":
            state.memory_presenter = FrenchPresenter()
        elif change == "signature":
            state.memory_presenter.presentation_signature = "a" * 64
        else:
            state.memory_normalizer = QueryNormalizer()

    presenter = state.memory_presenter
    presenter.change = mutate
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert caught.value.category == "source_conflict"
    assert len(presenter.calls) == 1


@pytest.mark.asyncio
async def test_matching_legacy_memory_requires_qualification_before_presentation(tmp_path):
    state, item = await seeded(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE memory_items SET metadata_json='{}' WHERE id=?", (item["id"],))
        await db.commit()
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert caught.value.category == "unavailable"
    assert state.memory_presenter.calls == []


@pytest.mark.asyncio
async def test_missing_presenter_is_explicit_failure_only_for_nonempty_french_results(tmp_path):
    state, _ = await seeded(tmp_path)
    state.memory_presenter = None
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert caught.value.category == "unavailable"
    state.memory_normalizer.english = "unmatched"
    assert await state.search_memory(MemorySearch(query="aucune correspondance")) == []


@pytest.mark.asyncio
async def test_limit_is_applied_before_presentation_and_invalid_batch_cannot_escape(tmp_path):
    state, _ = await seeded(tmp_path)
    await state.create_memory(MemoryCreate(content="customer retention second"), "phone")
    result = await state.search_memory(MemorySearch(query="conservation des clients", limit=1))
    assert len(result) == len(state.memory_presenter.calls[0].items) == 1
    state.memory_presenter.tamper = lambda result: setattr(
        result.items[0], "source_revision", "stale"
    )
    with pytest.raises(MemoryNormalizationError):
        await state.search_memory(MemorySearch(query="conservation des clients"))


@pytest.mark.asyncio
async def test_presentation_timeout_and_cancellation_release_shared_gate(tmp_path):
    state, _ = await seeded(tmp_path)
    state.memory_normalization_timeout_seconds = 0.02
    state.memory_presenter.release = asyncio.Event()
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert caught.value.category == "unavailable"
    assert not state.memory_normalization_gate.locked()
    state.memory_normalization_timeout_seconds = 2
    state.memory_presenter.entered.clear()
    pending = asyncio.create_task(
        state.search_memory(MemorySearch(query="conservation des clients"))
    )
    await asyncio.wait_for(state.memory_presenter.entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not state.memory_normalization_gate.locked()


@pytest.mark.asyncio
async def test_english_query_keeps_canonical_result_without_presentation(tmp_path):
    state, item = await seeded(tmp_path)
    state.memory_normalizer.source_language = "en"
    state.memory_presenter = None
    result = await state.search_memory(MemorySearch(query="customer retention"))
    assert result[0]["content"] == item["content"]
    assert "presentation" not in result[0]


@pytest.mark.asyncio
async def test_summary_presentation_keeps_separate_canonical_hash(tmp_path):
    state, _ = await seeded(tmp_path)
    item = await state.create_memory(
        MemoryCreate(content="customer retention policy", summary="A tentative retention policy."),
        "phone",
    )
    result = await state.search_memory(MemorySearch(query="conservation des clients"))
    found = next(row for row in result if row["id"] == item["id"])
    assert found["summary"] == item["summary"]
    assert found["presentation"]["summary"] == "Résumé français."
    assert found["presentation"]["summary_sha256"] == canonical_text_sha256(item["summary"])


@pytest.mark.asyncio
async def test_oversized_selected_source_batch_is_explicit_and_never_translated(tmp_path):
    state, _ = await seeded(tmp_path)
    for index in range(3):
        await state.create_memory(
            MemoryCreate(content=f"customer retention {index} " + "x" * 6000), "phone"
        )
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert caught.value.reason == "presentation_source_budget_exceeded"
    assert state.memory_presenter.calls == []


@pytest.mark.asyncio
async def test_embedding_provider_change_during_presentation_invalidates_hybrid_score(tmp_path):
    state, _ = await seeded(tmp_path, LocalProvider())

    async def mutate():
        state.embedding_service = LocalProvider()

    state.memory_presenter.change = mutate
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert caught.value.category == "source_conflict"


@pytest.mark.asyncio
async def test_search_enforces_final_source_check_even_if_provider_skips_callback(tmp_path):
    state, item = await seeded(tmp_path)
    actual = state.memory_presenter

    class UncheckingPresenter(FrenchPresenter):
        async def present(self, batch, *, recheck_sources=None):
            result = await actual.present(batch, recheck_sources=recheck_sources)
            await state.delete_memory(item["id"], "phone")
            return result

    state.memory_presenter = UncheckingPresenter()
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert caught.value.category == "source_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["confidence", "expected_revision", "supersedes_revision"])
async def test_search_rejects_inapplicable_accepted_metadata(tmp_path, change):
    state, item = await seeded(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        if change == "confidence":
            await db.execute("UPDATE memory_items SET confidence=0.123 WHERE id=?", (item["id"],))
        elif change == "expected_revision":
            await db.execute(
                "UPDATE memory_canonical_receipts SET expected_revision='foreign_revision' WHERE memory_id=?",
                (item["id"],),
            )
        else:
            metadata = {**item["metadata"], "supersedes_revision": "foreign_revision"}
            await db.execute(
                "UPDATE memory_items SET metadata_json=? WHERE id=?",
                (json.dumps(metadata), item["id"]),
            )
        await db.commit()
    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="conservation des clients"))
    assert caught.value.category == "unavailable"
    assert state.memory_presenter.calls == []


@pytest.mark.asyncio
async def test_pin_only_revision_remains_qualified_and_presented(tmp_path):
    state, item = await seeded(tmp_path)
    updated = await state.update_memory(item["id"], MemoryUpdate(pinned=True), "phone")
    assert updated["updated_at"] != item["updated_at"]
    result = (await state.search_memory(MemorySearch(query="conservation des clients")))[0]
    assert result["id"] == item["id"] and result["pinned"] is True
    assert result["presentation"]["source_revision"] == updated["updated_at"]
    assert result["metadata"] == item["metadata"]
