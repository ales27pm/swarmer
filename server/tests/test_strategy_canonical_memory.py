from __future__ import annotations

import hashlib
import json
from pathlib import Path

import aiosqlite
import pytest

from app.models import MemoryCreate
from app.services.episode_memory import EpisodeMemoryService
from app.services.memory_normalization import MemoryNormalizationError, canonical_text_sha256
from app.services.memory_presentation import (
    PRESENTATION_POLICY_SHA256,
    PRESENTATION_POLICY_VERSION,
    MemoryPresentationResult,
    MemoryPresentedItem,
)
from app.services.state_service import StateService
from app.services.strategy_retrieval import StrategyRetrieval
from tests.test_memory_canonical_store import ReviewedNormalizer
from tests.test_strategy_retrieval import _new_goal


class QueryNormalizer(ReviewedNormalizer):
    async def normalize(self, source, *, recheck_source=None):
        result = await super().normalize(source, recheck_source=recheck_source)
        if source.kind == "query":
            return result.model_copy(
                update={
                    "canonical_text": "send automatically",
                    "canonical_sha256": canonical_text_sha256("send automatically"),
                    "source_language": "fr",
                }
            )
        return result


class FrenchPresenter:
    presentation_signature = hashlib.sha256(b"fixture-presentation").hexdigest()

    def __init__(self):
        self.calls = []

    async def present(self, batch, *, recheck_sources=None):
        self.calls.append(batch)
        if recheck_sources and not await recheck_sources(batch):
            raise MemoryNormalizationError("source_conflict", "source_changed")
        source_hash = hashlib.sha256(
            json.dumps(
                batch.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        return MemoryPresentationResult(
            items=[
                MemoryPresentedItem(
                    memory_id=item.memory_id,
                    scope=item.scope,
                    source_revision=item.source_revision,
                    canonical_sha256=item.canonical_sha256,
                    summary_sha256=item.summary_sha256,
                    display_text="Ne pas envoyer automatiquement.",
                    display_summary=None,
                )
                for item in batch.items
            ],
            source_batch_sha256=source_hash,
            presentation_signature=self.presentation_signature,
            presentation_policy_version=PRESENTATION_POLICY_VERSION,
            presentation_policy_sha256=PRESENTATION_POLICY_SHA256,
            translator_model="fixture",
            translator_revision=None,
            reviewer_model="fixture",
            reviewer_revision=None,
            sources_revalidated=recheck_sources is not None,
        )


async def _service(tmp_path: Path):
    normalizer = QueryNormalizer()
    presenter = FrenchPresenter()
    state = StateService(
        tmp_path / "state.db",
        canonical_language="en",
        memory_normalizer=normalizer,
        memory_presenter=presenter,
    )
    await state.initialize()
    return state, normalizer, presenter


@pytest.mark.asyncio
async def test_french_agent_query_returns_french_hint_from_verified_english_without_writes(
    tmp_path,
):
    state, normalizer, presenter = await _service(tmp_path)
    memory = await state.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    before = await _counts(state.db_path)
    retrieval = StrategyRetrieval(
        state.db_path, EpisodeMemoryService(state.db_path), canonical_memory=state
    )
    hints = await retrieval.retrieve("Dois-je envoyer automatiquement ?")
    assert len(hints.memory) == 1
    hint = hints.memory[0]
    assert hint.text == "Ne pas envoyer automatiquement."
    assert hint.source_id == memory["id"]
    assert hint.as_dict()["canonical_language"] == "en"
    assert hint.as_dict()["presentation_language"] == "fr"
    assert hint.as_dict()["source_revision"] == memory["updated_at"]
    assert hint.as_dict()["canonical_content_sha256"] == canonical_text_sha256(memory["content"])
    assert await _counts(state.db_path) == before
    assert len(normalizer.calls) == 2 and len(presenter.calls) == 1
    assert (await state.get_memory(memory["id"]))["content"] == "Do not send automatically."


async def _counts(path):
    async with aiosqlite.connect(path) as db:
        return [
            (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0]
            for table in (
                "memory_items",
                "memory_source_journal",
                "memory_canonical_receipts",
                "audit_events",
            )
        ]


@pytest.mark.asyncio
async def test_agent_hints_scope_sensitivity_and_plan_exclusion(tmp_path):
    state, _, _ = await _service(tmp_path)
    goal, _ = await _new_goal(state.db_path, "canonical")
    allowed = []
    for scope, kind, sensitivity in (
        ("general", "constraint", "normal"),
        ("project:project_strategy", "constraint", "normal"),
        ("project:other", "constraint", "normal"),
        ("general", "constraint", "secret"),
        ("general", "plan", "normal"),
    ):
        item = await state.create_memory(
            MemoryCreate(
                content="Ne pas envoyer automatiquement.",
                scope=scope,
                kind=kind,
                sensitivity=sensitivity,
            ),
            "phone",
        )
        if (
            scope in {"general", "project:project_strategy"}
            and sensitivity == "normal"
            and kind != "plan"
        ):
            allowed.append(item["id"])
    retrieval = StrategyRetrieval(
        state.db_path,
        EpisodeMemoryService(state.db_path),
        canonical_memory=state,
        max_memory_hints=20,
        max_success_hints=0,
        max_failure_hints=0,
    )
    hints = await retrieval.retrieve("Dois-je envoyer automatiquement ?", goal_run_id=goal)
    assert {hint.source_id for hint in hints.memory} == set(allowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "scope", "sensitivity", "source"])
async def test_final_snapshot_rejects_changed_canonical_source_not_french_comparison(
    tmp_path, change
):
    state, _, _ = await _service(tmp_path)
    item = await state.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )

    class ChangedAfterSearch(StrategyRetrieval):
        async def _memory_hints(self, query, *, goal_run_id):
            hints = await super()._memory_hints(query, goal_run_id=goal_run_id)
            assert hints
            async with aiosqlite.connect(self.db_path) as db:
                if change == "revision":
                    await db.execute(
                        "UPDATE memory_items SET updated_at='2026-10-01T01:00:00+00:00' WHERE id=?",
                        (item["id"],),
                    )
                elif change == "scope":
                    await db.execute(
                        "UPDATE memory_items SET scope='project:other' WHERE id=?", (item["id"],)
                    )
                elif change == "sensitivity":
                    await db.execute(
                        "UPDATE memory_items SET sensitivity='secret' WHERE id=?", (item["id"],)
                    )
                else:
                    await db.execute(
                        "UPDATE memory_items SET content='Send automatically.' WHERE id=?",
                        (item["id"],),
                    )
                await db.commit()
            return hints

    hints = await ChangedAfterSearch(
        state.db_path, EpisodeMemoryService(state.db_path), canonical_memory=state
    ).retrieve("Dois-je envoyer automatiquement ?")
    assert hints.memory == () and hints.provenance_ids == ()


@pytest.mark.asyncio
async def test_agent_translation_failure_is_explicit_and_disabled_path_does_not_call_it(tmp_path):
    state, normalizer, _ = await _service(tmp_path)
    await state.create_memory(MemoryCreate(content="Ne pas envoyer automatiquement."), "phone")
    normalizer.failure = MemoryNormalizationError("unavailable", "provider_unavailable")
    with pytest.raises(MemoryNormalizationError):
        await StrategyRetrieval(
            state.db_path, EpisodeMemoryService(state.db_path), canonical_memory=state
        ).retrieve("Dois-je envoyer automatiquement ?")
    before = len(normalizer.calls)
    await StrategyRetrieval(state.db_path, EpisodeMemoryService(state.db_path)).retrieve(
        "send automatically"
    )
    assert len(normalizer.calls) == before


@pytest.mark.asyncio
async def test_english_query_keeps_english_hint_without_requiring_french_presentation(tmp_path):
    normalizer = ReviewedNormalizer()
    presenter = FrenchPresenter()
    state = StateService(
        tmp_path / "state.db",
        canonical_language="en",
        memory_normalizer=normalizer,
        memory_presenter=presenter,
    )
    await state.initialize()
    memory = await state.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    hints = await StrategyRetrieval(
        state.db_path, EpisodeMemoryService(state.db_path), canonical_memory=state
    ).retrieve("send automatically")
    assert hints.memory[0].text == "Do not send automatically."
    assert hints.memory[0].source_id == memory["id"]
    assert hints.memory[0].as_dict()["canonical_language"] == "en"
    assert "presentation_language" not in hints.memory[0].as_dict()
    assert presenter.calls == []
