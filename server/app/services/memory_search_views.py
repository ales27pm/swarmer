"""Read-only, paged lexical candidates from current native and pivot text.

SQL applies authorization before scoring. Its score is an upper bound until the
matching views and source receipts are qualified in bounded pages. This is a
substring scan, not an FTS index, and retains the established lexical score.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite

from app.services.memory_search_presentation import _qualified
from app.services.memory_text_views import _hash, text_view_sha256

PAGE_SIZE = 64
FILTER_SQL = """(? IS NULL OR m.scope=?) AND (? IS NULL OR m.kind=?)
    AND (? IS NULL OR m.scope IN (SELECT value FROM json_each(?)))
    AND (? IS NULL OR m.sensitivity=?)"""
_PAYLOAD_FIELDS = (
    "role",
    "language",
    "pipeline_signature",
    "content",
    "summary",
    "source_id",
    "source_sha256",
    "scope",
    "kind",
    "sensitivity",
)


@dataclass(frozen=True)
class SearchMemory:
    row: aiosqlite.Row
    original: tuple[str, str | None] | None
    # Rechecked after presentation I/O; no private provenance is added to API rows.
    view_token: tuple[Any, ...] | None


def lexical_scorer(
    term_channels: list[tuple[str, ...]],
) -> Callable[[str | None, str | None], float]:
    terms = set().union(*term_channels)
    complete_channels = [set(channel) for channel in term_channels if channel]
    # Small queries are faster with CPython's substring search. For larger
    # term sets the regexp consumes only each candidate first character, so
    # overlapping matches remain visible. The longest suffix at that position
    # also proves every query term that is its prefix.
    pattern = None
    prefixes: dict[str, set[str]] = {}
    if len(terms) > 16:
        by_first: dict[str, list[str]] = {}
        for term in sorted(terms, key=lambda value: (-len(value), value)):
            by_first.setdefault(term[0], []).append(term)
        pattern = re.compile(
            "|".join(
                re.escape(first) + "(?=(" + "|".join(re.escape(term[1:]) for term in group) + "))"
                for first, group in by_first.items()
            )
        )
        prefixes = {
            term: {other for other in by_first[term[0]] if term.startswith(other)} for term in terms
        }

    def score(content: str | None, summary: str | None) -> float:
        if content is None:
            return 0.0
        haystack = f"{content} {summary or ''}".casefold()
        if pattern is None:
            found = {term for term in terms if term in haystack}
        else:
            found = set()
            matched_work = 0
            for match in pattern.finditer(haystack):
                assert match.lastindex is not None
                matched = prefixes[match.group(0) + match.group(match.lastindex)]
                found.update(matched)
                if any(channel <= found for channel in complete_channels):
                    return 1.0
                # Repeated common words and nested prefixes favor substring
                # checks. Bound regexp match work, then finish every remaining
                # term exactly; this is an algorithm switch, not a score cutoff.
                matched_work += len(matched)
                if matched_work >= 16:
                    found.update(term for term in terms - found if term in haystack)
                    break
        return max(
            (
                sum(term in found for term in channel) / max(1, len(channel))
                for channel in term_channels
            ),
            default=0.0,
        )

    return score


async def _rows(db: aiosqlite.Connection, sql: str, ids: list[str]) -> list[aiosqlite.Row]:
    async with db.execute(sql, (json.dumps(ids),)) as cursor:
        return list(await cursor.fetchall())


async def qualify_search_rows(
    db: aiosqlite.Connection,
    rows: list[aiosqlite.Row],
) -> dict[str, SearchMemory]:
    """Qualify only candidate pages, never hash every unrelated stored memory.

    Absent heads and views retain pre-migration reads. A present head requires its
    complete current view set. Invalid canonical receipts disable native matching;
    existing canonical-text hits still reach the established final qualification
    error instead of silently disappearing.
    """
    if not rows:
        return {}
    ids = [row["id"] for row in rows]
    heads = {
        row["memory_id"]: row
        for row in await _rows(
            db,
            "SELECT * FROM memory_text_heads WHERE memory_id IN (SELECT value FROM json_each(?))",
            ids,
        )
    }
    views: dict[str, list[aiosqlite.Row]] = {}
    for view in await _rows(
        db,
        """SELECT v.* FROM memory_text_views v
        JOIN memory_text_heads h ON h.memory_id=v.memory_id AND h.revision=v.revision
        WHERE v.memory_id IN (SELECT value FROM json_each(?))""",
        ids,
    ):
        views.setdefault(view["memory_id"], []).append(view)
    orphans = {
        row["memory_id"]
        for row in await _rows(
            db,
            """SELECT DISTINCT v.memory_id
        FROM memory_text_views v WHERE v.memory_id IN (SELECT value FROM json_each(?))
        AND NOT EXISTS (SELECT 1 FROM memory_text_heads h WHERE h.memory_id=v.memory_id)""",
            ids,
        )
    }
    decoded = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
        except (ValueError, TypeError):
            metadata = None
        decoded[row["id"]] = {**dict(row), "metadata": metadata}
    receipt_ids: list[str] = []
    for item in decoded.values():
        metadata = item["metadata"]
        receipt_id = metadata.get("canonical_receipt_id") if isinstance(metadata, dict) else None
        if isinstance(receipt_id, str):
            receipt_ids.append(receipt_id)
    source_ids = [head["source_id"] for head in heads.values() if head["source_id"]]
    receipts = (
        {
            row["id"]: row
            for row in await _rows(
                db,
                "SELECT * FROM memory_canonical_receipts WHERE id IN (SELECT value FROM json_each(?))",
                receipt_ids,
            )
        }
        if receipt_ids
        else {}
    )
    sources = (
        {
            row["id"]: row
            for row in await _rows(
                db,
                "SELECT * FROM memory_source_journal WHERE id IN (SELECT value FROM json_each(?))",
                source_ids,
            )
        }
        if source_ids
        else {}
    )
    result = {}
    for row in rows:
        memory_id = row["id"]
        head = heads.get(memory_id)
        if head is None:
            if memory_id not in orphans:
                result[memory_id] = SearchMemory(row, None, None)
            continue
        if head["deleted"] or head["item_revision"] != row["updated_at"]:
            continue
        current = sorted(views.get(memory_id, []), key=lambda view: view["role"] != "original")
        expected_roles = ["original", "canonical"] if head["source_id"] else ["original"]
        if [view["role"] for view in current] != expected_roles:
            continue
        payloads = [{key: view[key] for key in _PAYLOAD_FIELDS} for view in current]
        if any(
            view["id"] != "mtv_" + _hash([memory_id, head["revision"], payload])
            or view["text_sha256"] != text_view_sha256(view["content"], view["summary"])
            or any(view[key] != row[key] for key in ("scope", "kind", "sensitivity"))
            or any(view[key] != head[key] for key in ("source_id", "source_sha256"))
            for view, payload in zip(current, payloads, strict=True)
        ):
            continue
        original, selected = current[0], current[-1]
        if (
            head["view_set_sha256"] != _hash(payloads)
            or head["index_view_id"] != selected["id"]
            or selected["content"] != row["content"]
            or selected["summary"] != row["summary"]
            or original["pipeline_signature"] != "original-v1"
            or not 1 <= len(original["language"]) <= 64
            or original["source_sha256"] != original["text_sha256"]
            or (
                head["source_id"]
                and (selected["language"] != "en" or not selected["pipeline_signature"])
            )
        ):
            continue
        native: tuple[str, str | None] | None = (original["content"], original["summary"])
        if head["source_id"]:
            item = decoded[memory_id]
            metadata = item["metadata"] if isinstance(item["metadata"], dict) else {}
            source = sources.get(head["source_id"])
            receipt_id = metadata.get("canonical_receipt_id")
            receipt = receipts.get(receipt_id) if isinstance(receipt_id, str) else None
            if not _qualified(item, receipt, source):
                native = None
            else:
                assert source is not None
                languages = {
                    metadata[field]["source_language"]
                    for field in ("content", "summary")
                    if source[field] is not None
                }
                language = next(iter(languages)) if len(languages) == 1 else "und"
                if (
                    selected["pipeline_signature"] != metadata.get("normalization_signature")
                    or any(
                        original[key] != source[key]
                        for key in ("content", "summary", "source_sha256")
                    )
                    or original["language"] != language
                ):
                    native = None
        result[memory_id] = SearchMemory(
            row,
            native,
            (
                head["revision"],
                head["item_revision"],
                head["view_set_sha256"],
                head["index_view_id"],
            ),
        )
    return result


async def lexical_candidates(
    db: aiosqlite.Connection,
    *,
    filters: tuple[Any, ...],
    term_channels: list[tuple[str, ...]],
    limit: int = 50,
) -> tuple[list[tuple[SearchMemory, float]], dict[str, SearchMemory]]:
    """Rank one row per memory, then qualify until top K is provably complete.

    The cursor holds one ordered SQL scan. Invalid or lowered native scores cannot
    use up the accepted-result budget, including when they fill several pages.
    """
    score = lexical_scorer(term_channels)

    def upper_score(
        content: str, summary: str | None, native: str | None, native_summary: str | None
    ) -> float:
        current_score = score(content, summary)
        if current_score == 1.0 or (content, summary) == (native, native_summary):
            return current_score
        return max(current_score, score(native, native_summary))

    await db.create_function(
        "memory_lexical_score",
        4,
        upper_score,
        deterministic=True,
    )
    accepted: list[tuple[SearchMemory, float]] = []
    qualified: dict[str, SearchMemory] = {}

    def rank(hit: tuple[SearchMemory, float]) -> tuple[float, bool, str]:
        item, value = hit
        return -value, not item.row["pinned"], item.row["id"]

    # The first materialization prevents unauthorized text reaching the UDF;
    # the second computes each upper bound once and sorts only compact rows.
    async with db.execute(
        f"""WITH scoped AS MATERIALIZED (
        SELECT m.id FROM memory_items m WHERE {FILTER_SQL}
    ), scored AS MATERIALIZED (
        SELECT m.id,m.pinned,memory_lexical_score(m.content,m.summary,v.content,v.summary) AS score
        FROM scoped s JOIN memory_items m ON m.id=s.id
        LEFT JOIN memory_text_heads h ON h.memory_id=m.id
        LEFT JOIN memory_text_views v ON v.memory_id=m.id AND v.revision=h.revision AND v.role='original'
        WHERE h.memory_id IS NULL OR (h.deleted=0 AND h.item_revision=m.updated_at)
    ) SELECT * FROM scored WHERE score>0 ORDER BY score DESC,pinned DESC,id""",
        filters,
    ) as cursor:
        while page := list(await cursor.fetchmany(PAGE_SIZE)):
            items = await _rows(
                db,
                "SELECT * FROM memory_items WHERE id IN (SELECT value FROM json_each(?))",
                [row["id"] for row in page],
            )
            current = await qualify_search_rows(db, items)
            for candidate in page:
                item = current.get(candidate["id"])
                if item is None:
                    continue
                value = upper_score(
                    item.row["content"], item.row["summary"], *(item.original or (None, None))
                )
                if value:
                    accepted.append((item, value))
            accepted = sorted(accepted, key=rank)[:limit]
            qualified = {item.row["id"]: item for item, _ in accepted}
            last = page[-1]
            if len(accepted) == limit and (-last["score"], not last["pinned"], last["id"]) >= rank(
                accepted[-1]
            ):
                break
    return accepted, qualified
