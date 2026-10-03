"""Read-only, paged lexical candidates from current native and pivot text.

SQL applies authorization before scoring. Its score is an upper bound until the
matching views and source receipts are qualified in bounded pages. This is a
substring scan, not an FTS index, and retains the established lexical score.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import aiosqlite

from app.services.memory_view_qualification import (
    SearchMemory as SearchMemory,  # noqa: PLC0414 - compatibility re-export
)
from app.services.memory_view_qualification import (
    _rows,
)
from app.services.memory_view_qualification import (
    qualify_search_rows as qualify_search_rows,  # noqa: PLC0414 - compatibility re-export
)

PAGE_SIZE = 64
FILTER_SQL = """(? IS NULL OR m.scope=?) AND (? IS NULL OR m.kind=?)
    AND (? IS NULL OR m.scope IN (SELECT value FROM json_each(?)))
    AND (? IS NULL OR m.sensitivity=?)"""


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
