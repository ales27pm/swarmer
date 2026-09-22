from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from app.services.audit_log import append_audit_event
from app.services.context_builder import safe_context_text
from app.services.embedding_service import EmbeddingService, EmbeddingServiceError
from app.services.goal_limits import runtime_expired
from app.services.project_contracts import ProjectMemoryContext
from app.services.swarm_contracts import GoalMemoryContextResponse

MAX_ITEMS = 512
MAX_EMBED_DOCUMENTS = 24
MAX_DIMENSIONS = 8_192
_TERMINAL = frozenset({"completed", "failed", "cancelled", "budget_exhausted"})
_CODE_LINE = re.compile(
    r"^\s*(?:def |class |import |from \S+ import |function |(?:export )?(?:const|let|var) "
    r"|return |#include|[{}\[\]]|\w+\s*(?:=|:=))"
)

MemoryPurpose = Literal["worker", "planner", "evaluator"]


class ProjectMemoryConflict(RuntimeError):
    """The requested project memory no longer matches its goal or trusted receipt."""


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _summary(value: str) -> str:
    """Keep decision prose, never project files, check output or fenced source."""
    prose = re.sub(r"```.*?(?:```|\Z)", "", value, flags=re.DOTALL)
    prose = "\n".join(line for line in prose.splitlines() if not _CODE_LINE.match(line))
    return safe_context_text(prose, max_chars=1_200).strip()


def _vector(value: object) -> list[float] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DIMENSIONS:
        return None
    if any(type(item) not in {int, float} for item in value):
        return None
    try:
        result = [float(item) for item in value]
    except (OverflowError, ValueError):
        return None
    if not all(math.isfinite(item) for item in result):
        return None
    scale = max(abs(item) for item in result)
    if scale <= 0:
        return None
    return result


def _cosine(left: list[float], right: list[float]) -> float:
    # Scale before normalizing, so finite but very large/small provider values
    # never overflow intermediate squares or denominators.
    def unit(values: list[float]) -> list[float]:
        scale = max(abs(value) for value in values)
        scaled = [value / scale for value in values]
        norm = math.hypot(*scaled)
        return [value / norm for value in scaled]

    return max(
        -1.0, min(1.0, math.fsum(a * b for a, b in zip(unit(left), unit(right), strict=True)))
    )


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]{3,}", text.casefold()))


def _context(mode: str, reason: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return ProjectMemoryContext.model_validate(
        {"mode": mode, "reason": reason, "items": items[:4]}
    ).model_dump()


class ProjectMemoryService:
    """Private project-scoped decision projection; vectors are disposable caches.

    Reads at most 480 recent messages and 32 historical plans per project.
    A dispatch context can spend at most one actual embedding request, containing
    its query plus 24 unindexed decisions. Provider failures are recorded and
    never retried for the same node/context. The worker's generation credit is
    preserved. All fallbacks identify their lexical mode explicitly.
    """

    def __init__(
        self,
        db_path: Path,
        embedding_service: EmbeddingService | None = None,
        *,
        model_revision: str | None = None,
        query_prefix: str = "",
        document_prefix: str = "",
        timeout_seconds: float = 10,
        hybrid: bool = False,
    ) -> None:
        if not 0 < timeout_seconds <= 10:
            raise ValueError("project memory timeout must be at most ten seconds")
        self.db_path, self.embedding_service = db_path, embedding_service
        self.model_revision = model_revision
        self.query_prefix, self.document_prefix = query_prefix, document_prefix
        self.timeout_seconds = timeout_seconds
        self.hybrid = hybrid
        self.provider_identity = _digest(
            {
                "format": 1,
                "provider": getattr(embedding_service, "provider_name", None),
                "origin": str(getattr(embedding_service, "base_url", "")).rstrip("/"),
                "model": getattr(embedding_service, "model", None),
                "revision": model_revision,
                "query_prefix": query_prefix,
                "document_prefix": document_prefix,
            }
        )

    async def initialize(self) -> None:
        # Tables belong to the central additive schema and its backup/version path.
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("SELECT 1 FROM project_memory_items LIMIT 1")
            await db.execute("SELECT 1 FROM project_memory_queries LIMIT 1")
            await db.execute("SELECT 1 FROM goal_memory_queries LIMIT 1")

    @staticmethod
    def _logical_goal(goal: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: goal[key]
            for key in (
                "id",
                "objective",
                "completion_criteria_json",
                "autonomy_profile",
                "max_steps",
                "max_replans",
                "max_parallelism",
                "max_runtime_seconds",
                "max_model_calls",
            )
        }

    @staticmethod
    async def _goal_snapshot(db: aiosqlite.Connection, goal_id: str) -> dict[str, Any]:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                """SELECT g.*,l.project_id,
            (SELECT r.id FROM project_revisions r WHERE r.project_id=l.project_id
             ORDER BY revision DESC LIMIT 1) AS revision_id,
            (SELECT r.sha256 FROM project_revisions r WHERE r.project_id=l.project_id
             ORDER BY revision DESC LIMIT 1) AS revision_sha256
            FROM goal_runs g LEFT JOIN goal_project_links l ON l.goal_run_id=g.id WHERE g.id=?""",
                (goal_id,),
            )
        ).fetchone()
        if row is None:
            raise ProjectMemoryConflict("goal not found")
        goal = dict(row)
        if goal["status"] in _TERMINAL or runtime_expired(goal):
            raise ProjectMemoryConflict("goal is not active")
        history = await (
            await db.execute(
                """SELECT m.role,m.content FROM goal_messages m JOIN goal_conversation_links l
            ON l.conversation_id=m.conversation_id WHERE l.goal_run_id=?
            ORDER BY m.rowid DESC LIMIT 40""",
                (goal_id,),
            )
        ).fetchall()
        goal["recent_conversation"] = [
            {"role": row["role"], "content": safe_context_text(row["content"], max_chars=4_000)}
            for row in reversed(list(history))
        ]
        latest_user = next((row["content"] for row in history if row["role"] == "user"), "")
        goal["memory_query"] = _summary(f"{latest_user}\n{goal['objective']}")
        return goal

    def _logical_fingerprint(self, goal: Mapping[str, Any], purpose: MemoryPurpose) -> str:
        return _digest(
            {
                "goal": self._logical_goal(goal),
                "purpose": purpose,
                "project_id": goal["project_id"],
                "conversation_revision": goal["conversation_revision"],
                "revision_id": goal["revision_id"],
                "revision_sha256": goal["revision_sha256"],
                "provider": self.provider_identity,
                "retrieval": "hybrid_rrf_v1" if self.hybrid else "semantic_or_lexical_v1",
                "query": goal["memory_query"],
                "recent_conversation": goal["recent_conversation"],
            }
        )

    @staticmethod
    async def _planning_eligibility(
        db: aiosqlite.Connection, goal: Mapping[str, Any]
    ) -> tuple[bool, int]:
        row = await (
            await db.execute(
                """SELECT
            (SELECT COALESCE(SUM(embedding_requested),0) FROM goal_memory_queries
             WHERE goal_run_id=? AND purpose='planner'),
            (SELECT COUNT(*) FROM plan_nodes WHERE goal_run_id=?),
            (SELECT COUNT(*) FROM goal_model_calls WHERE goal_run_id=?)""",
                (goal["id"], goal["id"], goal["id"]),
            )
        ).fetchone()
        credits = int(row[0]) if row else 0
        eligible = bool(
            row
            and goal["status"] == "planning"
            and goal["started_at"] is None
            and int(goal["step_count"]) == 0
            and int(goal["replan_count"]) == 0
            and row[1] == 0
            and row[2] == 0
            and int(goal["model_call_count"]) == credits
        )
        return eligible, credits

    def _response(
        self,
        goal: Mapping[str, Any],
        purpose: MemoryPurpose,
        memory: dict[str, Any],
        *,
        eligible: bool,
        credits: int,
    ) -> dict[str, Any]:
        fingerprint = _digest(
            {"logical": self._logical_fingerprint(goal, purpose), "memory": memory}
        )
        return GoalMemoryContextResponse.model_validate(
            {
                **memory,
                "schema_version": "1.0",
                "goal_id": goal["id"],
                "project_id": goal["project_id"],
                "conversation_revision": int(goal["conversation_revision"]),
                "base_revision_id": goal["revision_id"],
                "provider_fingerprint": self.provider_identity,
                "context_fingerprint": fingerprint,
                "embedding": {
                    "configured": self.embedding_service is not None,
                    "model": getattr(self.embedding_service, "model", None),
                    "model_revision": self.model_revision,
                    "storage": "ubuntu_sqlite",
                },
                "local_planning_eligible": eligible,
                "planning_embedding_call_count": credits,
                "recent_conversation": goal["recent_conversation"],
            }
        ).model_dump(mode="json")

    def _request_id(
        self,
        goal: Mapping[str, Any],
        node_id: str | None,
        query_sha: str,
        base_revision_id: str | None,
        purpose: MemoryPurpose,
        logical_fingerprint: str,
    ) -> str:
        identity = [
            goal["project_id"],
            goal["id"],
            node_id,
            goal["conversation_revision"],
            base_revision_id,
            query_sha,
            self.provider_identity,
            "hybrid_rrf_v1" if self.hybrid else "semantic_or_lexical_v1",
        ]
        if purpose != "worker":
            identity += [purpose, logical_fingerprint]
        return ("pmq_" if purpose == "worker" else "gmq_") + _digest(identity)[:48]

    async def retrieve_for_goal(
        self,
        goal_id: str,
        purpose: Literal["planner", "evaluator"],
        *,
        expected_goal_updated_at: str | None = None,
    ) -> dict[str, Any]:
        if purpose not in {"planner", "evaluator"}:
            raise ProjectMemoryConflict("invalid memory purpose")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN")
            goal = await self._goal_snapshot(db, goal_id)
            if (
                expected_goal_updated_at is not None
                and goal["updated_at"] != expected_goal_updated_at
            ):
                raise ProjectMemoryConflict("goal context changed")
            if purpose == "evaluator" and goal["started_at"] is None:
                raise ProjectMemoryConflict("goal evaluation has not started")
            eligible, credits = await self._planning_eligibility(db, goal)
            if goal["project_id"] is None:
                return self._response(
                    goal,
                    purpose,
                    _context("lexical", "no_linked_project", []),
                    eligible=eligible,
                    credits=credits,
                )
            logical = self._logical_fingerprint(goal, purpose)
            request_id = self._request_id(
                goal, None, _digest(goal["memory_query"]), goal["revision_id"], purpose, logical
            )
            cached = await (
                await db.execute("SELECT * FROM goal_memory_queries WHERE id=?", (request_id,))
            ).fetchone()
            if cached is not None and cached["context_json"] is not None:
                response = self._parse_receipt(cached["context_json"])
                await self._assert_response_current(
                    db, goal, purpose, response, cached["logical_fingerprint"]
                )
                response.update(
                    local_planning_eligible=eligible, planning_embedding_call_count=credits
                )
                return response
        memory = await self.retrieve(
            goal_id,
            None,
            str(goal["memory_query"]),
            base_revision_id=goal["revision_id"],
            conversation_revision=int(goal["conversation_revision"]),
            purpose=purpose,
            logical_fingerprint=logical,
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await self._goal_snapshot(db, goal_id)
            if self._logical_fingerprint(current, purpose) != logical:
                raise ProjectMemoryConflict("goal context changed")
            eligible, credits = await self._planning_eligibility(db, current)
            response = self._response(current, purpose, memory, eligible=eligible, credits=credits)
            await self._assert_response_current(db, current, purpose, response, logical)
            now = datetime.now(UTC).isoformat()
            # Lexical/no-budget results also receive an immutable, zero-charge receipt.
            await db.execute(
                """INSERT OR IGNORE INTO goal_memory_queries(id,project_id,goal_run_id,purpose,
                conversation_revision,base_revision_id,provider_identity,logical_fingerprint,
                query_sha256,status,embedding_requested,created_at,expires_at,completed_at)
                VALUES(?,?,?,?,?,?,?,?,?,'completed',0,?,?,?)""",
                (
                    request_id,
                    current["project_id"],
                    goal_id,
                    purpose,
                    current["conversation_revision"],
                    current["revision_id"],
                    self.provider_identity,
                    logical,
                    _digest(current["memory_query"]),
                    now,
                    now,
                    now,
                ),
            )
            # A concurrent in-flight query must finish before its result is reviewable.
            if memory["reason"] == "embedding_in_progress":
                raise ProjectMemoryConflict("project memory is still being prepared")
            await db.execute(
                "UPDATE goal_memory_queries SET context_json=?,context_fingerprint=? WHERE id=? AND context_json IS NULL",
                (
                    json.dumps(
                        response, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ),
                    response["context_fingerprint"],
                    request_id,
                ),
            )
            persisted = await (
                await db.execute(
                    "SELECT context_json FROM goal_memory_queries WHERE id=?", (request_id,)
                )
            ).fetchone()
            if persisted is None or persisted[0] is None:
                raise ProjectMemoryConflict("project memory receipt unavailable")
            await db.commit()
            # Concurrent callers share the first completed immutable result.
            result = self._parse_receipt(persisted[0])
            result.update(local_planning_eligible=eligible, planning_embedding_call_count=credits)
            return result

    @staticmethod
    def _parse_receipt(value: str) -> dict[str, Any]:
        try:
            return GoalMemoryContextResponse.model_validate_json(value).model_dump(mode="json")
        except ValueError as exc:
            raise ProjectMemoryConflict("project memory receipt is invalid") from exc

    async def _assert_response_current(
        self,
        db: aiosqlite.Connection,
        goal: Mapping[str, Any],
        purpose: MemoryPurpose,
        response: dict[str, Any],
        logical_fingerprint: str,
    ) -> None:
        memory = ProjectMemoryContext.model_validate(
            {key: response[key] for key in ("mode", "reason", "items")}
        ).model_dump(mode="json")
        if (
            logical_fingerprint != self._logical_fingerprint(goal, purpose)
            or response["goal_id"] != goal["id"]
            or response["project_id"] != goal["project_id"]
            or response["provider_fingerprint"] != self.provider_identity
            or response["conversation_revision"] != goal["conversation_revision"]
            or response["base_revision_id"] != goal["revision_id"]
            or response["recent_conversation"] != goal["recent_conversation"]
            or response["context_fingerprint"]
            != _digest({"logical": logical_fingerprint, "memory": memory})
        ):
            raise ProjectMemoryConflict("project memory context changed")
        for item in memory["items"]:
            # Resolve provenance from authoritative source rows, never a mutable vector projection.
            row = await (
                await db.execute(
                    "SELECT source_kind,source_id FROM project_memory_items WHERE id=? AND project_id=?",
                    (item["id"], goal["project_id"]),
                )
            ).fetchone()
            if row is None or row["source_id"] != item["source_id"]:
                raise ProjectMemoryConflict("project memory source changed")
            if row["source_kind"] == "message":
                source = await (
                    await db.execute(
                        """SELECT m.content FROM goal_messages m JOIN goal_conversation_links l
                    ON l.conversation_id=m.conversation_id JOIN goal_project_links p ON p.goal_run_id=l.goal_run_id
                    WHERE m.id=? AND l.goal_run_id=? AND p.project_id=?""",
                        (item["source_id"], goal["id"], goal["project_id"]),
                    )
                ).fetchone()
                summary = _summary(str(source[0])) if source else None
            else:
                source = await (
                    await db.execute(
                        "SELECT json_extract(snapshot_json,'$.plan') FROM project_revisions WHERE id=? AND project_id=?",
                        (item["source_id"], goal["project_id"]),
                    )
                ).fetchone()
                plan = json.loads(str(source[0] or "[]")) if source else None
                summary = (
                    _summary("\n".join(str(value) for value in plan))
                    if isinstance(plan, list)
                    else None
                )
            if summary != item["summary"]:
                raise ProjectMemoryConflict("project memory source changed")

    async def assert_context_current(
        self,
        goal_id: str,
        purpose: Literal["planner", "evaluator"],
        fingerprint: str,
        *,
        db: aiosqlite.Connection | None = None,
    ) -> None:
        if db is None:
            async with aiosqlite.connect(self.db_path) as connection:
                await connection.execute("BEGIN")
                await self.assert_context_current(goal_id, purpose, fingerprint, db=connection)
                return
        goal = await self._goal_snapshot(db, goal_id)
        eligible, credits = await self._planning_eligibility(db, goal)
        if purpose == "planner" and not eligible:
            raise ProjectMemoryConflict("goal is no longer eligible for initial local planning")
        if goal["project_id"] is None:
            expected = self._response(
                goal,
                purpose,
                _context("lexical", "no_linked_project", []),
                eligible=eligible,
                credits=credits,
            )
            if expected["context_fingerprint"] != fingerprint:
                raise ProjectMemoryConflict("project memory context changed")
            return
        row = await (
            await db.execute(
                "SELECT * FROM goal_memory_queries WHERE goal_run_id=? AND purpose=? AND context_fingerprint=?",
                (goal_id, purpose, fingerprint),
            )
        ).fetchone()
        if row is None or row["context_json"] is None:
            raise ProjectMemoryConflict("project memory receipt unavailable")
        response = self._parse_receipt(row["context_json"])
        await self._assert_response_current(db, goal, purpose, response, row["logical_fingerprint"])

    async def _read_sources(
        self,
        goal_id: str,
        base_revision_id: str | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            goal = await (
                await db.execute(
                    """SELECT g.*,l.project_id FROM goal_runs g JOIN goal_project_links l
                ON l.goal_run_id=g.id WHERE g.id=?""",
                    (goal_id,),
                )
            ).fetchone()
            if goal is None or goal["status"] in _TERMINAL or runtime_expired(dict(goal)):
                return None
            latest = await (
                await db.execute(
                    "SELECT id FROM project_revisions WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                    (goal["project_id"],),
                )
            ).fetchone()
            if (str(latest[0]) if latest else None) != base_revision_id:
                return None
            messages = list(
                await (
                    await db.execute(
                        """SELECT m.id,m.goal_run_id,m.role,m.content,m.rowid AS source_order
                FROM goal_messages m JOIN goal_conversation_links c ON c.conversation_id=m.conversation_id
                JOIN goal_project_links p ON p.goal_run_id=c.goal_run_id
                WHERE c.goal_run_id=? AND p.project_id=? ORDER BY m.rowid DESC LIMIT 480""",
                        (goal_id, goal["project_id"]),
                    )
                ).fetchall()
            )
            plans = list(
                await (
                    await db.execute(
                        """SELECT id,goal_run_id,revision,json_extract(snapshot_json,'$.plan') AS plan_json
                FROM project_revisions WHERE project_id=? ORDER BY revision DESC LIMIT 32""",
                        (goal["project_id"],),
                    )
                ).fetchall()
            )
        documents: list[dict[str, Any]] = []
        for message in reversed(messages):
            summary = _summary(str(message["content"]))
            if summary:
                documents.append(
                    {
                        "source_id": message["id"],
                        "source_kind": "message",
                        "source_goal_id": message["goal_run_id"],
                        "source_revision_id": None,
                        "source_order": int(message["source_order"]),
                        "summary": summary,
                    }
                )
        for plan in reversed(plans):
            values = json.loads(str(plan["plan_json"] or "[]"))
            summary = (
                _summary("\n".join(str(item) for item in values))
                if isinstance(values, list)
                else ""
            )
            if summary:
                documents.append(
                    {
                        "source_id": plan["id"],
                        "source_kind": "plan",
                        "source_goal_id": plan["goal_run_id"],
                        "source_revision_id": plan["id"],
                        "source_order": int(plan["revision"]),
                        "summary": summary,
                    }
                )
        for document in documents:
            document["id"] = (
                "pmem_"
                + _digest([goal["project_id"], document["source_kind"], document["source_id"]])[:48]
            )
            document["content_sha256"] = _digest(document["summary"])
        return dict(goal), documents

    async def _refresh_items(
        self,
        goal: Mapping[str, Any],
        documents: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """DELETE FROM project_memory_items WHERE project_id=?
                AND id NOT IN (SELECT value FROM json_each(?))""",
                (goal["project_id"], json.dumps([document["id"] for document in documents])),
            )
            for document in documents:
                await db.execute(
                    """INSERT INTO project_memory_items(id,project_id,source_kind,source_id,
                    source_goal_id,source_revision_id,source_order,summary,content_sha256,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    summary=excluded.summary,content_sha256=excluded.content_sha256,
                    embedding_identity=CASE WHEN content_sha256=excluded.content_sha256 THEN embedding_identity END,
                    dimensions=CASE WHEN content_sha256=excluded.content_sha256 THEN dimensions END,
                    vector_json=CASE WHEN content_sha256=excluded.content_sha256 THEN vector_json END,
                    embedding_fingerprint=CASE WHEN content_sha256=excluded.content_sha256 THEN embedding_fingerprint END,
                    updated_at=excluded.updated_at""",
                    (
                        document["id"],
                        goal["project_id"],
                        document["source_kind"],
                        document["source_id"],
                        document["source_goal_id"],
                        document["source_revision_id"],
                        document["source_order"],
                        document["summary"],
                        document["content_sha256"],
                        now,
                    ),
                )
            keep = {document["id"] for document in documents}
            rows = list(
                await (
                    await db.execute(
                        "SELECT * FROM project_memory_items WHERE project_id=? ORDER BY source_order,id LIMIT ?",
                        (goal["project_id"], MAX_ITEMS),
                    )
                ).fetchall()
            )
            await db.commit()
        return [dict(row) for row in rows if row["id"] in keep]

    def _stored_vector(self, item: Mapping[str, Any]) -> list[float] | None:
        if item.get("embedding_identity") != self.provider_identity:
            return None
        try:
            vector = _vector(json.loads(str(item["vector_json"])))
        except (ValueError, TypeError):
            return None
        if vector is None or len(vector) != item.get("dimensions"):
            return None
        expected = _digest([self.provider_identity, item["content_sha256"], len(vector), vector])
        return vector if expected == item.get("embedding_fingerprint") else None

    @staticmethod
    def _rank_lexical(
        query: str, items: Sequence[Mapping[str, Any]], reason: str
    ) -> dict[str, Any]:
        query_terms = _tokens(query)
        ranked: list[dict[str, Any]] = []
        for item in items:
            terms = _tokens(str(item["summary"]))
            score = len(query_terms & terms) / math.sqrt(max(1, len(query_terms) * len(terms)))
            if score > 0:
                ranked.append(
                    {
                        "id": item["id"],
                        "source_id": item["source_id"],
                        "summary": item["summary"],
                        "score": min(1.0, float(score)),
                    }
                )
        ranked.sort(key=lambda item: (-item["score"], item["id"]))
        return _context("lexical", reason, ranked)

    async def _reserve(
        self,
        goal: Mapping[str, Any],
        node_id: str | None,
        request_id: str,
        query_sha: str,
        base_revision_id: str | None,
        *,
        purpose: MemoryPurpose = "worker",
        logical_fingerprint: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        now = datetime.now(UTC)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT * FROM project_memory_queries WHERE id=?"
                    if purpose == "worker"
                    else "SELECT * FROM goal_memory_queries WHERE id=?",
                    (request_id,),
                )
            ).fetchone()
            if existing:
                return dict(existing), "cached"
            if purpose == "worker":
                current = await (
                    await db.execute(
                        """SELECT g.*,n.status AS node_status,n.task_id AS node_task_id
                FROM goal_runs g JOIN plan_nodes n ON n.goal_run_id=g.id
                WHERE g.id=? AND n.id=? AND n.required_skill='code.build_project'""",
                        (goal["id"], node_id),
                    )
                ).fetchone()
            else:
                current = await (
                    await db.execute(
                        """SELECT g.*,'ready' AS node_status,NULL AS node_task_id
                    FROM goal_runs g JOIN goal_project_links l ON l.goal_run_id=g.id
                    WHERE g.id=? AND l.project_id=?""",
                        (goal["id"], goal["project_id"]),
                    )
                ).fetchone()
            if (
                current is None
                or current["status"] in _TERMINAL
                or current["node_status"] not in {"ready", "dispatched"}
                or int(current["conversation_revision"]) != int(goal["conversation_revision"])
                or self._logical_goal(dict(current)) != self._logical_goal(goal)
                or runtime_expired(dict(current))
            ):
                return None, "context_changed"
            latest = await (
                await db.execute(
                    "SELECT id FROM project_revisions WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                    (goal["project_id"],),
                )
            ).fetchone()
            if (str(latest[0]) if latest else None) != base_revision_id:
                return None, "project_revision_changed"
            generation_credit = 1 if current["node_task_id"] is None else 0
            if int(current["model_call_count"]) + 1 + generation_credit > int(
                current["max_model_calls"]
            ):
                return None, "embedding_budget_unavailable"
            if purpose == "worker":
                await db.execute(
                    """INSERT INTO project_memory_queries(id,project_id,goal_run_id,node_id,
                conversation_revision,base_revision_id,provider_identity,query_sha256,status,
                created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,'started',?,?)""",
                    (
                        request_id,
                        goal["project_id"],
                        goal["id"],
                        node_id,
                        goal["conversation_revision"],
                        base_revision_id,
                        self.provider_identity,
                        query_sha,
                        now.isoformat(),
                        (now + timedelta(seconds=self.timeout_seconds)).isoformat(),
                    ),
                )
            else:
                await db.execute(
                    """INSERT INTO goal_memory_queries(id,project_id,goal_run_id,purpose,
                    conversation_revision,base_revision_id,provider_identity,logical_fingerprint,
                    query_sha256,status,embedding_requested,created_at,expires_at)
                    VALUES(?,?,?,?,?,?,?,?,?,'started',1,?,?)""",
                    (
                        request_id,
                        goal["project_id"],
                        goal["id"],
                        purpose,
                        goal["conversation_revision"],
                        base_revision_id,
                        self.provider_identity,
                        logical_fingerprint,
                        query_sha,
                        now.isoformat(),
                        (now + timedelta(seconds=self.timeout_seconds)).isoformat(),
                    ),
                )
            await db.execute(
                "UPDATE goal_runs SET model_call_count=model_call_count+1 WHERE id=?", (goal["id"],)
            )
            await append_audit_event(
                db,
                "goal.embedding.reserved",
                {
                    "goal_run_id": goal["id"],
                    "node_id": node_id,
                    "request_id": request_id,
                    "provider_fingerprint": self.provider_identity,
                    "model_calls_reserved": 1,
                },
                actor_type="control-plane",
                actor_id="project-memory",
                task_id=current["root_task_id"],
                trace_id=goal["id"],
                created_at=now.isoformat(),
            )
            await db.commit()
        return None, "reserved"

    async def retrieve(
        self,
        goal_id: str,
        node_id: str | None,
        query: str,
        *,
        base_revision_id: str | None,
        conversation_revision: int | None = None,
        purpose: MemoryPurpose = "worker",
        logical_fingerprint: str = "",
    ) -> dict[str, Any]:
        query = _summary(query)
        source = await self._read_sources(goal_id, base_revision_id)
        if source is None:
            return _context("lexical", "project_revision_changed", [])
        goal, documents = source
        if (
            conversation_revision is not None
            and int(goal["conversation_revision"]) != conversation_revision
        ):
            return _context("lexical", "conversation_changed", [])
        items = await self._refresh_items(goal, documents)

        async def fallback(reason: str) -> dict[str, Any]:
            if not await self._context_current(goal, base_revision_id):
                return _context("lexical", "project_context_changed", [])
            return self._rank_lexical(query, items, reason)

        if not query or not items:
            return _context("lexical", "no_indexable_decisions", [])
        if self.embedding_service is None:
            return await fallback("embedding_not_configured")
        query_sha = _digest(query)
        request_id = self._request_id(
            goal, node_id, query_sha, base_revision_id, purpose, logical_fingerprint
        )
        cached, state = await self._reserve(
            goal,
            node_id,
            request_id,
            query_sha,
            base_revision_id,
            purpose=purpose,
            logical_fingerprint=logical_fingerprint,
        )
        if state in {"context_changed", "project_revision_changed"}:
            return _context("lexical", state, [])
        query_vector: list[float] | None = None
        if state == "reserved":
            missing = [item for item in items if self._stored_vector(item) is None][
                :MAX_EMBED_DOCUMENTS
            ]
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    returned = await self.embedding_service.embed(
                        [self.query_prefix + query]
                        + [self.document_prefix + str(item["summary"]) for item in missing]
                    )
                checked_vectors = [_vector(value) for value in returned]
                if len(checked_vectors) != len(missing) + 1 or any(
                    vector is None for vector in checked_vectors
                ):
                    raise EmbeddingServiceError("invalid vectors")
                vectors = [vector for vector in checked_vectors if vector is not None]
                query_vector = vectors[0]
                if any(len(vector) != len(query_vector) for vector in vectors):
                    raise EmbeddingServiceError("inconsistent embedding dimensions")
                if not await self._persist_vectors(
                    goal,
                    node_id,
                    request_id,
                    base_revision_id,
                    query_vector,
                    missing,
                    vectors[1:],
                    purpose=purpose,
                ):
                    return _context("lexical", "project_context_changed", [])
                for item, vector in zip(missing, vectors[1:], strict=True):
                    item.update(
                        embedding_identity=self.provider_identity,
                        dimensions=len(vector),
                        vector_json=json.dumps(vector),
                        embedding_fingerprint=_digest(
                            [self.provider_identity, item["content_sha256"], len(vector), vector]
                        ),
                    )
            except (
                TimeoutError,
                EmbeddingServiceError,
                OSError,
                ValueError,
                TypeError,
                OverflowError,
            ):
                await self._fail_request(request_id, "embedding_unavailable", purpose=purpose)
                return await fallback("embedding_unavailable")
        elif cached is not None and cached["status"] == "completed":
            try:
                query_vector = _vector(json.loads(str(cached["query_vector_json"])))
            except (ValueError, TypeError):
                query_vector = None
            if (
                query_vector is None
                or len(query_vector) != cached["query_dimensions"]
                or _digest([request_id, len(query_vector), query_vector])
                != cached["query_vector_fingerprint"]
            ):
                return await fallback("embedding_cache_invalid")
        else:
            if (
                cached
                and cached["status"] == "started"
                and cached["expires_at"] <= datetime.now(UTC).isoformat()
            ):
                await self._fail_request(request_id, "embedding_interrupted", purpose=purpose)
                return await fallback("embedding_interrupted")
            return await fallback(
                "embedding_in_progress"
                if cached and cached["status"] == "started"
                else "embedding_unavailable"
                if cached
                else state,
            )
        if not await self._context_current(goal, base_revision_id):
            return _context("lexical", "project_context_changed", [])
        ranked: list[dict[str, Any]] = []
        if query_vector is None:
            return await fallback("embedding_cache_invalid")
        for item in items:
            stored_vector = self._stored_vector(item)
            if stored_vector is not None and len(stored_vector) == len(query_vector):
                score = _cosine(query_vector, stored_vector)
                if score > 0:
                    ranked.append(
                        {
                            "id": item["id"],
                            "source_id": item["source_id"],
                            "summary": item["summary"],
                            "score": score,
                        }
                    )
        if not ranked:
            return await fallback("embedding_index_unavailable")
        ranked.sort(key=lambda item: (-item["score"], item["id"]))
        if self.hybrid:
            lexical = self._rank_lexical(query, items, "hybrid")["items"]
            fused: dict[str, dict[str, Any]] = {}
            for results in (ranked, lexical):
                for rank, item in enumerate(results, start=1):
                    entry = fused.setdefault(item["id"], {**item, "score": 0.0})
                    entry["score"] += 30.5 / (60 + rank)
            ranked = sorted(fused.values(), key=lambda item: (-item["score"], item["id"]))
            return _context("hybrid", "lexical_semantic_rank_fusion", ranked)
        return _context("semantic", "historical_hints_recent_replies_take_precedence", ranked)

    async def _context_current(self, goal: Mapping[str, Any], base_revision_id: str | None) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """SELECT g.*,
                (SELECT r.id FROM project_revisions r WHERE r.project_id=l.project_id ORDER BY revision DESC LIMIT 1) AS revision_id
                FROM goal_runs g JOIN goal_project_links l ON l.goal_run_id=g.id
                WHERE g.id=? AND l.project_id=?""",
                    (goal["id"], goal["project_id"]),
                )
            ).fetchone()
        return (
            row is not None
            and row["status"] not in _TERMINAL
            and int(row["conversation_revision"]) == int(goal["conversation_revision"])
            and self._logical_goal(dict(row)) == self._logical_goal(goal)
            and row["revision_id"] == base_revision_id
            and not runtime_expired(dict(row))
        )

    async def _persist_vectors(
        self,
        goal: Mapping[str, Any],
        node_id: str | None,
        request_id: str,
        base_revision_id: str | None,
        query_vector: list[float],
        items: list[dict[str, Any]],
        vectors: list[list[float]],
        *,
        purpose: MemoryPurpose = "worker",
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            current = await (
                await db.execute(
                    """SELECT g.*,
                (SELECT r.id FROM project_revisions r WHERE r.project_id=l.project_id ORDER BY revision DESC LIMIT 1) AS revision_id
                FROM goal_runs g JOIN goal_project_links l ON l.goal_run_id=g.id
                WHERE g.id=? AND l.project_id=? AND (? IS NULL OR EXISTS
                (SELECT 1 FROM plan_nodes n WHERE n.goal_run_id=g.id AND n.id=?))""",
                    (goal["id"], goal["project_id"], node_id, node_id),
                )
            ).fetchone()
            receipt = await (
                await db.execute(
                    "SELECT status,expires_at FROM project_memory_queries WHERE id=?"
                    if purpose == "worker"
                    else "SELECT status,expires_at FROM goal_memory_queries WHERE id=?",
                    (request_id,),
                )
            ).fetchone()
            if (
                current is None
                or current["status"] in _TERMINAL
                or int(current["conversation_revision"]) != int(goal["conversation_revision"])
                or self._logical_goal(dict(current)) != self._logical_goal(goal)
                or current["revision_id"] != base_revision_id
                or runtime_expired(dict(current))
                or receipt is None
                or receipt["status"] != "started"
                or receipt["expires_at"] <= now
            ):
                await db.execute(
                    "UPDATE project_memory_queries SET status='failed',error_category='context_changed',completed_at=? WHERE id=?"
                    if purpose == "worker"
                    else "UPDATE goal_memory_queries SET status='failed',error_category='context_changed',completed_at=? WHERE id=?",
                    (now, request_id),
                )
                await db.commit()
                return False
            # A provider width change invalidates every old-width projection.
            # They can be rebuilt within a later node's bounded batch.
            await db.execute(
                """UPDATE project_memory_items SET embedding_identity=NULL,dimensions=NULL,
                vector_json=NULL,embedding_fingerprint=NULL WHERE project_id=? AND dimensions<>?""",
                (goal["project_id"], len(query_vector)),
            )
            for item, vector in zip(items, vectors, strict=True):
                await db.execute(
                    """UPDATE project_memory_items SET embedding_identity=?,dimensions=?,vector_json=?,
                    embedding_fingerprint=? WHERE id=? AND project_id=? AND content_sha256=?""",
                    (
                        self.provider_identity,
                        len(vector),
                        json.dumps(vector),
                        _digest(
                            [self.provider_identity, item["content_sha256"], len(vector), vector]
                        ),
                        item["id"],
                        goal["project_id"],
                        item["content_sha256"],
                    ),
                )
            await db.execute(
                """UPDATE project_memory_queries SET status='completed',query_dimensions=?,query_vector_json=?,
                query_vector_fingerprint=?,completed_at=? WHERE id=? AND status='started'"""
                if purpose == "worker"
                else """UPDATE goal_memory_queries SET status='completed',query_dimensions=?,query_vector_json=?,
                query_vector_fingerprint=?,completed_at=? WHERE id=? AND status='started'""",
                (
                    len(query_vector),
                    json.dumps(query_vector),
                    _digest([request_id, len(query_vector), query_vector]),
                    now,
                    request_id,
                ),
            )
            await db.commit()
        return True

    async def _fail_request(
        self, request_id: str, category: str, *, purpose: MemoryPurpose = "worker"
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE project_memory_queries SET status='failed',error_category=?,completed_at=? WHERE id=? AND status='started'"
                if purpose == "worker"
                else "UPDATE goal_memory_queries SET status='failed',error_category=?,completed_at=? WHERE id=? AND status='started'",
                (category, datetime.now(UTC).isoformat(), request_id),
            )
            await db.commit()
