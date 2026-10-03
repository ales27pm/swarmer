"""Independent public-service checks of native lexical search qualification."""

from __future__ import annotations

import asyncio
import json
import random

import aiosqlite
import pytest

from app.models import MemoryCreate, MemorySearch, MemoryUpdate
from app.services.memory_normalization import MemoryNormalizationError
from app.services.memory_search_views import lexical_scorer
from app.services.state_service import StateService
from tests.test_memory_canonical_store import ReviewedNormalizer
from tests.test_memory_query_normalization import QueryNormalizer


def reference_lexical_score(term_channels, content, summary):
    """The pre-optimization public scoring formula, without any regex machinery."""
    if content is None:
        return 0.0
    haystack = f"{content} {summary or ''}".casefold()
    return max(
        (
            sum(term in haystack for term in channel) / max(1, len(channel))
            for channel in term_channels
        ),
        default=0.0,
    )


@pytest.mark.parametrize(
    ("channels", "samples"),
    [
        (
            [("a", "ab", "aba", "abab", "ababa", "bab", "ba"), ("b", "baba", "absent")],
            [("ababa", None), ("babab", "ab"), ("aaaa", None), ("", "baba")],
        ),
        (
            [(".", ".*", "[", "]", "a+b", "(x)", "a|b", "^", "$", "?", "{2}", "\\b")],
            [("a+b[x]{2}.*", "(x)|a|b?^$\\b"), ("aaaaab", None), ("x", "a+b")],
        ),
        (
            [("strasse", "ss", "σ", "i\u0307", "ffi", "k"), ("é", "e\u0301", "missing")],
            [("Straße ẞ Σςσ İ ﬃ K", None), ("CAFÉ", "cafe\u0301"), ("STRASSE", None)],
        ),
        (
            [("src/a.swift", "a.swift", ".swift", "crm_keep_30d"), ("c:\\src\\a.py", "a.py")],
            [("SRC/A.SWIFT uses CRM_KEEP_30D", None), ("C:\\src\\A.py", "src/A.swift")],
        ),
        (
            [tuple("abcxyz0129/.[]+"), ("a", "aa", "aaa", "aaaa")],
            [("aaaa0[/]", None), ("9", "z+x"), ("", ""), (None, "abc")],
        ),
        (
            [(), ("shared", "native"), ("shared", "pivot", "missing")],
            [("shared native", "pivot"), ("SHARED", None), ("native", "pivot")],
        ),
        ([], [("anything", "summary"), (None, "anything")]),
    ],
    ids=[
        "overlapping-prefixes",
        "regex-literals",
        "unicode",
        "code-paths",
        "single-char",
        "channels",
        "empty",
    ],
)
def test_lexical_scorer_preserves_literal_substring_formula(channels, samples):
    score = lexical_scorer(channels)
    # Reuse one scorer, including repeated text, to catch state leaking between rows.
    for content, summary in samples + samples[::-1]:
        assert score(content, summary) == reference_lexical_score(channels, content, summary)


@pytest.mark.parametrize("seed", [0, 271828, 20261003])
def test_lexical_scorer_matches_reference_for_deterministic_random_inputs(seed):
    rng = random.Random(seed)
    alphabet = "aabAB012./\\[]()?+*|^$éÉΣςİßﬃ_"
    for _ in range(100):
        content = "".join(rng.choices(alphabet, k=rng.randrange(1, 160)))
        summary = "".join(rng.choices(alphabet, k=rng.randrange(0, 90)))
        folded = f"{content} {summary}".casefold()
        pool = {"missing_literal", "a", "aa", "aaa", "[", ".*", "ss"}
        for _ in range(35):
            start = rng.randrange(len(folded))
            pool.add(folded[start : start + rng.randrange(1, 15)].strip())
        pool.discard("")
        terms = sorted(pool)
        channels = [
            tuple(rng.sample(terms, rng.randrange(len(terms) + 1)))
            for _ in range(rng.randrange(1, 5))
        ]
        score = lexical_scorer(channels)
        for text, note in [
            (content, summary),
            (summary, None),
            (content.upper(), summary[::-1]),
            ("", content),
            (content, summary),
        ]:
            assert score(text, note) == reference_lexical_score(channels, text, note), (
                seed,
                channels,
                text,
                note,
            )


def test_lexical_scorer_does_not_truncate_large_term_sets_or_long_text():
    terms = tuple(f"term_{index:04d}" for index in range(512))
    channels = [terms, ("tail_marker", "missing_marker")]
    score = lexical_scorer(channels)
    samples = [
        ("x" * 2500 + terms[-1], None),
        ("x" * 2500, terms[-2]),
        (" ".join(terms), None),
        (" ".join(terms[::3]), "x" * 2500 + "tail_marker"),
    ]
    assert score(samples[0][0], None) == 1 / len(terms)
    for content, summary in samples:
        assert score(content, summary) == reference_lexical_score(channels, content, summary)


def test_lexical_scorer_counts_every_nested_term_and_long_literal():
    channels = [tuple("a" * length for length in range(1, 421)), ("a" * 2048 + "b", "z")]
    score = lexical_scorer(channels)
    for content, summary in [
        ("a" * 2500 + "b", None),
        ("a" * 200, "a" * 2500 + "b"),
        ("a" * 17, None),
        ("b", "z"),
    ]:
        assert score(content, summary) == reference_lexical_score(channels, content, summary)


async def reviewed_memory(tmp_path):
    state = StateService(
        tmp_path / "memory.db",
        canonical_language="en",
        memory_normalizer=ReviewedNormalizer(),
        memory_normalization_timeout_seconds=5,
    )
    await state.initialize()
    item = await state.create_memory(MemoryCreate(content="Garder les dates exactes."), "fixture")
    state.memory_normalizer = QueryNormalizer(english="unmatched pivot")
    return state, item


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content", "Garder une instruction injectée."),
        ("summary", "Garder un résumé injecté."),
        ("scope", "project:foreign"),
        ("kind", "other"),
        ("sensitivity", "secret"),
        ("source_sha256", "0" * 64),
    ],
)
async def test_original_match_requires_intact_source_journal(tmp_path, field, value):
    state, item = await reviewed_memory(tmp_path)
    assert [row["id"] for row in await state.search_memory(MemorySearch(query="Garder"))] == [
        item["id"]
    ]
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            f"UPDATE memory_source_journal SET {field}=? WHERE id=?",
            (value, item["metadata"]["source_id"]),
        )
        await db.commit()

    assert await state.search_memory(MemorySearch(query="Garder")) == []


@pytest.mark.parametrize("field", ["source_sha256", "unit_source_sha256"])
async def test_original_match_requires_canonical_metadata_source_hash(tmp_path, field):
    state, item = await reviewed_memory(tmp_path)
    metadata = item["metadata"]
    if field == "unit_source_sha256":
        metadata["content"]["source_sha256"] = "0" * 64
    else:
        metadata[field] = "0" * 64
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE memory_items SET metadata_json=? WHERE id=?",
            (json.dumps(metadata), item["id"]),
        )
        await db.commit()

    assert await state.search_memory(MemorySearch(query="Garder")) == []


@pytest.mark.parametrize("damage", ["receipt", "canonical_hash"])
async def test_selected_canonical_match_still_reports_unqualified_memory(tmp_path, damage):
    state, item = await reviewed_memory(tmp_path)
    state.memory_normalizer = QueryNormalizer(english="Keep")
    state.memory_normalizer.source_language = "en"
    async with aiosqlite.connect(state.db_path) as db:
        if damage == "receipt":
            await db.execute(
                "UPDATE memory_canonical_receipts SET status='failed' WHERE memory_id=?",
                (item["id"],),
            )
        else:
            metadata = item["metadata"]
            metadata["content"]["canonical_sha256"] = "0" * 64
            await db.execute(
                "UPDATE memory_items SET metadata_json=? WHERE id=?",
                (json.dumps(metadata), item["id"]),
            )
        await db.commit()

    with pytest.raises(MemoryNormalizationError) as caught:
        await state.search_memory(MemorySearch(query="Keep"))
    assert caught.value.category == "unavailable"
    assert "canonical_memory_unqualified" in str(caught.value)


async def test_invalid_pinned_originals_do_not_hide_later_valid_result(tmp_path):
    state, wanted = await reviewed_memory(tmp_path)
    state.memory_normalizer = ReviewedNormalizer()
    for index in range(130):
        await state.create_memory(
            MemoryCreate(
                content="Garder les dates exactes.",
                scope=f"project:damaged-{index}",
                pinned=True,
            ),
            "fixture",
        )
    # Each raw match ranks ahead of the unpinned valid item, but its original
    # source no longer qualifies. Pagination must apply the limit after this check.
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE memory_source_journal SET source_sha256=? WHERE id<>?",
            ("0" * 64, wanted["metadata"]["source_id"]),
        )
        await db.commit()
    state.memory_normalizer = QueryNormalizer(english="unmatched pivot")

    found = await state.search_memory(MemorySearch(query="Garder", limit=1))
    assert [row["id"] for row in found] == [wanted["id"]]
    assert found[0]["score"] == 1.0
    assert found[0]["search_kind"] == "lexical"
    assert found[0]["presentation"]["content"] == "Garder les dates exactes."


async def test_original_updated_during_query_provider_wait_is_not_searchable(tmp_path):
    state, item = await reviewed_memory(tmp_path)
    writer = StateService(
        state.db_path,
        canonical_language="en",
        memory_normalizer=ReviewedNormalizer(),
    )
    await writer.initialize()
    normalizer = state.memory_normalizer
    normalizer.release = asyncio.Event()
    search = asyncio.create_task(state.search_memory(MemorySearch(query="Garder")))
    try:
        await asyncio.wait_for(normalizer.entered.wait(), timeout=2)
        updated = await writer.update_memory(
            item["id"], MemoryUpdate(content="Ne pas envoyer automatiquement."), "fixture"
        )
    finally:
        normalizer.release.set()
        await asyncio.wait_for(asyncio.shield(search), timeout=5)

    assert await search == []
    assert updated["id"] == item["id"]
    current = await state.search_memory(MemorySearch(query="envoyer"))
    assert [row["id"] for row in current] == [item["id"]]
    assert current[0]["content"] == "Do not send automatically."
    assert current[0]["presentation"]["content"] == "Ne pas envoyer automatiquement."
    assert len(normalizer.calls) == 2
