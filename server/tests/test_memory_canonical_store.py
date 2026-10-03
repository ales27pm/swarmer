from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import aiosqlite
import pytest

from app.models import MemoryCreate, MemoryUpdate
from app.services.memory_normalization import (
    POLICY_SHA256,
    POLICY_VERSION,
    MemoryNormalizationError,
    MemoryNormalizationResult,
    MemoryNormalizationSource,
    canonical_text_sha256,
    normalization_identity,
)
from app.services.state_service import StateService


class ReviewedNormalizer:
    normalization_signature = hashlib.sha256(b"fixture-normalizer-v1").hexdigest()

    def __init__(self) -> None:
        self.calls: list[MemoryNormalizationSource] = []
        self.failure: MemoryNormalizationError | None = None
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def normalize(self, source: MemoryNormalizationSource, *, recheck_source=None):
        self.calls.append(source)
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        if self.failure:
            raise self.failure
        if recheck_source and not await recheck_source(source):
            raise MemoryNormalizationError("source_conflict", "source_changed")
        english = {
            "Ne pas envoyer automatiquement.": "Do not send automatically.",
            "Garder les dates exactes.": "Keep the exact dates.",
            "Une possibilité, pas une certitude.": "A possibility, not a certainty.",
        }.get(source.text, source.text)
        return MemoryNormalizationResult(
            canonical_text=english,
            canonical_sha256=canonical_text_sha256(english),
            scope=source.scope,
            kind=source.kind,
            applicability_sha256=source.applicability_sha256,
            source_id=source.source_id,
            source_version=source.source_version,
            source_sha256=source.source_sha256,
            source_language="fr" if english != source.text else "en",
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


async def enabled(tmp_path: Path, provider: ReviewedNormalizer | None = None) -> StateService:
    service = StateService(
        tmp_path / "state.db",
        canonical_language="en",
        memory_normalizer=provider,
        memory_normalization_timeout_seconds=2,
    )
    await service.initialize()
    return service


async def count(service: StateService, table: str) -> int:
    assert table in {
        "memory_items",
        "memory_source_journal",
        "memory_canonical_receipts",
        "memory_embeddings",
    }
    async with aiosqlite.connect(service.db_path) as db:
        row = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.asyncio
async def test_french_create_replay_and_restart_keep_one_english_record_and_original(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    request = MemoryCreate(
        content="Ne pas envoyer automatiquement.", summary="Une possibilité, pas une certitude."
    )
    first = await service.create_memory(request, "phone")
    replay = await service.create_memory(request, "phone")
    restarted = await enabled(tmp_path, provider)
    assert await restarted.create_memory(request, "phone") == replay == first
    assert first["content"] == "Do not send automatically."
    assert first["summary"] == "A possibility, not a certainty."
    assert first["metadata"]["canonical_language"] == "en"
    assert len(provider.calls) == 2
    assert await count(service, "memory_items") == 1
    async with aiosqlite.connect(service.db_path) as db:
        row = await (
            await db.execute("SELECT content,summary FROM memory_source_journal")
        ).fetchone()
    assert row == (request.content, request.summary)


@pytest.mark.asyncio
async def test_no_provider_and_invalid_translation_never_create_active_french_record(tmp_path):
    service = await enabled(tmp_path)
    with pytest.raises(MemoryNormalizationError) as absent:
        await service.create_memory(
            MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
        )
    assert absent.value.category == "unavailable"
    provider = ReviewedNormalizer()
    provider.failure = MemoryNormalizationError("uncertain", "review_not_accepted")
    service.memory_normalizer = provider
    with pytest.raises(MemoryNormalizationError) as rejected:
        await service.create_memory(
            MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
        )
    assert rejected.value.category == "uncertain"
    assert await count(service, "memory_items") == 0
    assert await count(service, "memory_source_journal") == 1


@pytest.mark.asyncio
async def test_update_preserves_both_original_sources_and_pin_does_not_translate(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    first = await service.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    updated = await service.update_memory(
        first["id"], MemoryUpdate(content="Garder les dates exactes."), "phone"
    )
    assert updated and updated["content"] == "Keep the exact dates."
    assert await count(service, "memory_items") == 1
    assert await count(service, "memory_source_journal") == 2
    pinned = await service.update_memory(first["id"], MemoryUpdate(pinned=True), "phone")
    assert pinned and pinned["pinned"] is True and len(provider.calls) == 2
    assert (
        await service.update_memory(
            first["id"], MemoryUpdate(content="Garder les dates exactes."), "phone"
        )
    )["content"] == updated["content"]
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_pending_write_does_not_overwrite_concurrent_correction(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    item = await service.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(
        service.update_memory(
            item["id"], MemoryUpdate(content="Garder les dates exactes."), "phone"
        )
    )
    await asyncio.wait_for(provider.entered.wait(), 2)
    # A second legacy service stands in for an already-authorized concurrent edit.
    concurrent = StateService(service.db_path)
    await concurrent.update_memory(
        item["id"], MemoryUpdate(content="Keep this newer correction."), "phone"
    )
    provider.release.set()
    with pytest.raises(MemoryNormalizationError) as conflict:
        await pending
    assert conflict.value.category == "source_conflict"
    assert (await service.get_memory(item["id"]))["content"] == "Keep this newer correction."


@pytest.mark.asyncio
async def test_scope_kind_and_sensitivity_never_deduplicate_across_boundaries(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    items = []
    for extras in (
        {},
        {"scope": "project:one"},
        {"kind": "preference"},
        {"sensitivity": "private"},
    ):
        items.append(
            await service.create_memory(
                MemoryCreate(content="Ne pas envoyer automatiquement.", **extras), "phone"
            )
        )
    assert len({item["id"] for item in items}) == 4 and len(provider.calls) == 4


@pytest.mark.asyncio
async def test_busy_and_oversized_requests_do_not_wait_or_call_another_model(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    with pytest.raises(MemoryNormalizationError) as large:
        await service.create_memory(MemoryCreate(content="é" * 4001), "phone")
    assert large.value.category == "invalid" and provider.calls == []
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(
        service.create_memory(MemoryCreate(content="Ne pas envoyer automatiquement."), "phone")
    )
    await asyncio.wait_for(provider.entered.wait(), 2)
    with pytest.raises(MemoryNormalizationError):
        await asyncio.wait_for(
            service.create_memory(MemoryCreate(content="Garder les dates exactes."), "phone"), 0.25
        )
    provider.release.set()
    await pending
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_legacy_default_keeps_current_behavior_without_normalizer_calls(tmp_path):
    provider = ReviewedNormalizer()
    service = StateService(tmp_path / "state.db", memory_normalizer=provider)
    await service.initialize()
    item = await service.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    assert item["content"] == "Ne pas envoyer automatiquement." and provider.calls == []


@pytest.mark.asyncio
async def test_delete_purges_originals_and_fresh_create_gets_new_generation(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    request = MemoryCreate(content="Ne pas envoyer automatiquement.")
    first = await service.create_memory(request, "phone")
    assert await service.delete_memory(first["id"], "phone")
    assert await count(service, "memory_source_journal") == 0
    async with aiosqlite.connect(service.db_path) as db:
        assert await (
            await db.execute("SELECT status,result_json FROM memory_canonical_receipts")
        ).fetchone() == ("deleted", None)
    fresh = await service.create_memory(request, "phone")
    assert fresh["id"] != first["id"] and len(provider.calls) == 2
    assert await count(service, "memory_items") == 1


@pytest.mark.asyncio
async def test_delete_during_update_never_restores_memory_or_originals(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    first = await service.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(
        service.update_memory(
            first["id"], MemoryUpdate(content="Garder les dates exactes."), "phone"
        )
    )
    await asyncio.wait_for(provider.entered.wait(), 2)
    await service.delete_memory(first["id"], "phone")
    provider.release.set()
    with pytest.raises(MemoryNormalizationError) as conflict:
        await pending
    assert conflict.value.category == "source_conflict"
    assert (
        await count(service, "memory_items") == await count(service, "memory_source_journal") == 0
    )


@pytest.mark.asyncio
async def test_normalizer_replacement_during_call_keeps_original_only(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(
        service.create_memory(MemoryCreate(content="Ne pas envoyer automatiquement."), "phone")
    )
    await asyncio.wait_for(provider.entered.wait(), 2)
    service.memory_normalizer = ReviewedNormalizer()
    provider.release.set()
    with pytest.raises(MemoryNormalizationError) as conflict:
        await pending
    assert conflict.value.category == "source_conflict"
    assert await count(service, "memory_items") == 0


@pytest.mark.asyncio
async def test_source_journal_change_during_call_cannot_publish(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(
        service.create_memory(MemoryCreate(content="Ne pas envoyer automatiquement."), "phone")
    )
    await asyncio.wait_for(provider.entered.wait(), 2)
    async with aiosqlite.connect(service.db_path) as db:
        await db.execute("UPDATE memory_source_journal SET content='changed source'")
        await db.commit()
    provider.release.set()
    with pytest.raises(MemoryNormalizationError) as conflict:
        await pending
    assert (
        conflict.value.category == "source_conflict" and await count(service, "memory_items") == 0
    )


@pytest.mark.asyncio
async def test_late_duplicate_after_user_correction_cannot_revert_or_duplicate(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    request = MemoryCreate(content="Ne pas envoyer automatiquement.")
    first = await service.create_memory(request, "phone")
    await service.update_memory(
        first["id"], MemoryUpdate(content="Garder les dates exactes."), "phone"
    )
    with pytest.raises(MemoryNormalizationError) as stale:
        await service.create_memory(request, "phone")
    assert stale.value.category == "source_conflict"
    assert await count(service, "memory_items") == 1 and len(provider.calls) == 2
    assert (await service.get_memory(first["id"]))["content"] == "Keep the exact dates."


@pytest.mark.asyncio
async def test_independent_instances_do_not_duplicate_a_pending_create(tmp_path):
    provider = ReviewedNormalizer()
    first = await enabled(tmp_path, provider)
    second = await enabled(tmp_path, provider)
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    request = MemoryCreate(content="Ne pas envoyer automatiquement.")
    pending = asyncio.create_task(first.create_memory(request, "phone"))
    await asyncio.wait_for(provider.entered.wait(), 2)
    with pytest.raises(MemoryNormalizationError) as duplicate:
        await second.create_memory(request, "phone")
    assert duplicate.value.reason == "normalization_in_progress"
    provider.release.set()
    accepted = await pending
    assert await second.create_memory(request, "phone") == accepted
    assert len(provider.calls) == await count(first, "memory_items") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"pinned": True}, {"confidence": 0.25}])
async def test_create_metadata_changes_do_not_collapse_to_earlier_memory(tmp_path, change):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    original = await service.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    other = await service.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement.", **change), "phone"
    )
    assert original["id"] != other["id"]
    for key, value in change.items():
        assert other[key] == value


@pytest.mark.asyncio
async def test_deadline_releases_gate_and_explicit_retry_can_succeed(tmp_path):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    provider.release = asyncio.Event()
    service.memory_normalization_timeout_seconds = 0.03
    request = MemoryCreate(content="Ne pas envoyer automatiquement.")
    with pytest.raises(MemoryNormalizationError) as timeout:
        await service.create_memory(request, "phone")
    assert timeout.value.reason == "deadline_exceeded"
    assert not service.memory_normalization_gate.locked()
    assert await count(service, "memory_items") == 0
    provider.release.set()
    service.memory_normalization_timeout_seconds = 2
    assert (await service.create_memory(request, "phone"))[
        "content"
    ] == "Do not send automatically."


@pytest.mark.asyncio
async def test_invalid_canonical_hash_never_publishes(tmp_path):
    class WrongHash(ReviewedNormalizer):
        async def normalize(self, source, *, recheck_source=None):
            result = await super().normalize(source, recheck_source=recheck_source)
            return result.model_copy(update={"canonical_sha256": "0" * 64})

    service = await enabled(tmp_path, WrongHash())
    with pytest.raises(MemoryNormalizationError) as invalid:
        await service.create_memory(
            MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
        )
    assert invalid.value.category == "invalid" and await count(service, "memory_items") == 0


@pytest.mark.asyncio
async def test_summary_rejection_is_atomic_and_preserves_prior_revision(tmp_path):
    class RejectSummary(ReviewedNormalizer):
        async def normalize(self, source, *, recheck_source=None):
            if source.source_id.endswith(":summary"):
                raise MemoryNormalizationError("uncertain", "review_not_accepted")
            return await super().normalize(source, recheck_source=recheck_source)

    provider = RejectSummary()
    service = await enabled(tmp_path, provider)
    first = await service.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    with pytest.raises(MemoryNormalizationError):
        await service.update_memory(
            first["id"],
            MemoryUpdate(
                content="Garder les dates exactes.", summary="Une possibilité, pas une certitude."
            ),
            "phone",
        )
    assert await service.get_memory(first["id"]) == first
    assert await count(service, "memory_items") == 1


@pytest.mark.asyncio
async def test_legacy_original_preserved_when_first_converted_and_gets_are_inert(tmp_path):
    legacy = StateService(tmp_path / "state.db")
    await legacy.initialize()
    old = await legacy.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    current = await service.update_memory(
        old["id"], MemoryUpdate(content="Garder les dates exactes."), "phone"
    )
    assert current["content"] == "Keep the exact dates."
    assert await count(service, "memory_source_journal") == 2
    await service.get_memory(old["id"])
    await service.list_memory()
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_accepted_english_and_bound_original_views_are_embedded(tmp_path):
    from tests.test_memory_indexing_regressions import LocalProvider

    provider = ReviewedNormalizer()
    embeddings = LocalProvider()
    service = await enabled(tmp_path, provider)
    service.embedding_service = embeddings
    await service.create_memory(MemoryCreate(content="Ne pas envoyer automatiquement."), "phone")
    assert embeddings.calls == [
        ["Do not send automatically. "],
        ["Ne pas envoyer automatiquement. "],
    ]


@pytest.mark.asyncio
async def test_unvalidated_model_copy_cannot_bypass_english_receipt_contract(tmp_path):
    class WrongLanguage(ReviewedNormalizer):
        async def normalize(self, source, *, recheck_source=None):
            result = await super().normalize(source, recheck_source=recheck_source)
            return result.model_copy(update={"canonical_language": "fr"})

    service = await enabled(tmp_path, WrongLanguage())
    with pytest.raises(MemoryNormalizationError) as rejected:
        await service.create_memory(
            MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
        )
    assert rejected.value.category == "invalid" and await count(service, "memory_items") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["source", "receipt"])
async def test_replay_rejects_missing_source_or_corrupt_receipt(tmp_path, changed):
    provider = ReviewedNormalizer()
    service = await enabled(tmp_path, provider)
    request = MemoryCreate(content="Ne pas envoyer automatiquement.")
    item = await service.create_memory(request, "phone")
    async with aiosqlite.connect(service.db_path) as db:
        if changed == "source":
            await db.execute("DELETE FROM memory_source_journal")
        else:
            await db.execute("UPDATE memory_canonical_receipts SET result_json='not json'")
        await db.commit()
    with pytest.raises(MemoryNormalizationError) as conflict:
        await service.create_memory(request, "phone")
    assert conflict.value.category == "source_conflict"
    assert (
        len(provider.calls) == 1
        and (await service.get_memory(item["id"]))["content"] == item["content"]
    )
