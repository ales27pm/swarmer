from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.services.context_builder import safe_context_text
from app.services.embedding_service import EmbeddingService, EmbeddingServiceError

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_OUTCOME = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_SUCCESS_OUTCOMES = frozenset({"completed", "success", "succeeded"})
_FAILURE_OUTCOMES = frozenset(
    {"failed", "failure", "budget_exhausted", "dead_lettered", "quarantined"}
)
_MAX_OBJECTIVE_CHARS = 800
_MAX_PLAN_CHARS = 1_200
_MAX_STEP_SUMMARY_CHARS = 512
_MAX_STEPS = 128
_MAX_TAGS = 64
_MAX_SEARCH_CANDIDATES = 500

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL UNIQUE,
    root_task_id TEXT NOT NULL,
    objective_summary TEXT NOT NULL,
    plan_summary TEXT NOT NULL,
    outcome TEXT NOT NULL,
    score REAL,
    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
    worker_types_json TEXT NOT NULL,
    failure_tags_json TEXT NOT NULL,
    user_feedback_score REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(root_task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_episodes_goal_created
    ON episodes(goal_run_id, created_at DESC, id ASC);
CREATE INDEX IF NOT EXISTS idx_episodes_outcome_created
    ON episodes(outcome, created_at DESC, id ASC);
CREATE TABLE IF NOT EXISTS episode_steps (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    node_type TEXT NOT NULL,
    skill TEXT,
    agent_id TEXT,
    input_summary TEXT NOT NULL,
    output_summary TEXT NOT NULL,
    result_status TEXT NOT NULL,
    latency_ms INTEGER,
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
    UNIQUE(episode_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_episode_steps_episode
    ON episode_steps(episode_id, sequence ASC);
CREATE TABLE IF NOT EXISTS episode_embeddings (
    episode_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK(dimensions > 0),
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(episode_id, provider),
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
"""


@dataclass(frozen=True, slots=True)
class EpisodeStepInput:
    node_type: str
    input_summary: str
    output_summary: str
    result_status: str
    skill: str | None = None
    agent_id: str | None = None
    latency_ms: int | None = None


type EpisodeStepLike = EpisodeStepInput | Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EpisodeStepRecord:
    id: str
    episode_id: str
    sequence: int
    node_type: str
    skill: str | None
    agent_id: str | None
    input_summary: str
    output_summary: str
    result_status: str
    latency_ms: int | None


@dataclass(frozen=True, slots=True)
class EpisodeRecord:
    id: str
    goal_run_id: str
    root_task_id: str
    objective_summary: str
    plan_summary: str
    outcome: str
    score: float | None
    duration_ms: int
    worker_types: tuple[str, ...]
    failure_tags: tuple[str, ...]
    user_feedback_score: float | None
    created_at: str
    updated_at: str
    steps: tuple[EpisodeStepRecord, ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["worker_types"] = list(self.worker_types)
        value["failure_tags"] = list(self.failure_tags)
        value["steps"] = [asdict(step) for step in self.steps]
        return value


@dataclass(frozen=True, slots=True)
class EpisodeSearchResult:
    episode: EpisodeRecord
    score: float
    search_kind: str
    components: Mapping[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode": self.episode.as_dict(),
            "score": self.score,
            "search_kind": self.search_kind,
            "components": dict(self.components),
        }


class EpisodeConflict(RuntimeError):
    """A goal already owns a different immutable episode."""


class EpisodeMemoryService:
    """Durable, summary-only episodic memory with optional semantic ranking.

    SQLite remains authoritative. Embeddings are disposable projections; an
    unavailable, malformed, or dimension-incompatible provider always degrades
    to lexical ranking without losing the recorded episode.
    """

    def __init__(
        self,
        db_path: Path,
        embedding_service: EmbeddingService | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self.embedding_service = embedding_service
        self.clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def record_episode(
        self,
        *,
        goal_run_id: str,
        root_task_id: str,
        objective_summary: str,
        plan_summary: str,
        outcome: str,
        steps: Sequence[EpisodeStepLike] = (),
        score: float | None = None,
        duration_ms: int = 0,
        worker_types: Sequence[str] = (),
        failure_tags: Sequence[str] = (),
        user_feedback_score: float | None = None,
        created_at: datetime | None = None,
    ) -> EpisodeRecord:
        goal_run_id = _validated_identifier(goal_run_id, "goal_run_id")
        root_task_id = _validated_identifier(root_task_id, "root_task_id")
        outcome = _validated_outcome(outcome)
        normalized_steps = _normalize_steps(steps)
        normalized_workers = _normalize_identifiers(worker_types, "worker_types")
        normalized_failures = _normalize_identifiers(failure_tags, "failure_tags")
        score = _optional_bounded_float(score, "score", minimum=0.0, maximum=1.0)
        user_feedback_score = _optional_bounded_float(
            user_feedback_score,
            "user_feedback_score",
            minimum=0.0,
            maximum=5.0,
        )
        if type(duration_ms) is not int or duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer")
        timestamp = _utc_iso(created_at or self.clock())
        episode_id = f"ep_{uuid4().hex}"
        safe_objective = safe_context_text(objective_summary, max_chars=_MAX_OBJECTIVE_CHARS)
        safe_plan = safe_context_text(plan_summary, max_chars=_MAX_PLAN_CHARS)
        if not safe_objective:
            raise ValueError("objective_summary must contain safe text")
        if not safe_plan:
            raise ValueError("plan_summary must contain safe text")

        existing: EpisodeRecord | None = None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing_row = await (
                    await db.execute(
                        "SELECT * FROM episodes WHERE goal_run_id=?",
                        (goal_run_id,),
                    )
                ).fetchone()
                if existing_row is not None:
                    existing = _episode_from_row(
                        existing_row,
                        await self._steps_locked(db, str(existing_row["id"])),
                    )
                else:
                    await db.execute(
                        """
                        INSERT INTO episodes(
                            id,goal_run_id,root_task_id,objective_summary,plan_summary,
                            outcome,score,duration_ms,worker_types_json,failure_tags_json,
                            user_feedback_score,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            episode_id,
                            goal_run_id,
                            root_task_id,
                            safe_objective,
                            safe_plan,
                            outcome,
                            score,
                            duration_ms,
                            _json_array(normalized_workers),
                            _json_array(normalized_failures),
                            user_feedback_score,
                            timestamp,
                            timestamp,
                        ),
                    )
                    for sequence, step in enumerate(normalized_steps, start=1):
                        await db.execute(
                            """
                            INSERT INTO episode_steps(
                                id,episode_id,sequence,node_type,skill,agent_id,
                                input_summary,output_summary,result_status,latency_ms
                            ) VALUES(?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                f"eps_{uuid4().hex}",
                                episode_id,
                                sequence,
                                step.node_type,
                                step.skill,
                                step.agent_id,
                                step.input_summary,
                                step.output_summary,
                                step.result_status,
                                step.latency_ms,
                            ),
                        )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

        if existing is not None:
            if not _same_episode_semantics(
                existing,
                root_task_id=root_task_id,
                objective_summary=safe_objective,
                plan_summary=safe_plan,
                outcome=outcome,
                score=score,
                duration_ms=duration_ms,
                worker_types=normalized_workers,
                failure_tags=normalized_failures,
                user_feedback_score=user_feedback_score,
                steps=normalized_steps,
            ):
                raise EpisodeConflict("goal run already owns a different episode")
            episode_id = existing.id

        if self.embedding_service is not None:
            try:
                await self._index_episode(episode_id)
            except (EmbeddingServiceError, TypeError, ValueError):
                # The embedding is a rebuildable projection. The summary-only
                # authoritative episode must survive provider failure.
                pass
        record = await self.get_episode(episode_id)
        if record is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("episode disappeared after recording")
        return record

    async def get_episode(self, episode_id: str) -> EpisodeRecord | None:
        _validated_identifier(episode_id, "episode_id")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                row = await (
                    await db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,))
                ).fetchone()
                if row is None:
                    return None
                steps = await self._steps_locked(db, episode_id)
            finally:
                await db.rollback()
        return _episode_from_row(row, steps)

    async def set_user_feedback_score(
        self,
        goal_run_id: str,
        score: float,
    ) -> EpisodeRecord | None:
        """Attach server-observed user feedback without rewriting the trajectory."""

        goal_run_id = _validated_identifier(goal_run_id, "goal_run_id")
        normalized_score = _optional_bounded_float(
            score,
            "user_feedback_score",
            minimum=0.0,
            maximum=5.0,
        )
        if normalized_score is None:  # pragma: no cover - score is not optional here
            raise RuntimeError("feedback score normalization failed")
        now = _utc_iso(self.clock())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    UPDATE episodes SET user_feedback_score=?,updated_at=?
                    WHERE goal_run_id=?
                    """,
                    (normalized_score, now, goal_run_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if cursor.rowcount != 1:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute("SELECT id FROM episodes WHERE goal_run_id=?", (goal_run_id,))
            ).fetchone()
        return await self.get_episode(str(row[0])) if row is not None else None

    async def search(
        self,
        query: str,
        *,
        skills: Sequence[str] = (),
        preferred_outcome: str | None = None,
        outcomes: Sequence[str] | None = None,
        limit: int = 10,
    ) -> list[EpisodeSearchResult]:
        safe_query = safe_context_text(query, max_chars=512)
        if not safe_query:
            raise ValueError("query must contain safe text")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        normalized_skills = frozenset(_normalize_identifiers(skills, "skills"))
        preferred = _validated_outcome(preferred_outcome) if preferred_outcome else None
        outcome_filter = (
            frozenset(_validated_outcome(value) for value in outcomes)
            if outcomes is not None
            else None
        )
        query_vector = await self._query_vector(safe_query)
        provider = self.embedding_service.provider_name if self.embedding_service else None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                rows = await (
                    await db.execute(
                        """
                        SELECT e.*,x.vector_json,x.dimensions
                        FROM episodes AS e
                        LEFT JOIN episode_embeddings AS x
                          ON x.episode_id=e.id AND x.provider=?
                        ORDER BY e.created_at DESC,e.id ASC LIMIT ?
                        """,
                        (provider or "", _MAX_SEARCH_CANDIDATES),
                    )
                ).fetchall()
                step_map: dict[str, tuple[EpisodeStepRecord, ...]] = {}
                for row in rows:
                    episode_id = str(row["id"])
                    step_map[episode_id] = await self._steps_locked(db, episode_id)
            finally:
                await db.rollback()

        now = self.clock()
        if now.tzinfo is None:
            raise RuntimeError("episode memory clock must be timezone-aware")
        query_terms = _terms(safe_query)
        results: list[EpisodeSearchResult] = []
        for row in rows:
            outcome = str(row["outcome"])
            if outcome_filter is not None and outcome not in outcome_filter:
                continue
            episode = _episode_from_row(row, step_map[str(row["id"])])
            lexical_score = _lexical_score(query_terms, _episode_search_text(episode))
            semantic_score = _stored_similarity(
                query_vector,
                row["vector_json"],
                row["dimensions"],
            )
            relevance = semantic_score if semantic_score is not None else lexical_score
            skill_score = _overlap_score(normalized_skills, _episode_skills(episode))
            outcome_score = _outcome_score(outcome, preferred)
            recency_score = _recency_score(episode.created_at, now)
            observed_score = episode.score if episode.score is not None else 0.5
            feedback_score = (
                episode.user_feedback_score / 5.0
                if episode.user_feedback_score is not None
                else 0.5
            )
            components = {
                "relevance": round(relevance, 8),
                "skill_overlap": round(skill_score, 8),
                "outcome": round(outcome_score, 8),
                "recency": round(recency_score, 8),
                "observed_score": round(observed_score, 8),
                "user_feedback": round(feedback_score, 8),
            }
            total = (
                0.45 * relevance
                + 0.20 * skill_score
                + 0.15 * outcome_score
                + 0.10 * recency_score
                + 0.05 * observed_score
                + 0.05 * feedback_score
            )
            results.append(
                EpisodeSearchResult(
                    episode=episode,
                    score=round(total, 8),
                    search_kind="hybrid" if semantic_score is not None else "lexical",
                    components=components,
                )
            )
        results.sort(key=lambda item: (-item.score, item.episode.id))
        return results[:limit]

    async def _index_episode(self, episode_id: str) -> None:
        if self.embedding_service is None:
            return
        episode = await self.get_episode(episode_id)
        if episode is None:
            return
        text = _episode_search_text(episode)
        vectors = await self.embedding_service.embed([text])
        if len(vectors) != 1:
            raise ValueError("embedding provider returned an invalid vector count")
        vector = _validated_vector(vectors[0])
        now = _utc_iso(self.clock())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                """
                INSERT OR REPLACE INTO episode_embeddings(
                    episode_id,provider,dimensions,vector_json,updated_at
                ) VALUES(?,?,?,?,?)
                """,
                (
                    episode_id,
                    self.embedding_service.provider_name,
                    len(vector),
                    json.dumps(vector, separators=(",", ":")),
                    now,
                ),
            )
            await db.commit()

    async def _query_vector(self, query: str) -> list[float] | None:
        if self.embedding_service is None:
            return None
        try:
            vectors = await self.embedding_service.embed([query])
            if len(vectors) != 1:
                return None
            return _validated_vector(vectors[0])
        except (EmbeddingServiceError, TypeError, ValueError):
            return None

    @staticmethod
    async def _steps_locked(
        db: aiosqlite.Connection,
        episode_id: str,
    ) -> tuple[EpisodeStepRecord, ...]:
        rows = await (
            await db.execute(
                "SELECT * FROM episode_steps WHERE episode_id=? ORDER BY sequence ASC",
                (episode_id,),
            )
        ).fetchall()
        return tuple(_step_from_row(row) for row in rows)


def _normalize_steps(values: Sequence[EpisodeStepLike]) -> tuple[EpisodeStepInput, ...]:
    if len(values) > _MAX_STEPS:
        raise ValueError(f"steps may contain at most {_MAX_STEPS} entries")
    return tuple(_normalize_step(value) for value in values)


def _normalize_step(value: EpisodeStepLike) -> EpisodeStepInput:
    if isinstance(value, EpisodeStepInput):
        raw = value
    else:
        allowed = {
            "node_type",
            "skill",
            "agent_id",
            "input_summary",
            "output_summary",
            "result_status",
            "latency_ms",
        }
        if set(value) - allowed:
            raise ValueError("episode step contains unsupported fields")
        required = {"node_type", "input_summary", "output_summary", "result_status"}
        if not required.issubset(value):
            raise ValueError("episode step is missing required fields")
        raw = EpisodeStepInput(
            node_type=_required_string(value["node_type"], "node_type"),
            skill=_optional_string(value.get("skill"), "skill"),
            agent_id=_optional_string(value.get("agent_id"), "agent_id"),
            input_summary=_required_string(value["input_summary"], "input_summary"),
            output_summary=_required_string(value["output_summary"], "output_summary"),
            result_status=_required_string(value["result_status"], "result_status"),
            latency_ms=_optional_int(value.get("latency_ms"), "latency_ms"),
        )
    latency = raw.latency_ms
    if latency is not None and (type(latency) is not int or latency < 0):
        raise ValueError("latency_ms must be a non-negative integer")
    safe_input = safe_context_text(raw.input_summary, max_chars=_MAX_STEP_SUMMARY_CHARS)
    safe_output = safe_context_text(raw.output_summary, max_chars=_MAX_STEP_SUMMARY_CHARS)
    if not safe_input or not safe_output:
        raise ValueError("episode step summaries must contain safe text")
    return EpisodeStepInput(
        node_type=_validated_identifier(raw.node_type, "node_type"),
        skill=_validated_optional_identifier(raw.skill, "skill"),
        agent_id=_validated_optional_identifier(raw.agent_id, "agent_id"),
        input_summary=safe_input,
        output_summary=safe_output,
        result_status=_validated_identifier(raw.result_status, "result_status"),
        latency_ms=latency,
    )


def _episode_from_row(
    row: aiosqlite.Row,
    steps: tuple[EpisodeStepRecord, ...],
) -> EpisodeRecord:
    return EpisodeRecord(
        id=str(row["id"]),
        goal_run_id=str(row["goal_run_id"]),
        root_task_id=str(row["root_task_id"]),
        objective_summary=str(row["objective_summary"]),
        plan_summary=str(row["plan_summary"]),
        outcome=str(row["outcome"]),
        score=float(row["score"]) if row["score"] is not None else None,
        duration_ms=int(row["duration_ms"]),
        worker_types=tuple(_decode_string_array(row["worker_types_json"])),
        failure_tags=tuple(_decode_string_array(row["failure_tags_json"])),
        user_feedback_score=(
            float(row["user_feedback_score"]) if row["user_feedback_score"] is not None else None
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        steps=steps,
    )


def _step_from_row(row: aiosqlite.Row) -> EpisodeStepRecord:
    return EpisodeStepRecord(
        id=str(row["id"]),
        episode_id=str(row["episode_id"]),
        sequence=int(row["sequence"]),
        node_type=str(row["node_type"]),
        skill=str(row["skill"]) if row["skill"] is not None else None,
        agent_id=str(row["agent_id"]) if row["agent_id"] is not None else None,
        input_summary=str(row["input_summary"]),
        output_summary=str(row["output_summary"]),
        result_status=str(row["result_status"]),
        latency_ms=int(row["latency_ms"]) if row["latency_ms"] is not None else None,
    )


def _episode_search_text(episode: EpisodeRecord) -> str:
    parts = [episode.objective_summary, episode.plan_summary, episode.outcome]
    parts.extend(episode.worker_types)
    parts.extend(episode.failure_tags)
    for step in episode.steps:
        parts.extend(
            value
            for value in (
                step.node_type,
                step.skill,
                step.input_summary,
                step.output_summary,
                step.result_status,
            )
            if value
        )
    return " ".join(parts)


def _same_episode_semantics(
    episode: EpisodeRecord,
    *,
    root_task_id: str,
    objective_summary: str,
    plan_summary: str,
    outcome: str,
    score: float | None,
    duration_ms: int,
    worker_types: tuple[str, ...],
    failure_tags: tuple[str, ...],
    user_feedback_score: float | None,
    steps: tuple[EpisodeStepInput, ...],
) -> bool:
    recorded_steps = tuple(
        (
            step.node_type,
            step.skill,
            step.agent_id,
            step.input_summary,
            step.output_summary,
            step.result_status,
            step.latency_ms,
        )
        for step in episode.steps
    )
    requested_steps = tuple(
        (
            step.node_type,
            step.skill,
            step.agent_id,
            step.input_summary,
            step.output_summary,
            step.result_status,
            step.latency_ms,
        )
        for step in steps
    )
    return (
        episode.root_task_id == root_task_id
        and episode.objective_summary == objective_summary
        and episode.plan_summary == plan_summary
        and episode.outcome == outcome
        and episode.score == score
        and episode.duration_ms == duration_ms
        and episode.worker_types == worker_types
        and episode.failure_tags == failure_tags
        and episode.user_feedback_score == user_feedback_score
        and recorded_steps == requested_steps
    )


def _episode_skills(episode: EpisodeRecord) -> frozenset[str]:
    return frozenset(step.skill for step in episode.steps if step.skill is not None)


def _stored_similarity(
    query_vector: list[float] | None,
    encoded: object,
    dimensions: object,
) -> float | None:
    if query_vector is None or encoded is None or dimensions is None:
        return None
    try:
        if type(dimensions) is not int or int(dimensions) != len(query_vector):
            return None
        raw = json.loads(str(encoded))
        if not isinstance(raw, list):
            return None
        vector = _validated_vector(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if len(vector) != len(query_vector):
        return None
    dot = sum(left * right for left, right in zip(query_vector, vector, strict=True))
    qnorm = math.sqrt(sum(value * value for value in query_vector))
    vnorm = math.sqrt(sum(value * value for value in vector))
    if qnorm == 0.0 or vnorm == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (qnorm * vnorm)))


def _validated_vector(raw: Sequence[object]) -> list[float]:
    if not raw:
        raise ValueError("embedding vector must not be empty")
    values: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("embedding vector values must be finite numbers")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("embedding vector values must be finite numbers")
        values.append(converted)
    return values


def _lexical_score(query_terms: frozenset[str], text: str) -> float:
    if not query_terms:
        return 0.0
    candidate = _terms(text)
    return len(query_terms & candidate) / len(query_terms)


def _overlap_score(expected: frozenset[str], actual: frozenset[str]) -> float:
    if not expected:
        return 0.5
    return len(expected & actual) / len(expected)


def _outcome_score(outcome: str, preferred: str | None) -> float:
    if preferred is not None:
        same_class = (
            preferred in _SUCCESS_OUTCOMES
            and outcome in _SUCCESS_OUTCOMES
            or preferred in _FAILURE_OUTCOMES
            and outcome in _FAILURE_OUTCOMES
        )
        return 1.0 if outcome == preferred or same_class else 0.0
    if outcome in _SUCCESS_OUTCOMES:
        return 1.0
    if outcome in _FAILURE_OUTCOMES:
        return 0.0
    return 0.5


def _recency_score(created_at: str, now: datetime) -> float:
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        return 0.0
    age_days = max(0.0, (now.astimezone(UTC) - created.astimezone(UTC)).total_seconds() / 86_400)
    return 1.0 / (1.0 + age_days / 30.0)


def _terms(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[\w.:-]+", value.casefold()))


def _validated_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a safe identifier")
    return value


def _validated_optional_identifier(value: str | None, field: str) -> str | None:
    return _validated_identifier(value, field) if value is not None else None


def _validated_outcome(value: str) -> str:
    if not isinstance(value, str) or not _OUTCOME.fullmatch(value):
        raise ValueError("outcome must be a safe identifier")
    return value


def _normalize_identifiers(values: Sequence[str], field: str) -> tuple[str, ...]:
    if len(values) > _MAX_TAGS:
        raise ValueError(f"{field} may contain at most {_MAX_TAGS} entries")
    normalized = tuple(_validated_identifier(value, field) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} must not contain duplicates")
    return tuple(sorted(normalized))


def _optional_bounded_float(
    value: float | None,
    field: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    converted = float(value)
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return converted


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field)


def _optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    return value


def _decode_string_array(raw: object) -> list[str]:
    value = json.loads(str(raw))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError("episode string array is corrupted")
    return value


def _json_array(values: Sequence[str]) -> str:
    return json.dumps(values, separators=(",", ":"), sort_keys=False)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()
