"""Qualified per-view semantic candidates and explicit truncated rank fusion.

Each semantic stream scans every eligible indexed row in bounded pages. Fusion
then combines only the first ``depth`` distinct memories of each ranked stream;
it makes no claim of exhaustive global RRF ranking beyond that candidate depth.
"""

from __future__ import annotations

import json
import math
from typing import Any

import aiosqlite

from app.services.memory_normalization import MemoryNormalizationError
from app.services.memory_search_views import FILTER_SQL
from app.services.memory_vectors import memory_prepared_cosine, memory_unit, memory_vector
from app.services.memory_view_qualification import SearchMemory, _rows, qualify_search_rows

PAGE_SIZE = 64
ROLES = ("original", "canonical")
RankedHits = list[tuple[SearchMemory, float]]


def _rank(hit: tuple[SearchMemory, float]) -> tuple[float, bool, str]:
    item, score = hit
    return -score, not bool(item.row["pinned"]), str(item.row["id"])


async def semantic_candidates(
    db: aiosqlite.Connection,
    *,
    filters: tuple[Any, ...],
    query_vectors: dict[str, list[float]],
    provider: str,
    limit: int = 50,
) -> tuple[dict[str, RankedHits], dict[str, SearchMemory]]:
    """Read exact current-view vectors within the caller's read transaction.

    Authorization and vector provenance joins precede vector decoding/scoring.
    Qualifying a bounded page before retaining top candidates prevents corrupted
    higher-scoring records from consuming the accepted candidate budget.
    """
    if not db.in_transaction:
        raise RuntimeError("semantic candidates require a read transaction")
    if type(limit) is not int or limit < 1:
        raise ValueError("semantic candidate limit must be positive")
    if set(query_vectors) - set(ROLES):
        raise ValueError("unknown semantic query role")
    rankings: dict[str, RankedHits] = {role: [] for role in ROLES}
    vectors = {
        role: memory_unit(valid)
        for role, value in query_vectors.items()
        if (valid := memory_vector(value)) is not None
    }
    if not vectors or not provider:
        return rankings, {}
    sql = f"""WITH scoped AS MATERIALIZED (
        SELECT m.id FROM memory_items m WHERE {FILTER_SQL}
    ) SELECT e.*,v.role FROM scoped s
    JOIN memory_items m ON m.id=s.id
    JOIN memory_text_heads h ON h.memory_id=m.id
    JOIN memory_view_embeddings e ON e.memory_id=m.id AND e.revision=h.revision
    JOIN memory_text_views v ON v.id=e.view_id AND v.memory_id=e.memory_id
        AND v.revision=e.revision
    WHERE e.provider=? AND v.role IN (SELECT value FROM json_each(?))
        AND h.deleted=0 AND h.item_revision=m.updated_at
        AND e.item_revision=m.updated_at AND e.view_sha256=v.text_sha256
        AND e.pipeline_signature=v.pipeline_signature
        AND e.source_id IS v.source_id AND e.source_id IS h.source_id
        AND e.source_sha256=v.source_sha256 AND e.source_sha256=h.source_sha256
    ORDER BY e.memory_id,e.view_id"""
    async with db.execute(sql, (*filters, provider, json.dumps(list(vectors)))) as cursor:
        cursor.row_factory = aiosqlite.Row
        while page := list(await cursor.fetchmany(PAGE_SIZE)):
            items = await _rows(
                db,
                "SELECT * FROM memory_items WHERE id IN (SELECT value FROM json_each(?))",
                list(dict.fromkeys(str(row["memory_id"]) for row in page)),
            )
            qualified = await qualify_search_rows(db, items)
            for row in page:
                item = qualified.get(row["memory_id"])
                # Canonical provenance failures deliberately retain lexical
                # candidates for final explicit errors, but grant no vectors.
                if item is None or item.view_token is None or item.original is None:
                    continue
                query = vectors[row["role"]]
                if row["dimensions"] != len(query):
                    continue
                try:
                    vector = memory_vector(json.loads(row["vector_json"]), len(query))
                except (ValueError, TypeError):
                    continue
                if vector is None:
                    continue
                similarity = memory_prepared_cosine(query, vector)
                if similarity > 0:
                    rankings[row["role"]].append((item, similarity))
            for role in ROLES:
                rankings[role] = sorted(rankings[role], key=_rank)[:limit]
    return rankings, {str(item.row["id"]): item for hits in rankings.values() for item, _ in hits}


def _public_item(item: SearchMemory) -> dict[str, Any]:
    value = dict(item.row)
    raw = value.pop("metadata_json", None)
    try:
        value["metadata"] = json.loads(raw) if raw else None
    except (ValueError, TypeError) as exc:
        raise MemoryNormalizationError("unavailable", "canonical_memory_unqualified") from exc
    value["pinned"] = bool(value["pinned"])
    return value


def fuse_rankings(
    lexical_hits: RankedHits,
    semantic_rankings: dict[str, RankedHits],
    *,
    k: int = 60,
    depth: int = 50,
) -> list[dict[str, Any]]:
    """Fuse distinct current memories using ranks, never raw cosine magnitudes.

    The normalizer is shared by every result: ``(k+1)/active_stream_count``.
    Thus score stays in [0,1] while preserving the exact truncated RRF order.
    With no semantic evidence, the established lexical overlap score survives.
    """
    if type(k) is not int or k < 1 or type(depth) is not int or depth < 1:
        raise ValueError("rank fusion k and depth must be positive integers")
    if set(semantic_rankings) - set(ROLES):
        raise ValueError("unknown semantic ranking role")
    raw_streams = {"lexical": lexical_hits} | {
        role: semantic_rankings.get(role, []) for role in ROLES
    }
    streams: dict[str, RankedHits] = {}
    snapshots: dict[str, SearchMemory] = {}
    for name, hits in raw_streams.items():
        selected: RankedHits = []
        seen: set[str] = set()
        for item, value in hits:
            if not math.isfinite(value) or value <= 0:
                continue
            memory_id = str(item.row["id"])
            previous = snapshots.get(memory_id)
            if previous is not None and (
                previous.view_token != item.view_token
                or previous.original != item.original
                or dict(previous.row) != dict(item.row)
            ):
                raise MemoryNormalizationError("source_conflict", "memory_search_revision_changed")
            snapshots[memory_id] = item
            if memory_id not in seen:
                selected.append((item, value))
                seen.add(memory_id)
            if len(selected) == depth:
                break
        if selected:
            streams[name] = selected
    if not streams:
        return []
    fused = any(name != "lexical" for name in streams)
    ranks: dict[str, dict[str, int]] = {}
    lexical_scores = {str(item.row["id"]): value for item, value in streams.get("lexical", [])}
    for name, hits in streams.items():
        for rank, (item, _) in enumerate(hits, start=1):
            ranks.setdefault(str(item.row["id"]), {})[name] = rank
    output = []
    for memory_id, contributions in ranks.items():
        semantic = any(role in contributions for role in ROLES)
        lexical = "lexical" in contributions
        raw_score = math.fsum(1 / (k + rank) for rank in contributions.values())
        output.append(
            {
                **_public_item(snapshots[memory_id]),
                "score": min(1.0, (k + 1) * raw_score / len(streams))
                if fused
                else lexical_scores[memory_id],
                "search_kind": "hybrid"
                if semantic and lexical
                else "vector"
                if semantic
                else "lexical",
                "score_kind": "rrf" if fused else "lexical_overlap",
                "ranking": {
                    "algorithm": f"rrf-k{k}-depth{depth}-v1" if fused else "lexical-overlap-v1",
                    "k": k,
                    "depth": depth,
                    "active_streams": list(streams),
                    "ranks": contributions,
                },
            }
        )
    return sorted(output, key=lambda item: (-item["score"], not item["pinned"], item["id"]))
