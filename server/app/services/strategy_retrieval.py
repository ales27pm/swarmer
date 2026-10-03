from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import aiosqlite

from app.models import MemorySearch
from app.services.context_builder import safe_context_text
from app.services.episode_memory import EpisodeMemoryService, EpisodeSearchResult
from app.services.memory_display_validation import bound_memory_presentation
from app.services.memory_normalization import MemoryNormalizationError, canonical_text_sha256
from app.services.memory_relevance import (
    general_fact_is_relevant,
    general_fact_may_be_relevant,
    memory_relevance_terms,
)
from app.services.model_request_execution import ModelRequestExecutor

if TYPE_CHECKING:
    from app.services.state_service import StateService

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
    canonical_language: Literal["en"] | None = None
    presentation_language: Literal["fr"] | None = None
    source_revision: str | None = None
    canonical_content_sha256: str | None = None
    canonical_summary_sha256: str | None = None
    canonical_receipt_id: str | None = None
    canonical_metadata_sha256: str | None = None
    scope: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


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
    """Return scoped compact lessons, never prior executable plans or step lists.

    Without a goal, only explicit general/global memories are eligible. A goal
    resolves its persistent project from the server's links; free-form scopes
    and unlinked historical episodes are not promoted to shared knowledge.
    """

    def __init__(
        self,
        db_path: Path,
        episode_memory: EpisodeMemoryService,
        *,
        max_success_hints: int = 2,
        max_failure_hints: int = 2,
        max_memory_hints: int = 2,
        canonical_memory: StateService | None = None,
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
        if canonical_memory is not None and canonical_memory.db_path != db_path:
            raise ValueError("canonical memory must share the strategy database")
        self.canonical_memory = canonical_memory

    async def retrieve(
        self,
        query: str,
        *,
        skills: Sequence[str] = (),
        goal_run_id: str | None = None,
        model_executor: ModelRequestExecutor | None = None,
    ) -> StrategyHints:
        safe_query = safe_context_text(query, max_chars=512)
        if not safe_query:
            raise ValueError("query must contain safe text")
        if goal_run_id is not None and (
            not isinstance(goal_run_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", goal_run_id) is None
        ):
            raise ValueError("goal_run_id must be a stable identifier")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA query_only=ON")
            project_id = await self._project_locked(db, goal_run_id)
        execution: dict[str, Any] = (
            {"model_executor": model_executor} if model_executor is not None else {}
        )
        successes = await self._episodes(
            safe_query,
            skills=skills,
            outcomes=_SUCCESS_OUTCOMES,
            limit=self.max_success_hints,
            kind="success",
            goal_run_id=goal_run_id,
            **execution,
        )
        failures = await self._episodes(
            safe_query,
            skills=skills,
            outcomes=_FAILURE_OUTCOMES,
            limit=self.max_failure_hints,
            kind="failure",
            goal_run_id=goal_run_id,
            **execution,
        )
        memory = await self._memory_hints(safe_query, goal_run_id=goal_run_id, **execution)
        # Retrieval may await different providers between each group. Close the
        # scope over all selected provenance in one final read snapshot; never
        # combine old/new projects or a source moved out of this project.
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                if await self._project_locked(db, goal_run_id) != project_id:
                    return StrategyHints((), (), (), ())
                episode_rows = await (
                    await db.execute(
                        """SELECT e.id FROM episodes e
                        JOIN goal_project_links p ON p.goal_run_id=e.goal_run_id
                        WHERE p.project_id=? AND e.id IN (SELECT value FROM json_each(?))""",
                        (
                            project_id,
                            json.dumps([hint.source_id for hint in (*successes, *failures)]),
                        ),
                    )
                ).fetchall()
                memory_rows = await (
                    await db.execute(
                        """SELECT * FROM memory_items
                        WHERE sensitivity='normal' AND (
                            scope IN ('general','global') OR scope='project:' || ?
                        ) AND id IN (SELECT value FROM json_each(?))""",
                        (project_id, json.dumps([hint.source_id for hint in memory])),
                    )
                ).fetchall()
            finally:
                await db.rollback()
        eligible_episodes = {str(row[0]) for row in episode_rows}
        eligible_memories = {str(row["id"]): dict(row) for row in memory_rows}
        successes = tuple(hint for hint in successes if hint.source_id in eligible_episodes)
        failures = tuple(hint for hint in failures if hint.source_id in eligible_episodes)
        memory = tuple(
            hint
            for hint in memory
            if _memory_still_current(hint, eligible_memories.get(hint.source_id))
        )
        # This receipt describes the final read snapshot, not a durable lease
        # on mutable source rows. Call admission must still fence its own goal
        # revision; changes after return cannot be prevented by a read service.
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

    @staticmethod
    async def _project_locked(db: aiosqlite.Connection, goal_run_id: str | None) -> str | None:
        row = await (
            await db.execute(
                "SELECT project_id FROM goal_project_links WHERE goal_run_id=?", (goal_run_id,)
            )
        ).fetchone()
        return str(row[0]) if row else None

    async def _episodes(
        self,
        query: str,
        *,
        skills: Sequence[str],
        outcomes: tuple[str, ...],
        limit: int,
        kind: str,
        goal_run_id: str | None,
        model_executor: ModelRequestExecutor | None = None,
    ) -> tuple[StrategyHint, ...]:
        if limit == 0 or goal_run_id is None:
            return ()
        results = await self.episode_memory.search(
            query,
            skills=skills,
            preferred_outcome=outcomes[0],
            outcomes=outcomes,
            limit=limit,
            goal_run_id=goal_run_id,
            **({"model_executor": model_executor} if model_executor is not None else {}),
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

    async def _memory_hints(
        self,
        query: str,
        *,
        goal_run_id: str | None,
        model_executor: ModelRequestExecutor | None = None,
    ) -> tuple[StrategyHint, ...]:
        if self.max_memory_hints == 0:
            return ()
        if self.canonical_memory is not None:
            return await self._canonical_memory_hints(
                query,
                goal_run_id=goal_run_id,
                **({"model_executor": model_executor} if model_executor is not None else {}),
            )
        query_terms = _terms(query)
        fact_query_terms = memory_relevance_terms(query)
        rows: list[aiosqlite.Row] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id,scope,kind,content,summary,pinned,confidence,updated_at
                FROM memory_items
                WHERE sensitivity='normal' AND (
                    scope IN ('general','global') OR scope=(
                        SELECT 'project:' || project_id FROM goal_project_links
                        WHERE goal_run_id=?
                    )
                )
                ORDER BY pinned DESC,updated_at DESC,id ASC
                """,
                (goal_run_id,),
            ) as cursor:
                async for row in cursor:
                    source = str(row["summary"] or row["content"])
                    if not general_fact_may_be_relevant(
                        scope=str(row["scope"]),
                        kind=str(row["kind"]),
                        source=source,
                        query_terms=fact_query_terms,
                    ):
                        continue
                    if not general_fact_is_relevant(
                        scope=str(row["scope"]),
                        kind=str(row["kind"]),
                        source=safe_context_text(source, max_chars=_MAX_HINT_CHARS),
                        query_terms=fact_query_terms,
                    ):
                        continue
                    rows.append(row)
                    if len(rows) == _MAX_MEMORY_SCAN:
                        break
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

    async def _canonical_memory_hints(
        self,
        query: str,
        *,
        goal_run_id: str | None,
        model_executor: ModelRequestExecutor | None = None,
    ) -> tuple[StrategyHint, ...]:
        if self.canonical_memory is None or self.canonical_memory.canonical_language != "en":
            raise MemoryNormalizationError("unavailable", "canonical_memory_not_enabled")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA query_only=ON")
            project_id = await self._project_locked(db, goal_run_id)
        scopes = ("general", "global") + ((f"project:{project_id}",) if project_id else ())
        items = await self.canonical_memory.search_memory(
            MemorySearch(query=query, limit=self.max_memory_hints),
            allowed_scopes=scopes,
            required_sensitivity="normal",
            **({"model_executor": model_executor} if model_executor is not None else {}),
        )
        hints: list[StrategyHint] = []
        for item in items:
            if "plan" in str(item["kind"]).casefold():
                continue
            presentation = item.get("presentation")
            metadata = item.get("metadata")
            content_hash = canonical_text_sha256(item["content"])
            summary_hash = (
                canonical_text_sha256(item["summary"]) if item.get("summary") is not None else None
            )
            if (
                not isinstance(metadata, dict)
                or metadata.get("canonical_language") != "en"
                or not isinstance(metadata.get("canonical_receipt_id"), str)
                or item["scope"] not in scopes
                or item["sensitivity"] != "normal"
            ):
                raise MemoryNormalizationError("invalid", "strategy_memory_presentation_invalid")
            if presentation is not None:
                if not bound_memory_presentation(item):
                    raise MemoryNormalizationError(
                        "invalid", "strategy_memory_presentation_invalid"
                    )
                text = presentation.get("summary") or presentation.get("content")
            else:
                # StateService qualifies EN provenance even when the English
                # query needs no temporary French display translation.
                text = item.get("summary") or item["content"]
            if not isinstance(text, str) or not text.strip():
                raise MemoryNormalizationError("invalid", "strategy_memory_presentation_invalid")
            safe = _compact_lesson(text)
            if not safe:
                continue
            hints.append(
                StrategyHint(
                    kind="memory",
                    source_id=item["id"],
                    text=safe,
                    relevance=float(item["score"]),
                    canonical_language="en",
                    presentation_language="fr" if presentation is not None else None,
                    source_revision=item["updated_at"],
                    canonical_content_sha256=content_hash,
                    canonical_summary_sha256=summary_hash,
                    scope=item["scope"],
                    canonical_receipt_id=metadata["canonical_receipt_id"],
                    canonical_metadata_sha256=_metadata_hash(metadata),
                )
            )
        return tuple(hints[: self.max_memory_hints])


def _metadata_hash(metadata: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _memory_still_current(hint: StrategyHint, row: dict[str, Any] | None) -> bool:
    if row is None or "plan" in str(row["kind"]).casefold():
        return False
    if hint.canonical_language is None:
        return _compact_lesson(str(row["summary"] or row["content"])) == hint.text
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
        return bool(
            isinstance(metadata, dict)
            and metadata.get("canonical_language") == "en"
            and row["updated_at"] == hint.source_revision
            and row["scope"] == hint.scope
            and canonical_text_sha256(row["content"]) == hint.canonical_content_sha256
            and (canonical_text_sha256(row["summary"]) if row["summary"] is not None else None)
            == hint.canonical_summary_sha256
            and metadata.get("canonical_receipt_id") == hint.canonical_receipt_id
            and _metadata_hash(metadata) == hint.canonical_metadata_sha256
        )
    except (TypeError, ValueError):
        return False


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
