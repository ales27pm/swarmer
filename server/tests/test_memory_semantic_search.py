"""Real SQLite provenance checks and hand-calculated rank-fusion ablations."""

from __future__ import annotations

import json
import sqlite3

import aiosqlite
import pytest

from app.models import MemoryCreate
from app.services.memory_normalization import MemoryNormalizationError
from app.services.memory_semantic_search import fuse_rankings, semantic_candidates
from app.services.memory_view_qualification import SearchMemory
from app.services.memory_view_vector_schema import initialize_view_vector_schema_locked
from app.services.state_service import StateService
from tests.test_memory_canonical_store import ReviewedNormalizer

PROVIDER = "memory-v3:fixture-space"
UNRESTRICTED = (None,) * 8


async def state_with_memory(tmp_path):
    state = StateService(
        tmp_path / "state.db",
        canonical_language="en",
        memory_normalizer=ReviewedNormalizer(),
    )
    await state.initialize()
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await initialize_view_vector_schema_locked(db)
        await db.commit()
    item = await state.create_memory(
        MemoryCreate(content="Garder les dates exactes.", scope="project:wanted"), "fixture"
    )
    return state, item


async def cache_view(state, memory_id, role, vector):
    """Populate the derived cache from real, public-service-created current views."""
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            """INSERT INTO memory_view_embeddings(
                memory_id,revision,view_id,provider,source_id,source_sha256,
                view_sha256,pipeline_signature,item_revision,dimensions,vector_json,updated_at)
            SELECT v.memory_id,v.revision,v.id,?,v.source_id,v.source_sha256,
                v.text_sha256,v.pipeline_signature,h.item_revision,?,?,h.updated_at
            FROM memory_text_views v JOIN memory_text_heads h
                ON h.memory_id=v.memory_id AND h.revision=v.revision
            WHERE v.memory_id=? AND v.role=?""",
            (PROVIDER, len(vector), json.dumps(vector), memory_id, role),
        )
        await db.commit()


async def search(state, *, vectors=None, filters=UNRESTRICTED, limit=50):
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("BEGIN")
        return await semantic_candidates(
            db,
            filters=filters,
            query_vectors=vectors or {"original": [1.0, 0.0], "canonical": [0.0, 1.0]},
            provider=PROVIDER,
            limit=limit,
        )


async def test_each_query_vector_searches_its_corresponding_view(tmp_path):
    state, item = await state_with_memory(tmp_path)
    await cache_view(state, item["id"], "original", [1.0, 0.0])
    await cache_view(state, item["id"], "canonical", [0.0, 1.0])
    rankings, candidates = await search(state)
    assert list(candidates) == [item["id"]]
    for role in ("original", "canonical"):
        [(found, cosine)] = rankings[role]
        assert found.row["id"] == item["id"] and cosine == 1.0
    native_only, _ = await search(state, vectors={"original": [1.0, 0.0]})
    assert native_only["original"] and native_only["canonical"] == []
    pivot_only, _ = await search(state, vectors={"canonical": [0.0, 1.0]})
    assert pivot_only["canonical"] and pivot_only["original"] == []
    reversed_queries, _ = await search(
        state, vectors={"original": [0.0, 1.0], "canonical": [1.0, 0.0]}
    )
    assert reversed_queries == {"original": [], "canonical": []}


async def test_semantic_only_match_older_than_600_newer_memories_survives(tmp_path):
    state, item = await state_with_memory(tmp_path)
    await cache_view(state, item["id"], "original", [1.0, 0.0])
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            """INSERT INTO memory_items(id,scope,kind,content,created_at,updated_at)
            VALUES(?,'project:wanted','fact','Unrelated recent item','2099','2099')""",
            [(f"newer_{index}",) for index in range(600)],
        )
        await db.commit()
    rankings, _ = await search(state, vectors={"original": [1.0, 0.0]}, limit=1)
    assert [found.row["id"] for found, _ in rankings["original"]] == [item["id"]]
    [result] = fuse_rankings([], rankings)
    assert result["id"] == item["id"] and result["search_kind"] == "vector"


async def test_scoped_valid_result_survives_multiple_pages_of_corrupt_high_vectors(tmp_path):
    state, wanted = await state_with_memory(tmp_path)
    await cache_view(state, wanted["id"], "original", [0.8, 0.6])
    for index in range(130):
        damaged = await state.create_memory(
            MemoryCreate(
                content="Garder les dates exactes.",
                summary=f"Distinct source {index}",
                scope="project:wanted",
                pinned=True,
            ),
            "fixture",
        )
        await cache_view(state, damaged["id"], "original", [1.0, 0.0])
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE memory_source_journal SET source_sha256=? WHERE id<>?",
            ("0" * 64, wanted["metadata"]["source_id"]),
        )
        await db.commit()
    for fields in [
        {"scope": "project:forbidden"},
        {"scope": "project:wanted", "kind": "other"},
        {"scope": "project:wanted", "sensitivity": "secret"},
    ]:
        forbidden = await state.create_memory(
            MemoryCreate(content="Ne pas envoyer automatiquement.", pinned=True, **fields),
            "fixture",
        )
        await cache_view(state, forbidden["id"], "original", [1.0, 0.0])
    allowed = json.dumps(["project:wanted"])
    filters = (
        "project:wanted",
        "project:wanted",
        "fact",
        "fact",
        allowed,
        allowed,
        "normal",
        "normal",
    )
    rankings, candidates = await search(
        state, vectors={"original": [1.0, 0.0]}, filters=filters, limit=1
    )
    [(found, cosine)] = rankings["original"]
    assert found.row["id"] == wanted["id"] and cosine == pytest.approx(0.8)
    assert list(candidates) == [wanted["id"]]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", 999),
        ("provider", "different-space"),
        ("source_id", "forged-source"),
        ("source_sha256", "0" * 64),
        ("view_sha256", "0" * 64),
        ("pipeline_signature", "forged-pipeline"),
        ("item_revision", "stale-revision"),
        ("dimensions", 3),
        ("vector_json", "not-json"),
        ("vector_json", "[true, 0]"),
        ("vector_json", "[NaN, 0]"),
        ("vector_json", "[0, 0]"),
    ],
)
async def test_vector_binding_or_numeric_damage_never_grants_semantic_evidence(
    tmp_path, field, value
):
    state, item = await state_with_memory(tmp_path)
    await cache_view(state, item["id"], "original", [1.0, 0.0])
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(f"UPDATE memory_view_embeddings SET {field}=?", (value,))
        await db.commit()
    assert await search(state) == ({"original": [], "canonical": []}, {})


@pytest.mark.parametrize("damage", ["wrong_view", "tombstone", "receipt"])
async def test_invalid_current_source_disables_both_semantic_roles(tmp_path, damage):
    state, item = await state_with_memory(tmp_path)
    await cache_view(state, item["id"], "original", [1.0, 0.0])
    if damage != "wrong_view":
        await cache_view(state, item["id"], "canonical", [0.0, 1.0])
    async with aiosqlite.connect(state.db_path) as db:
        if damage == "wrong_view":
            await db.execute(
                """UPDATE memory_view_embeddings SET view_id=(
                    SELECT id FROM memory_text_views WHERE memory_id=? AND role='canonical')""",
                (item["id"],),
            )
        elif damage == "tombstone":
            await db.execute("UPDATE memory_text_heads SET deleted=1")
        else:
            await db.execute("UPDATE memory_canonical_receipts SET status='failed'")
        await db.commit()
    assert await search(state) == ({"original": [], "canonical": []}, {})


async def test_old_single_vector_cache_cannot_invent_per_view_vectors(tmp_path):
    state, item = await state_with_memory(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            """INSERT INTO memory_embeddings(memory_id,provider,dimensions,vector_json,updated_at)
            VALUES(?,?,2,'[1,0]',?)""",
            (item["id"], PROVIDER, item["updated_at"]),
        )
        await db.commit()
    assert await search(state) == ({"original": [], "canonical": []}, {})


def memory(memory_id, *, revision=1, pinned=False):
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            """SELECT ? AS id,'general' AS scope,'fact' AS kind,'Content' AS content,
            NULL AS summary,'normal' AS sensitivity,1.0 AS confidence,? AS pinned,
            '{}' AS metadata_json,'created' AS created_at,? AS updated_at""",
            (memory_id, pinned, str(revision)),
        ).fetchone()
    return SearchMemory(row, ("Original", None), (revision, str(revision), "set", "view"))


def test_rrf_consensus_beats_single_channel_maxima_by_hand_calculated_ranks():
    a, b, c, d = (memory(name) for name in "abcd")
    result = fuse_rankings(
        [(a, 1.0), (b, 0.01)],
        {"original": [(c, 1.0), (b, 0.01)], "canonical": [(d, 1.0), (b, 0.01)]},
    )
    assert [row["id"] for row in result] == ["b", "a", "c", "d"]
    assert result[0]["score"] == pytest.approx(61 / 62)
    assert all(row["score"] == pytest.approx(1 / 3) for row in result[1:])
    assert result[0]["ranking"] == {
        "algorithm": "rrf-k60-depth50-v1",
        "k": 60,
        "depth": 50,
        "active_streams": ["lexical", "original", "canonical"],
        "ranks": {"lexical": 2, "original": 2, "canonical": 2},
    }
    assert result[0]["search_kind"] == "hybrid"
    assert result[1]["search_kind"] == "lexical"
    assert result[2]["search_kind"] == result[3]["search_kind"] == "vector"
    assert all(row["score_kind"] == "rrf" for row in result)
    assert all("metadata_json" not in row and row["metadata"] == {} for row in result)


def test_rank_fusion_depends_on_order_not_cosine_magnitude_and_deduplicates():
    a, b = memory("a"), memory("b")
    first = fuse_rankings(
        [(a, 1.0), (a, 0.9), (b, 0.01)],
        {"original": [(b, 1.0), (b, 0.7), (a, 0.01)]},
    )
    second = fuse_rankings([(a, 0.3), (b, 0.2)], {"original": [(b, 0.8), (a, 0.7)]})
    assert first == second
    assert {row["id"] for row in first} == {"a", "b"}
    assert first[0]["ranking"]["ranks"] == {"lexical": 1, "original": 2}


def test_lexical_fallback_retains_overlap_score_and_no_semantic_claim():
    a, b = memory("a"), memory("b")
    result = fuse_rankings([(a, 1.0), (b, 0.25)], {"original": [], "canonical": []})
    assert [(row["id"], row["score"]) for row in result] == [("a", 1.0), ("b", 0.25)]
    assert all(row["search_kind"] == "lexical" for row in result)
    assert all(row["score_kind"] == "lexical_overlap" for row in result)
    assert all(row["ranking"]["algorithm"] == "lexical-overlap-v1" for row in result)
    assert fuse_rankings([], {}) == []


def test_fusion_explicitly_truncates_each_distinct_stream_at_depth_50():
    items = [memory(f"item_{index:02d}") for index in range(51)]
    results = fuse_rankings([(item, 1.0) for item in items], {"original": [(items[-1], 1.0)]})
    last = next(row for row in results if row["id"] == "item_50")
    assert last["ranking"]["ranks"] == {"original": 1}
    assert last["ranking"]["algorithm"] == "rrf-k60-depth50-v1"
    assert last["score"] == 0.5
    assert all(0 < row["score"] <= 1 for row in results)


@pytest.mark.parametrize("change", ["revision", "source_qualification"])
def test_fusion_never_combines_conflicting_snapshots_of_one_memory(change):
    original = memory("same")
    changed = (
        memory("same", revision=2)
        if change == "revision"
        else SearchMemory(original.row, None, original.view_token)
    )
    with pytest.raises(MemoryNormalizationError) as caught:
        fuse_rankings([(original, 1.0)], {"original": [(changed, 1.0)]})
    assert caught.value.category == "source_conflict"


def test_identical_role_rankings_have_declared_votes_but_one_result_per_memory():
    lexical, semantic = memory("lexical"), memory("semantic")
    [winner, other] = fuse_rankings(
        [(lexical, 1.0)],
        {"original": [(semantic, 1.0)], "canonical": [(semantic, 1.0)]},
    )
    assert winner["id"] == "semantic" and winner["score"] == pytest.approx(2 / 3)
    assert winner["search_kind"] == "vector"
    assert winner["ranking"]["ranks"] == {"original": 1, "canonical": 1}
    assert winner["ranking"]["active_streams"] == ["lexical", "original", "canonical"]
    assert other["id"] == "lexical" and other["score"] == pytest.approx(1 / 3)
