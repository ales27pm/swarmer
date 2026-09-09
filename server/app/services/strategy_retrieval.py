from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from app.services.context_builder import safe_context_text
from app.services.episode_memory import EpisodeMemoryService, EpisodeSearchResult

_SUCCESS_OUTCOMES = ("completed", "success", "succeeded")
_FAILURE_OUTCOMES = (
    "failed",
    "failure",
    "budget_exhausted",
    "dead_lettered",
    "quarantined",
)
_MAX_HINT_CHARS = 280
_MAX_MEMORY_SCAN = 200


@dataclass(frozen=True, slots=True)
class StrategyHint:
    kind: str
    source_id: str
    text: str
    relevance: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StrategyHints:
    successful: tuple[StrategyHint, ...]
    failures: tuple[StrategyHint, ...]
    memory: tuple[StrategyHint, ...]
    provenance_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "successful": [hint.as_dict() for hint in self.successful],
            "failures": [hint.as_dict() for hint in self.failures],
            "memory": [hint.as_dict() for hint in self.memory],
            "provenance_ids": list(self.provenance_ids),
        }


class StrategyRetrieval:
    """Return compact lessons, never prior executable plans or step lists."""

    def __init__(
        self,
        db_path: Path,
        episode_memory: EpisodeMemoryService,
        *,
        max_success_hints: int = 2,
        max_failure_hints: int = 2,
        max_memory_hints: int = 2,
    ) -> None:
        for name, value in (
            ("max_success_hints", max_success_hints),
            ("max_failure_hints", max_failure_hints),
            ("max_memory_hints", max_memory_hints),
        ):
            if not 0 <= value <= 20:
                raise ValueError(f"{name} must be between 0 and 20")
        self.db_path = db_path
        self.episode_memory = episode_memory
        self.max_success_hints = max_success_hints
        self.max_failure_hints = max_failure_hints
        self.max_memory_hints = max_memory_hints

    async def retrieve(
        self,
        query: str,
        *,
        skills: Sequence[str] = (),
    ) -> StrategyHints:
        safe_query = safe_context_text(query, max_chars=512)
        if not safe_query:
            raise ValueError("query must contain safe text")
        successes = await self._episodes(
            safe_query,
            skills=skills,
            outcomes=_SUCCESS_OUTCOMES,
            limit=self.max_success_hints,
            kind="success",
        )
        failures = await self._episodes(
            safe_query,
            skills=skills,
            outcomes=_FAILURE_OUTCOMES,
            limit=self.max_failure_hints,
            kind="failure",
        )
        memory = await self._memory_hints(safe_query)
        provenance: list[str] = []
        seen: set[str] = set()
        for hint in (*successes, *failures, *memory):
            if hint.source_id not in seen:
                seen.add(hint.source_id)
                provenance.append(hint.source_id)
        return StrategyHints(
            successful=successes,
            failures=failures,
            memory=memory,
            provenance_ids=tuple(provenance),
        )

    async def _episodes(
        self,
        query: str,
        *,
        skills: Sequence[str],
        outcomes: tuple[str, ...],
        limit: int,
        kind: str,
    ) -> tuple[StrategyHint, ...]:
        if limit == 0:
            return ()
        results = await self.episode_memory.search(
            query,
            skills=skills,
            preferred_outcome=outcomes[0],
            outcomes=outcomes,
            limit=limit,
        )
        return tuple(self._episode_hint(result, kind=kind) for result in results)

    @staticmethod
    def _episode_hint(result: EpisodeSearchResult, *, kind: str) -> StrategyHint:
        episode = result.episode
        objective = _compact_lesson(episode.objective_summary)
        if kind == "failure":
            tag_text = ", ".join(episode.failure_tags[:3]) or "prior failure"
            text = f"Avoid {tag_text}; earlier objective: {objective}"
        else:
            text = f"Prior successful objective: {objective}"
        # Deliberately never read or return plan_summary or episode_steps here.
        return StrategyHint(
            kind=kind,
            source_id=episode.id,
            text=safe_context_text(text, max_chars=_MAX_HINT_CHARS),
            relevance=result.score,
        )

    async def _memory_hints(self, query: str) -> tuple[StrategyHint, ...]:
        if self.max_memory_hints == 0:
            return ()
        query_terms = _terms(query)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT id,kind,content,summary,pinned,confidence,updated_at
                    FROM memory_items
                    WHERE sensitivity='normal'
                    ORDER BY pinned DESC,updated_at DESC,id ASC LIMIT ?
                    """,
                    (_MAX_MEMORY_SCAN,),
                )
            ).fetchall()
        ranked: list[tuple[float, str, StrategyHint]] = []
        for row in rows:
            if "plan" in str(row["kind"]).casefold():
                continue
            source = str(row["summary"] or row["content"])
            safe_source = safe_context_text(source, max_chars=_MAX_HINT_CHARS)
            if not safe_source:
                continue
            overlap = _overlap(query_terms, _terms(safe_source))
            pinned_bonus = 0.05 if bool(row["pinned"]) else 0.0
            confidence = _bounded(float(row["confidence"]), 0.0, 1.0)
            relevance = min(1.0, 0.8 * overlap + 0.15 * confidence + pinned_bonus)
            if overlap == 0.0 and not bool(row["pinned"]):
                continue
            hint = StrategyHint(
                kind="memory",
                source_id=str(row["id"]),
                text=_compact_lesson(safe_source),
                relevance=round(relevance, 8),
            )
            ranked.append((-relevance, str(row["id"]), hint))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return tuple(item[2] for item in ranked[: self.max_memory_hints])


def _compact_lesson(value: str) -> str:
    safe = safe_context_text(value, max_chars=_MAX_HINT_CHARS)
    # Collapse list/step formatting. Strategy retrieval exposes a one-line
    # lesson, not a prior plan that a model could replay as instructions.
    safe = re.sub(r"(?:^|\s)(?:\d+[.)]|[-*])\s+", " ", safe)
    safe = re.sub(r"\s+", " ", safe).strip()
    sentence = re.split(r"(?<=[.!?])\s+", safe, maxsplit=1)[0]
    return safe_context_text(sentence, max_chars=_MAX_HINT_CHARS)


def _terms(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[\w.:-]+", value.casefold()))


def _overlap(expected: frozenset[str], actual: frozenset[str]) -> float:
    if not expected:
        return 0.0
    return len(expected & actual) / len(expected)


def _bounded(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
