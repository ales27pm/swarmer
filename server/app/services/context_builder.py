from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.services.agent_liveness import (
    DEFAULT_AGENT_OFFLINE_TIMEOUT_SECONDS,
    agent_is_fresh,
)
from app.services.feedback_dataset import redact_dataset_text
from app.services.swarm_contracts import EvaluationNodeResult, GoalEvaluationContext

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_WHITESPACE = re.compile(r"\s+")
_ADDITIONAL_SECRET = re.compile(
    r"(?i)(?:\b(?:authorization|auth|password|passwd|secret|private[_ -]?key|"
    r"access[_ -]?key|session[_ -]?id|api[_ -]?key|token|grant|lease[_ -]?token)"
    r"\s*[:=]\s*)(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^,;\n]+)|"
    r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:sk|ghp|github_pat)-?[A-Za-z0-9_-]{12,}\b|"
    r"\bAKIA[A-Z0-9]{16}\b|"
    r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b"
)
_CREDENTIAL_URL = re.compile(r"(?i)\b(?:redis|rediss|https?)://[^/@\s:]+:[^/@\s]+@")
_ADDITIONAL_PATH = re.compile(
    r"(?<![A-Za-z0-9:])/(?:Users|home|root|private|etc|var|tmp|opt|srv)"
    r"(?:/[^\s\"',;]*)?"
)
_GENERIC_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9/:])(?:/(?!/)[^\s\"',;]+|[A-Za-z]:\\[^\s\"',;]+)"
)
_MAX_SAFE_TEXT_CHARS = 4_000

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS goal_contexts (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    root_task_id TEXT NOT NULL,
    node_id TEXT,
    purpose TEXT NOT NULL,
    context_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    approx_token_count INTEGER NOT NULL CHECK(approx_token_count >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(root_task_id) REFERENCES tasks(id),
    FOREIGN KEY(node_id) REFERENCES plan_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_contexts_goal
    ON goal_contexts(goal_run_id, created_at);
"""


@dataclass(frozen=True, slots=True)
class ContextCard:
    card_id: str
    kind: str
    summary: str
    provenance_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["provenance_ids"] = list(self.provenance_ids)
        return value

    def as_model_dict(self) -> dict[str, str]:
        """Return only the bounded material presented to a model."""

        return {
            "card_id": self.card_id,
            "kind": self.kind,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class GoalContext:
    id: str
    goal_run_id: str
    root_task_id: str
    node_id: str | None
    purpose: str
    cards: tuple[ContextCard, ...]
    provenance_ids: tuple[str, ...]
    card_ids: tuple[str, ...]
    approx_token_count: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal_run_id": self.goal_run_id,
            "root_task_id": self.root_task_id,
            "node_id": self.node_id,
            "purpose": self.purpose,
            "cards": [card.as_dict() for card in self.cards],
            "provenance_ids": list(self.provenance_ids),
            "card_ids": list(self.card_ids),
            "approx_token_count": self.approx_token_count,
            "created_at": self.created_at,
        }

    def model_payload(self) -> dict[str, object]:
        """Return the exact payload whose size is recorded for this context."""

        return _card_model_payload(self.purpose, self.cards)


@dataclass(frozen=True, slots=True)
class ModelContextRecord:
    """A persisted structured model payload and its safe provenance."""

    id: str
    goal_run_id: str
    root_task_id: str
    node_id: str | None
    purpose: str
    payload: dict[str, Any]
    provenance_ids: tuple[str, ...]
    approx_token_count: int
    created_at: str

    def model_payload(self) -> dict[str, Any]:
        return dict(self.payload)


class ContextBuilder:
    """Build a bounded, deterministic, summary-only model context from SQLite.

    Authoritative values are loaded by identifier. Explicit attributed additions
    such as strategy hints or user replan guidance cross the same redaction and
    budget boundary before persistence. The context conveys no execution authority.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        max_tokens: int = 2_048,
        max_memory_items: int = 6,
        max_episode_items: int = 4,
        max_agent_cards: int = 6,
        offline_timeout_seconds: int = DEFAULT_AGENT_OFFLINE_TIMEOUT_SECONDS,
        max_upstream_results: int = 8,
        max_result_chars_per_node: int = 2_000,
        max_items: int | None = None,
        max_upstream_chars: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 64 <= max_tokens <= 32_768:
            raise ValueError("max_tokens must be between 64 and 32768")
        if not 0 <= max_memory_items <= 100:
            raise ValueError("max_memory_items must be between 0 and 100")
        if not 0 <= max_episode_items <= 100:
            raise ValueError("max_episode_items must be between 0 and 100")
        if not 0 <= max_agent_cards <= 64:
            raise ValueError("max_agent_cards must be between 0 and 64")
        if offline_timeout_seconds <= 0:
            raise ValueError("agent offline timeout must be positive")
        if not 0 <= max_upstream_results <= 20:
            raise ValueError("max_upstream_results must be between 0 and 20")
        if not 0 <= max_result_chars_per_node <= 100_000:
            raise ValueError("max_result_chars_per_node must be between 0 and 100000")
        if max_items is not None and not 1 <= max_items <= 256:
            raise ValueError("max_items must be between 1 and 256")
        if max_upstream_chars is not None and not 0 <= max_upstream_chars <= 100_000:
            raise ValueError("max_upstream_chars must be between 0 and 100000")
        self.db_path = db_path
        self.max_tokens = max_tokens
        self.max_memory_items = max_memory_items
        self.max_episode_items = max_episode_items
        self.max_agent_cards = max_agent_cards
        self.offline_timeout_seconds = offline_timeout_seconds
        self.max_upstream_results = max_upstream_results
        self.max_result_chars_per_node = max_result_chars_per_node
        # Compatibility-only caps retained for callers of the pre-v0.12 API.
        # The explicit source budgets above remain independently enforced.
        self.max_items = max_items
        self.max_upstream_chars = max_upstream_chars
        self.clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def build(
        self,
        *,
        goal_run_id: str,
        node_id: str | None = None,
        purpose: str = "planner",
        additional_cards: Sequence[ContextCard] = (),
    ) -> GoalContext:
        goal_run_id = _validated_identifier(goal_run_id, "goal_run_id")
        node_id = _validated_optional_identifier(node_id, "node_id")
        purpose = _validated_identifier(purpose, "purpose")
        normalized_additional = _normalized_additional_cards(additional_cards)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                goal = await (
                    await db.execute("SELECT * FROM goal_runs WHERE id=?", (goal_run_id,))
                ).fetchone()
                if goal is None:
                    raise ValueError("goal run not found")
                root_task_id = _validated_identifier(str(goal["root_task_id"]), "root_task_id")
                root = await (
                    await db.execute("SELECT * FROM tasks WHERE id=?", (root_task_id,))
                ).fetchone()
                if root is None:
                    raise ValueError("root task not found")
                node = await self._node_locked(db, goal_run_id, node_id)
                upstream = await self._upstream_locked(
                    db,
                    goal_run_id,
                    node_id,
                    limit=self.max_upstream_results,
                )
                memories = await self._memory_locked(db, limit=self.max_memory_items)
                episodes = await self._episodes_locked(
                    db,
                    goal_run_id,
                    limit=self.max_episode_items,
                )
                agents = await self._agents_locked(db, limit=self.max_agent_cards)
                failures = await self._failures_locked(db, goal_run_id, node_id)
            finally:
                await db.rollback()

        candidates: list[ContextCard] = []
        candidates.append(_goal_card(goal))
        candidates.append(_root_card(root))
        if node is not None:
            candidates.append(_node_card(node, max_result_chars=self.max_result_chars_per_node))
        candidates.append(_constraints_card(goal, node))
        candidates.append(_budgets_card(goal))
        candidates.extend(normalized_additional)
        candidates.extend(_goal_failure_cards(goal))
        candidates.extend(_failure_cards(failures, max_result_chars=self.max_result_chars_per_node))

        upstream_chars = 0
        for row in upstream:
            card = _upstream_card(
                row,
                max_result_chars=self.max_result_chars_per_node,
            )
            if self.max_upstream_chars is None:
                bounded = card.summary
            else:
                remaining = self.max_upstream_chars - upstream_chars
                if remaining <= 0:
                    break
                bounded = safe_context_text(card.summary, max_chars=remaining)
            if not bounded:
                continue
            candidates.append(
                ContextCard(
                    card_id=card.card_id,
                    kind=card.kind,
                    summary=bounded,
                    provenance_ids=card.provenance_ids,
                )
            )
            upstream_chars += len(bounded)

        candidates.extend(_episode_card(row) for row in episodes)
        candidates.extend(_memory_card(row) for row in memories)
        max_non_agent_cards = self.max_items if self.max_items is not None else len(candidates)
        non_agent_cards = self._bounded_cards(
            candidates,
            max_cards=max_non_agent_cards,
            purpose=purpose,
        )
        agent_candidates = [_agent_card(row) for row in agents]
        cards = self._append_with_token_budget(
            non_agent_cards,
            agent_candidates,
            purpose=purpose,
        )
        provenance_ids = _stable_unique(
            provenance for card in cards for provenance in card.provenance_ids
        )
        card_ids = tuple(card.card_id for card in cards)
        context_payload = _card_model_payload(purpose, cards)
        token_count = _payload_tokens(context_payload)
        if token_count > self.max_tokens:  # pragma: no cover - guarded by bounding
            raise RuntimeError("context token budget invariant failed")
        context_id = f"ctx_{uuid4().hex}"
        timestamp = _utc_iso(self.clock())
        provenance_payload = {
            "source_ids": list(provenance_ids),
            "card_ids": list(card_ids),
            "card_provenance": {card.card_id: list(card.provenance_ids) for card in cards},
        }
        encoded_context = _canonical_json(context_payload)
        encoded_provenance = _canonical_json(provenance_payload)
        # Only redacted/bounded cards are encoded; raw task prompts never cross
        # this persistence boundary.
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                """
                INSERT INTO goal_contexts(
                    id,goal_run_id,root_task_id,node_id,purpose,context_json,
                    provenance_json,approx_token_count,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    context_id,
                    goal_run_id,
                    root_task_id,
                    node_id,
                    purpose,
                    encoded_context,
                    encoded_provenance,
                    token_count,
                    timestamp,
                ),
            )
            await db.commit()
        return GoalContext(
            id=context_id,
            goal_run_id=goal_run_id,
            root_task_id=root_task_id,
            node_id=node_id,
            purpose=purpose,
            cards=cards,
            provenance_ids=provenance_ids,
            card_ids=card_ids,
            approx_token_count=token_count,
            created_at=timestamp,
        )

    async def build_for_goal(
        self,
        goal_run_id: str,
        *,
        node_id: str | None = None,
        purpose: str = "planner",
        additional_cards: Sequence[ContextCard] = (),
    ) -> GoalContext:
        return await self.build(
            goal_run_id=goal_run_id,
            node_id=node_id,
            purpose=purpose,
            additional_cards=additional_cards,
        )

    async def build_evaluation_context(
        self,
        context: GoalEvaluationContext,
        *,
        provenance_ids: Sequence[str] = (),
    ) -> tuple[GoalEvaluationContext, ModelContextRecord]:
        """Redact, bound, and persist the exact structured evaluator payload."""

        goal_run_id = _validated_identifier(context.goal_run_id, "goal_run_id")
        bounded = bound_evaluation_context(context, max_tokens=self.max_tokens)
        payload = bounded.model_dump(mode="json")
        token_count = _payload_tokens(payload)
        if token_count > self.max_tokens:  # pragma: no cover - guarded by bounding
            raise RuntimeError("evaluation context token budget invariant failed")
        normalized_provenance = _stable_unique(
            _validated_identifier(item, "source_id") for item in provenance_ids
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT root_task_id FROM goal_runs WHERE id=?",
                    (goal_run_id,),
                )
            ).fetchone()
        if row is None:
            raise ValueError("goal run not found")
        root_task_id = _validated_identifier(str(row["root_task_id"]), "root_task_id")
        context_id = f"ctx_{uuid4().hex}"
        timestamp = _utc_iso(self.clock())
        encoded_context = _canonical_json(payload)
        encoded_provenance = _canonical_json(
            {
                "source_ids": list(normalized_provenance),
                "card_ids": [],
                "payload_kind": "goal_evaluation",
            }
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                """
                INSERT INTO goal_contexts(
                    id,goal_run_id,root_task_id,node_id,purpose,context_json,
                    provenance_json,approx_token_count,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    context_id,
                    goal_run_id,
                    root_task_id,
                    None,
                    "evaluator",
                    encoded_context,
                    encoded_provenance,
                    token_count,
                    timestamp,
                ),
            )
            await db.commit()
        record = ModelContextRecord(
            id=context_id,
            goal_run_id=goal_run_id,
            root_task_id=root_task_id,
            node_id=None,
            purpose="evaluator",
            payload=payload,
            provenance_ids=normalized_provenance,
            approx_token_count=token_count,
            created_at=timestamp,
        )
        return bounded, record

    async def get_record(self, context_id: str) -> ModelContextRecord | None:
        """Load the exact persisted model payload, independent of its shape."""

        context_id = _validated_identifier(context_id, "context_id")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM goal_contexts WHERE id=?", (context_id,))
            ).fetchone()
        if row is None:
            return None
        payload = _strict_object(row["context_json"])
        provenance = _strict_object(row["provenance_json"])
        source_ids = provenance.get("source_ids")
        if not isinstance(source_ids, list) or not all(
            isinstance(item, str) for item in source_ids
        ):
            raise RuntimeError("stored goal context provenance is corrupted")
        return ModelContextRecord(
            id=str(row["id"]),
            goal_run_id=str(row["goal_run_id"]),
            root_task_id=str(row["root_task_id"]),
            node_id=str(row["node_id"]) if row["node_id"] is not None else None,
            purpose=str(row["purpose"]),
            payload=payload,
            provenance_ids=tuple(source_ids),
            approx_token_count=int(row["approx_token_count"]),
            created_at=str(row["created_at"]),
        )

    async def get(self, context_id: str) -> GoalContext | None:
        context_id = _validated_identifier(context_id, "context_id")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM goal_contexts WHERE id=?", (context_id,))
            ).fetchone()
        if row is None:
            return None
        context_payload = _strict_object(row["context_json"])
        provenance_payload = _strict_object(row["provenance_json"])
        raw_cards = context_payload.get("cards")
        if not isinstance(raw_cards, list):
            raise TypeError("stored goal context is structured; use get_record")
        raw_card_provenance = provenance_payload.get("card_provenance", {})
        if not isinstance(raw_card_provenance, dict):
            raise TypeError("stored goal context provenance is corrupted")
        cards: list[ContextCard] = []
        for value in raw_cards:
            if not isinstance(value, dict) or set(value) not in (
                {"card_id", "kind", "summary"},
                {"card_id", "kind", "summary", "provenance_ids"},
            ):
                raise RuntimeError("stored context card is corrupted")
            provenances = value.get(
                "provenance_ids",
                raw_card_provenance.get(str(value["card_id"])),
            )
            if not isinstance(provenances, list) or not all(
                isinstance(item, str) for item in provenances
            ):
                raise RuntimeError("stored context provenance is corrupted")
            cards.append(
                ContextCard(
                    card_id=str(value["card_id"]),
                    kind=str(value["kind"]),
                    summary=str(value["summary"]),
                    provenance_ids=tuple(provenances),
                )
            )
        source_ids = provenance_payload.get("source_ids")
        card_ids = provenance_payload.get("card_ids")
        if (
            not isinstance(source_ids, list)
            or not all(isinstance(item, str) for item in source_ids)
            or not isinstance(card_ids, list)
            or not all(isinstance(item, str) for item in card_ids)
        ):
            raise RuntimeError("stored goal context provenance is corrupted")
        return GoalContext(
            id=str(row["id"]),
            goal_run_id=str(row["goal_run_id"]),
            root_task_id=str(row["root_task_id"]),
            node_id=str(row["node_id"]) if row["node_id"] is not None else None,
            purpose=str(row["purpose"]),
            cards=tuple(cards),
            provenance_ids=tuple(source_ids),
            card_ids=tuple(card_ids),
            approx_token_count=int(row["approx_token_count"]),
            created_at=str(row["created_at"]),
        )

    async def extend_provenance(
        self,
        context_id: str,
        source_ids: Sequence[str],
    ) -> None:
        """Validate legacy provenance additions without creating phantom sources.

        New callers must attach provenance to an ``additional_cards`` entry so
        that a source can only be recorded when its bounded material was sent.
        """

        context_id = _validated_identifier(context_id, "context_id")
        normalized = tuple(_validated_identifier(item, "source_id") for item in source_ids)
        if not normalized:
            return
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT provenance_json FROM goal_contexts WHERE id=?",
                    (context_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise ValueError("goal context not found")
            provenance = _strict_object(row["provenance_json"])
            existing = provenance.get("source_ids")
            card_ids = provenance.get("card_ids")
            if (
                not isinstance(existing, list)
                or not all(isinstance(item, str) for item in existing)
                or not isinstance(card_ids, list)
                or not all(isinstance(item, str) for item in card_ids)
            ):
                await db.rollback()
                raise RuntimeError("stored goal context provenance is corrupted")
            if not set(normalized).issubset(existing):
                await db.rollback()
                raise ValueError(
                    "provenance must be attached to bounded material before persistence"
                )
            await db.rollback()

    def _bounded_cards(
        self,
        candidates: Sequence[ContextCard],
        *,
        max_cards: int,
        purpose: str,
    ) -> tuple[ContextCard, ...]:
        selected = list(candidates[:max_cards])
        while (
            selected
            and _payload_tokens(
                _card_model_payload(purpose, tuple(_minimum_card(card) for card in selected))
            )
            > self.max_tokens
        ):
            selected.pop()
        bounded: list[ContextCard] = []
        for index, candidate in enumerate(selected):
            reserved = tuple(_minimum_card(card) for card in selected[index + 1 :])
            card = _fit_card_for_payload(
                candidate,
                prefix=tuple(bounded),
                suffix=reserved,
                purpose=purpose,
                max_tokens=self.max_tokens,
            )
            if card is None:
                break
            bounded.append(card)
        return tuple(bounded)

    def _append_with_token_budget(
        self,
        current: tuple[ContextCard, ...],
        candidates: Sequence[ContextCard],
        *,
        purpose: str,
    ) -> tuple[ContextCard, ...]:
        bounded = list(current)
        for candidate in candidates:
            card = _fit_card_for_payload(
                candidate,
                prefix=tuple(bounded),
                suffix=(),
                purpose=purpose,
                max_tokens=self.max_tokens,
            )
            if card is None:
                break
            bounded.append(card)
        return tuple(bounded)

    @staticmethod
    async def _node_locked(
        db: aiosqlite.Connection,
        goal_run_id: str,
        node_id: str | None,
    ) -> aiosqlite.Row | None:
        if node_id is None:
            return None
        row = await (
            await db.execute(
                "SELECT * FROM plan_nodes WHERE id=? AND goal_run_id=?",
                (node_id, goal_run_id),
            )
        ).fetchone()
        if row is None:
            raise ValueError("plan node not found in goal run")
        return row

    @staticmethod
    async def _upstream_locked(
        db: aiosqlite.Connection,
        goal_run_id: str,
        node_id: str | None,
        *,
        limit: int,
    ) -> list[aiosqlite.Row]:
        if node_id is None or limit == 0:
            return []
        return list(
            await (
                await db.execute(
                    """
                    SELECT n.*,e.dependency_type
                    FROM plan_edges AS e
                    JOIN plan_nodes AS n ON n.id=e.from_node_id
                    WHERE e.goal_run_id=? AND e.to_node_id=? AND n.status='completed'
                    ORDER BY n.completed_at ASC,n.id ASC
                    LIMIT ?
                    """,
                    (goal_run_id, node_id, limit),
                )
            ).fetchall()
        )

    @staticmethod
    async def _memory_locked(
        db: aiosqlite.Connection,
        *,
        limit: int,
    ) -> list[aiosqlite.Row]:
        if limit == 0:
            return []
        return list(
            await (
                await db.execute(
                    """
                    SELECT id,scope,kind,content,summary,sensitivity,confidence,pinned,updated_at
                    FROM memory_items
                    WHERE sensitivity='normal'
                    ORDER BY pinned DESC,updated_at DESC,id ASC LIMIT ?
                    """,
                    (limit,),
                )
            ).fetchall()
        )

    @staticmethod
    async def _episodes_locked(
        db: aiosqlite.Connection,
        current_goal_run_id: str,
        *,
        limit: int,
    ) -> list[aiosqlite.Row]:
        if limit == 0:
            return []
        return list(
            await (
                await db.execute(
                    """
                    SELECT id,goal_run_id,objective_summary,outcome,score,
                           failure_tags_json,user_feedback_score,created_at
                    FROM episodes WHERE goal_run_id<>?
                    ORDER BY created_at DESC,id ASC LIMIT ?
                    """,
                    (current_goal_run_id, limit),
                )
            ).fetchall()
        )

    async def _agents_locked(
        self,
        db: aiosqlite.Connection,
        *,
        limit: int,
    ) -> list[aiosqlite.Row]:
        if limit == 0:
            return []
        now = self.clock()
        agents: list[aiosqlite.Row] = []
        async with db.execute(
            """
            SELECT a.id,a.name,a.version,a.model_id,a.status,a.skills_json,a.last_seen_at,
                   a.max_concurrency,a.runtime,a.supported_protocol_version,
                   COALESCE(s.composite_score,0.0) AS observed_score
            FROM agents AS a
            LEFT JOIN agent_score_snapshots AS s ON s.agent_id=a.id
            WHERE a.status IN ('online','draining')
            ORDER BY CASE a.status WHEN 'online' THEN 0 ELSE 1 END,
                     observed_score DESC,a.id ASC
            """
        ) as cursor:
            async for row in cursor:
                if agent_is_fresh(dict(row), now=now, timeout_seconds=self.offline_timeout_seconds):
                    agents.append(row)
                    if len(agents) == limit:
                        break
        return agents

    @staticmethod
    async def _failures_locked(
        db: aiosqlite.Connection,
        goal_run_id: str,
        node_id: str | None,
    ) -> list[aiosqlite.Row]:
        if node_id is None:
            cursor = await db.execute(
                """
                SELECT id,status,error_summary,required_skill,updated_at
                FROM plan_nodes
                WHERE goal_run_id=? AND error_summary IS NOT NULL
                ORDER BY updated_at DESC,id ASC LIMIT ?
                """,
                (goal_run_id, 12),
            )
        else:
            cursor = await db.execute(
                """
                SELECT id,status,error_summary,required_skill,updated_at
                FROM plan_nodes
                WHERE goal_run_id=? AND error_summary IS NOT NULL AND id<>?
                ORDER BY updated_at DESC,id ASC LIMIT ?
                """,
                (goal_run_id, node_id, 12),
            )
        return list(await cursor.fetchall())


def safe_context_text(value: str | None, *, max_chars: int = _MAX_SAFE_TEXT_CHARS) -> str:
    """Return bounded text safe for summary persistence and model context.

    This layers context-specific fail-closed coverage on the repository's
    dataset redactor. It deliberately redacts credential values and protected
    local paths before any truncation, so truncation cannot expose a prefix.
    """

    if value is None or max_chars <= 0:
        return ""
    if not isinstance(value, str):
        raise TypeError("context text must be a string")
    redacted = redact_dataset_text(value) or ""
    redacted = _CREDENTIAL_URL.sub("<redacted-credential-url>", redacted)
    redacted = _ADDITIONAL_SECRET.sub("<redacted-secret>", redacted)
    redacted = _ADDITIONAL_PATH.sub("<protected-path>", redacted)
    redacted = _GENERIC_ABSOLUTE_PATH.sub("<protected-path>", redacted)
    redacted = "".join(max(character, " ") for character in redacted)
    redacted = _WHITESPACE.sub(" ", redacted).strip()
    if len(redacted) <= max_chars:
        return redacted
    if max_chars <= 1:
        return "…"[:max_chars]
    return redacted[: max_chars - 1].rstrip() + "…"


def _goal_card(row: aiosqlite.Row) -> ContextCard:
    criteria = _safe_json_string_list(row["completion_criteria_json"])
    summary = safe_context_text(
        "Goal "
        f"objective={row['objective']}; status={row['status']}; phase={row['current_phase']}; "
        f"autonomy={row['autonomy_profile']}; criteria={'; '.join(criteria)}",
        max_chars=1_000,
    )
    return ContextCard(
        card_id=f"goal:{row['id']}",
        kind="goal",
        summary=summary,
        provenance_ids=(str(row["id"]),),
    )


def _root_card(row: aiosqlite.Row) -> ContextCard:
    summary = safe_context_text(
        f"Root task title={row['title']}; status={row['status']}; objective={row['input']}",
        max_chars=1_200,
    )
    return ContextCard(
        card_id=f"root-task:{row['id']}",
        kind="root_task",
        summary=summary,
        provenance_ids=(str(row["id"]),),
    )


def _node_card(row: aiosqlite.Row, *, max_result_chars: int) -> ContextCard:
    result = _bounded_node_result(
        row["result_summary"],
        max_chars=max_result_chars,
        unavailable="not available",
    )
    error = _bounded_node_result(
        row["error_summary"],
        max_chars=max_result_chars,
        unavailable="none",
    )
    summary = safe_context_text(
        f"Node {row['title']}; type={row['node_type']}; status={row['status']}; "
        f"objective={row['objective']}; expected={row['expected_output']}; "
        f"result={result}; error={error}",
        max_chars=1_200,
    )
    return ContextCard(
        card_id=f"node:{row['id']}",
        kind="node",
        summary=summary,
        provenance_ids=(str(row["id"]),),
    )


def _constraints_card(goal: aiosqlite.Row, node: aiosqlite.Row | None) -> ContextCard:
    fields = [
        "Models only propose; policy and gateways remain authoritative",
        f"autonomy_profile={goal['autonomy_profile']}",
        f"max_parallelism={goal['max_parallelism']}",
    ]
    provenance = [str(goal["id"])]
    if node is not None:
        if node["required_skill"]:
            fields.append(f"required_skill={node['required_skill']}")
        fields.append(f"node_type={node['node_type']}")
        provenance.append(str(node["id"]))
    return ContextCard(
        card_id=f"constraints:{goal['id']}:{node['id'] if node is not None else 'goal'}",
        kind="constraints",
        summary=safe_context_text("; ".join(fields), max_chars=800),
        provenance_ids=tuple(provenance),
    )


def _budgets_card(goal: aiosqlite.Row) -> ContextCard:
    summary = (
        f"steps={goal['step_count']}/{goal['max_steps']}; "
        f"replans={goal['replan_count']}/{goal['max_replans']}; "
        f"model_calls={goal['model_call_count']}/{goal['max_model_calls']}; "
        f"runtime_seconds={goal['max_runtime_seconds']}"
    )
    return ContextCard(
        card_id=f"budgets:{goal['id']}",
        kind="budgets",
        summary=safe_context_text(summary, max_chars=500),
        provenance_ids=(str(goal["id"]),),
    )


def _goal_failure_cards(goal: aiosqlite.Row) -> list[ContextCard]:
    failure = goal["failure_reason"]
    evaluator = goal["evaluator_summary"]
    if failure is None and evaluator is None:
        return []
    return [
        ContextCard(
            card_id=f"goal-failure:{goal['id']}",
            kind="failure",
            summary=safe_context_text(
                f"Goal failure={failure or 'none'}; evaluator={evaluator or 'none'}",
                max_chars=700,
            ),
            provenance_ids=(str(goal["id"]),),
        )
    ]


def _failure_cards(
    rows: Sequence[aiosqlite.Row],
    *,
    max_result_chars: int,
) -> list[ContextCard]:
    return [
        ContextCard(
            card_id=f"failure:{row['id']}",
            kind="failure",
            summary=safe_context_text(
                f"Node status={row['status']}; skill={row['required_skill'] or 'none'}; "
                "failure="
                + _bounded_node_result(
                    row["error_summary"],
                    max_chars=max_result_chars,
                    unavailable="none",
                ),
                max_chars=700,
            ),
            provenance_ids=(str(row["id"]),),
        )
        for row in rows
    ]


def _upstream_card(row: aiosqlite.Row, *, max_result_chars: int) -> ContextCard:
    result = _bounded_node_result(
        row["result_summary"],
        max_chars=max_result_chars,
        unavailable="summary unavailable",
    )
    summary = safe_context_text(
        f"Completed upstream {row['title']}; dependency={row['dependency_type']}; result={result}",
        max_chars=1_000,
    )
    return ContextCard(
        card_id=f"upstream:{row['id']}",
        kind="upstream",
        summary=summary,
        provenance_ids=(str(row["id"]),),
    )


def _episode_card(row: aiosqlite.Row) -> ContextCard:
    failures = _safe_json_string_list(row["failure_tags_json"])
    summary = safe_context_text(
        f"Prior episode outcome={row['outcome']}; objective={row['objective_summary']}; "
        f"failure_tags={','.join(failures) if failures else 'none'}; "
        f"score={row['score'] if row['score'] is not None else 'unscored'}",
        max_chars=700,
    )
    return ContextCard(
        card_id=f"episode:{row['id']}",
        kind="episode",
        summary=summary,
        provenance_ids=(str(row["id"]), str(row["goal_run_id"])),
    )


def _memory_card(row: aiosqlite.Row) -> ContextCard:
    source_text = row["summary"] if row["summary"] else row["content"]
    summary = safe_context_text(
        f"Memory scope={row['scope']}; kind={row['kind']}; summary={source_text}",
        max_chars=700,
    )
    return ContextCard(
        card_id=f"memory:{row['id']}",
        kind="memory",
        summary=summary,
        provenance_ids=(str(row["id"]),),
    )


def _agent_card(row: aiosqlite.Row) -> ContextCard:
    skills = _safe_json_string_list(row["skills_json"])
    card_version = safe_context_text(str(row["version"]), max_chars=64)
    summary = safe_context_text(
        f"Agent {row['name']}; status={row['status']}; runtime={row['runtime']}; "
        f"protocol={row['supported_protocol_version']}; skills={','.join(skills)}; "
        f"max_concurrency={row['max_concurrency']}; observed_score={row['observed_score']}",
        max_chars=700,
    )
    return ContextCard(
        card_id=f"agent:{row['id']}:{card_version}",
        kind="agent_card",
        summary=summary,
        provenance_ids=(str(row["id"]),),
    )


def _bounded_node_result(
    value: object,
    *,
    max_chars: int,
    unavailable: str,
) -> str:
    if value is None or not str(value).strip():
        return unavailable
    if max_chars == 0:
        return "omitted by context budget"
    return safe_context_text(str(value), max_chars=max_chars) or unavailable


def _normalized_additional_cards(cards: Sequence[ContextCard]) -> tuple[ContextCard, ...]:
    normalized: list[ContextCard] = []
    seen: set[str] = set()
    for card in cards:
        if not isinstance(card, ContextCard):
            raise TypeError("additional context cards must be ContextCard values")
        card_id = _validated_identifier(card.card_id, "card_id")
        kind = _validated_identifier(card.kind, "kind")
        if card_id in seen:
            raise ValueError("additional context card IDs must be unique")
        seen.add(card_id)
        summary = safe_context_text(card.summary, max_chars=1_000)
        if not summary:
            continue
        provenance = _stable_unique(
            _validated_identifier(item, "source_id") for item in card.provenance_ids
        )
        normalized.append(
            ContextCard(
                card_id=card_id,
                kind=kind,
                summary=summary,
                provenance_ids=provenance,
            )
        )
    return tuple(normalized)


def _card_model_payload(
    purpose: str,
    cards: Sequence[ContextCard],
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "purpose": purpose,
        "cards": [card.as_model_dict() for card in cards],
    }


def _payload_tokens(payload: Mapping[str, object]) -> int:
    return _approx_tokens(_canonical_json(payload))


def _minimum_card(card: ContextCard) -> ContextCard:
    return ContextCard(
        card_id=card.card_id,
        kind=card.kind,
        summary="…",
        provenance_ids=card.provenance_ids,
    )


def _fit_card_for_payload(
    card: ContextCard,
    *,
    prefix: Sequence[ContextCard],
    suffix: Sequence[ContextCard],
    purpose: str,
    max_tokens: int,
) -> ContextCard | None:
    minimum = _minimum_card(card)
    if _payload_tokens(_card_model_payload(purpose, (*prefix, minimum, *suffix))) > max_tokens:
        return None
    if _payload_tokens(_card_model_payload(purpose, (*prefix, card, *suffix))) <= max_tokens:
        return card
    low = 1
    high = len(card.summary)
    best: ContextCard | None = None
    while low <= high:
        midpoint = (low + high) // 2
        candidate = ContextCard(
            card_id=card.card_id,
            kind=card.kind,
            summary=safe_context_text(card.summary, max_chars=midpoint),
            provenance_ids=card.provenance_ids,
        )
        if (
            _payload_tokens(_card_model_payload(purpose, (*prefix, candidate, *suffix)))
            <= max_tokens
        ):
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def bound_evaluation_context(
    context: GoalEvaluationContext,
    *,
    max_tokens: int,
) -> GoalEvaluationContext:
    """Return a schema-valid evaluator context under an exact JSON budget.

    Node identifiers remain available for validating model-proposed dependency
    references. If the configured budget is too small even for the identifiers
    and minimally redacted fields, evaluation fails closed instead of silently
    exceeding the configured limit.
    """

    if not 64 <= max_tokens <= 32_768:
        raise ValueError("max_tokens must be between 64 and 32768")

    def text(value: str, *, limit: int, fallback: str) -> str:
        bounded = safe_context_text(value, max_chars=max(1, limit))
        return bounded or safe_context_text(fallback, max_chars=max(1, limit)) or "…"

    def candidate(char_limit: int, node_limit: int) -> GoalEvaluationContext:
        nodes: list[EvaluationNodeResult] = []
        for node in context.node_results[:node_limit]:
            result = (
                text(node.result_summary, limit=char_limit, fallback="redacted result")
                if node.result_summary is not None
                else None
            )
            failure = (
                text(
                    node.failure_reason,
                    limit=min(char_limit, 500),
                    fallback="redacted failure",
                )
                if node.failure_reason is not None
                else None
            )
            nodes.append(
                EvaluationNodeResult(
                    node_id=node.node_id,
                    title=text(
                        node.title,
                        limit=min(char_limit, 500),
                        fallback="redacted node",
                    ),
                    status=node.status,
                    expected_output=text(
                        node.expected_output,
                        limit=char_limit,
                        fallback="redacted expected output",
                    ),
                    result_summary=result,
                    failure_reason=failure,
                    node_type=node.node_type,
                    required_skill=node.required_skill,
                )
            )
        return GoalEvaluationContext(
            schema_version="1.0",
            goal_run_id=context.goal_run_id,
            objective=text(context.objective, limit=char_limit, fallback="redacted objective"),
            completion_criteria=[
                text(
                    criterion,
                    limit=min(char_limit, 500),
                    fallback="redacted criterion",
                )
                for criterion in context.completion_criteria
            ],
            node_results=nodes,
            known_node_ids=list(context.known_node_ids),
            available_skills=(
                list(context.available_skills) if context.available_skills is not None else None
            ),
            remaining_step_budget=context.remaining_step_budget,
            remaining_model_call_budget=context.remaining_model_call_budget,
            elapsed_seconds=context.elapsed_seconds,
            state_fingerprint=context.state_fingerprint,
        )

    for node_limit in range(len(context.node_results), -1, -1):
        minimum = candidate(1, node_limit)
        if _payload_tokens(minimum.model_dump(mode="json")) > max_tokens:
            continue
        low = 1
        high = 4_000
        best = minimum
        while low <= high:
            midpoint = (low + high) // 2
            current = candidate(midpoint, node_limit)
            if _payload_tokens(current.model_dump(mode="json")) <= max_tokens:
                best = current
                low = midpoint + 1
            else:
                high = midpoint - 1
        return best
    raise ValueError("evaluation context cannot fit the configured token budget")


def _approx_tokens(value: str) -> int:
    return max(1, math.ceil(len(value.encode("utf-8")) / 4))


def _validated_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a safe identifier")
    return value


def _validated_optional_identifier(value: str | None, field: str) -> str | None:
    return _validated_identifier(value, field) if value is not None else None


def _safe_json_string_list(raw: object) -> tuple[str, ...]:
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(
        safe_context_text(item, max_chars=200)
        for item in value
        if isinstance(item, str) and safe_context_text(item, max_chars=200)
    )


def _stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _strict_object(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise RuntimeError("stored goal context is invalid JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("stored goal context is not an object")
    return value


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()
