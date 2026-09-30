"""Passive, scope-rechecked memory receipts. No retrieval, indexing or reconciliation."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import aiosqlite
from pydantic import ValidationError

from app.services.context_builder import safe_context_text
from app.services.memory_inspection_contracts import (
    MemoryUsageEntry,
    MemoryUsageItem,
    MemoryUsagePage,
    MemoryUsageRetrieval,
)

MAX_PAGE_BYTES = 128 * 1024
_MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_TABLES = ("goal_memory_queries", "goal_model_calls", "agent_jobs")


class MemoryInspectionCursorError(ValueError):
    pass


class MemoryInspectionEvidenceError(ValueError):
    pass


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _date(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError
        return value
    except ValueError as exc:
        raise MemoryInspectionEvidenceError("invalid recorded date") from exc


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_RECEIPT_BYTES:
        raise MemoryInspectionEvidenceError("receipt unavailable")
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise TypeError
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise MemoryInspectionEvidenceError("invalid receipt") from exc


def _code(value: Any, maximum: int = 100) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
        return None
    return value if len(value) <= maximum else None


def _excerpt(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise MemoryInspectionEvidenceError("invalid receipt text")
    # Source URLs are not needed for this receipt view; in particular do not expose
    # internal origins, signed links or credential-bearing URLs from old cards.
    value = re.sub(r"\b(?:[a-zA-Z][a-zA-Z0-9+.-]*://|www\.)[^\s<>]+", "[lien masqué]", value)
    return safe_context_text(value, max_chars=800) or None


def _retrieval(value: dict[str, Any]) -> MemoryUsageRetrieval:
    mode = value.get("mode")
    fingerprint = value.get("provider_fingerprint")
    return MemoryUsageRetrieval(
        mode=mode if mode in {"lexical", "semantic", "hybrid"} else "unknown",
        reason=_code(value.get("reason")),
        provider_fingerprint=(
            fingerprint if isinstance(fingerprint, str) and _HASH.fullmatch(fingerprint) else None
        ),
    )


async def _one(
    db: aiosqlite.Connection, sql: str, parameters: tuple[Any, ...]
) -> aiosqlite.Row | None:
    return await (await db.execute(sql, parameters)).fetchone()


class _Sources:
    """Resolve current authoritative scope before admitting or counting visible items."""

    def __init__(self, db: aiosqlite.Connection, project_id: str | None) -> None:
        self.db, self.project_id = db, project_id
        self.cache: dict[str, dict[str, Any] | None] = {}

    async def resolve(self, identity: str) -> dict[str, Any] | None:
        if identity in self.cache:
            return self.cache[identity]
        value = await self._resolve(identity)
        self.cache[identity] = value
        return value

    async def _resolve(self, identity: str) -> dict[str, Any] | None:
        common: dict[str, Any] = {
            "id": identity,
            "source_id": None,
            "source_goal_id": None,
            "source_revision_id": None,
            "source_at": None,
            "source_state": "unknown",
            "verification": "unknown",
        }
        if identity.startswith("mem_"):
            row = await _one(
                self.db,
                """SELECT id,scope,sensitivity,created_at FROM memory_items
                WHERE id=? AND (scope IN ('general','global') OR scope=?)""",
                (identity, f"project:{self.project_id}" if self.project_id else ""),
            )
            if row is None:
                return None
            event = await _one(
                self.db,
                """SELECT 1 FROM audit_events WHERE event_type='memory.remembered'
                AND actor_type='device' AND json_valid(payload_json)
                AND json_extract(payload_json,'$.memory_id')=? LIMIT 1""",
                (identity,),
            )
            return {
                **common,
                "source_id": identity,
                "source_kind": "general_memory",
                "scope": "general" if row["scope"] in {"general", "global"} else "project",
                "source_state": "unknown" if row["sensitivity"] == "normal" else "redacted",
                "source_at": _date(row["created_at"]),
                "verification": "user_asserted" if event else "unknown",
            }
        if not self.project_id:
            return None
        if identity.startswith("ep_"):
            row = await _one(
                self.db,
                """SELECT e.goal_run_id,e.created_at FROM episodes e
                JOIN goal_project_links p ON p.goal_run_id=e.goal_run_id
                JOIN goal_runs g ON g.id=e.goal_run_id AND g.root_task_id=e.root_task_id
                WHERE e.id=? AND p.project_id=?""",
                (identity, self.project_id),
            )
            return (
                None
                if row is None
                else {
                    **common,
                    "source_id": identity,
                    "source_kind": "episode",
                    "scope": "project",
                    "source_goal_id": row["goal_run_id"],
                    "source_at": _date(row["created_at"]),
                    "verification": "recorded_outcome",
                }
            )
        if not identity.startswith("pmem_"):
            return None
        row = await _one(
            self.db,
            """SELECT m.source_id,m.source_kind,m.source_goal_id,m.source_revision_id
            FROM project_memory_items m JOIN goal_project_links p ON p.goal_run_id=m.source_goal_id
            WHERE m.id=? AND m.project_id=? AND p.project_id=m.project_id""",
            (identity, self.project_id),
        )
        if row is None:
            return None
        if row["source_kind"] == "message":
            source = await _one(
                self.db,
                """SELECT m.created_at,m.role FROM goal_messages m
                JOIN goal_conversation_links c ON c.goal_run_id=m.goal_run_id AND c.conversation_id=m.conversation_id
                JOIN goal_project_links p ON p.goal_run_id=m.goal_run_id
                WHERE m.id=? AND m.goal_run_id=? AND p.project_id=?""",
                (row["source_id"], row["source_goal_id"], self.project_id),
            )
            verification = (
                ("user_asserted" if source["role"] == "user" else "assistant_claim")
                if source
                else "unknown"
            )
        else:
            source = await _one(
                self.db,
                """SELECT r.created_at FROM project_revisions r
                JOIN goal_project_links p ON p.goal_run_id=r.goal_run_id
                WHERE r.id=? AND r.id=? AND r.goal_run_id=? AND r.project_id=? AND p.project_id=r.project_id""",
                (
                    row["source_id"],
                    row["source_revision_id"],
                    row["source_goal_id"],
                    self.project_id,
                ),
            )
            verification = "assistant_claim" if source else "unknown"
        return {
            **common,
            "source_id": _identifier(row["source_id"]),
            "source_kind": "message" if row["source_kind"] == "message" else "project_plan",
            "scope": "project",
            "source_goal_id": row["source_goal_id"],
            "source_revision_id": _identifier(row["source_revision_id"]),
            "source_at": _date(source["created_at"]) if source else None,
            "source_state": "unknown" if source else "missing",
            "verification": verification,
        }

    async def items(self, candidates: list[dict[str, Any]]) -> tuple[list[MemoryUsageItem], int]:
        if len(candidates) > 4096:
            raise MemoryInspectionEvidenceError("receipt has too many references")
        items: list[MemoryUsageItem] = []
        masked: list[MemoryUsageItem] = []
        seen: set[str] = set()
        omitted = 0
        for candidate in candidates:
            identity = _identifier(candidate.get("id"))
            source = await self.resolve(identity) if identity else None
            if source is None or identity in seen:
                omitted += 1
                continue
            claimed = candidate.get("source_id")
            if claimed is not None and claimed != source["source_id"]:
                omitted += 1
                continue
            seen.add(cast(str, identity))
            excerpt = (
                _excerpt(candidate.get("summary")) if source["source_state"] == "unknown" else None
            )
            target = masked if source["source_state"] == "redacted" else items
            target.append(MemoryUsageItem(**source, summary=excerpt))
        eligible = items + masked
        return eligible[:100], omitted + max(0, len(eligible) - 100)


# All three joins are constrained by the requested goal before SQL limits. JSON
# selection is limited to receipts; file contents and queries are never selected.
_QUERIES = (
    """SELECT q.rowid AS rid,'retrieval:'||q.id AS id,q.created_at,q.completed_at,q.status,q.purpose,
       q.conversation_revision,g.root_task_id AS task_id,NULL AS node_id,NULL AS model_call_id,
       NULL AS worker_job_id,NULL AS context_id,NULL AS model_id,q.context_json AS payload,
       NULL AS provenance,'retrieved' AS stage
       FROM goal_memory_queries q JOIN goal_runs g ON g.id=q.goal_run_id
       JOIN goal_project_links p ON p.goal_run_id=q.goal_run_id AND p.project_id=q.project_id
       WHERE q.goal_run_id=:goal AND q.rowid<=:f0 AND q.context_json IS NOT NULL""",
    """SELECT c.rowid AS rid,'model_call:'||c.id AS id,c.created_at,c.completed_at,c.status,c.role AS purpose,
       c.conversation_revision,COALESCE(n.task_id,g.root_task_id) AS task_id,c.node_id,
       c.id AS model_call_id,NULL AS worker_job_id,x.id AS context_id,c.model_id,
       x.context_json AS payload,x.provenance_json AS provenance,'attached_to_model_call' AS stage
       FROM goal_model_calls c JOIN goal_runs g ON g.id=c.goal_run_id
       JOIN goal_contexts x ON x.id=c.context_id AND x.goal_run_id=c.goal_run_id
          AND x.root_task_id=g.root_task_id AND x.node_id IS c.node_id AND x.purpose=c.role
       LEFT JOIN plan_nodes n ON n.id=c.node_id AND n.goal_run_id=c.goal_run_id
       LEFT JOIN tasks t ON t.id=n.task_id AND t.source='goal:'||c.goal_run_id
       WHERE c.goal_run_id=:goal AND c.rowid<=:f1 AND (c.node_id IS NULL OR t.id IS NOT NULL)
       AND CASE WHEN NOT json_valid(x.context_json) THEN 1 ELSE
       (json_type(x.context_json,'$.project_memory')='object' OR EXISTS(
          SELECT 1 FROM json_each(x.context_json,'$.cards') card
          WHERE json_extract(card.value,'$.kind') IN ('memory','episode','project_memory_hint'))) END""",
    """SELECT j.rowid AS rid,'worker_job:'||j.id AS id,j.created_at,j.completed_at,j.status,
       j.required_skill AS purpose,n.conversation_revision,j.task_id,n.id AS node_id,
       NULL AS model_call_id,j.id AS worker_job_id,NULL AS context_id,NULL AS model_id,
       json_extract(j.payload_json,'$.memory') AS payload,NULL AS provenance,'included_in_worker_job' AS stage
       FROM agent_jobs j JOIN plan_nodes n ON n.worker_job_id=j.id AND n.task_id=j.task_id
          AND n.node_type='worker' AND n.required_skill=j.required_skill
       JOIN tasks t ON t.id=j.task_id AND t.source='goal:'||n.goal_run_id
       WHERE n.goal_run_id=:goal AND j.rowid<=:f2 AND json_valid(j.payload_json)
       AND json_type(j.payload_json,'$.memory')='object'""",
)


def _references(
    payload: dict[str, Any], provenance: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if provenance is None:
        memory = payload
        raw = memory.get("items")
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise MemoryInspectionEvidenceError("invalid memory receipt items")
        return raw, memory
    source_ids = provenance.get("source_ids")
    if not isinstance(source_ids, list):
        raise MemoryInspectionEvidenceError("missing receipt provenance")
    if isinstance(payload.get("project_memory"), dict):
        memory = payload["project_memory"]
        raw, _ = _references(memory, None)
        return [
            item if item.get("id") in source_ids and item.get("source_id") in source_ids else {}
            for item in raw
        ], memory
    cards = payload.get("cards")
    card_provenance = provenance.get("card_provenance")
    if not isinstance(cards, list) or not isinstance(card_provenance, dict):
        raise MemoryInspectionEvidenceError("invalid context cards")
    result = []
    for card in cards:
        if not isinstance(card, dict) or card.get("kind") not in {
            "memory",
            "episode",
            "project_memory_hint",
        }:
            continue
        card_id = card.get("card_id")
        prefix = {
            "memory": "memory:",
            "episode": "episode:",
            "project_memory_hint": "project-memory:",
        }[card["kind"]]
        if not isinstance(card_id, str) or not card_id.startswith(prefix):
            raise MemoryInspectionEvidenceError("invalid memory card identity")
        identity = card_id[len(prefix) :]
        refs = card_provenance.get(card_id)
        if (
            not isinstance(refs, list)
            or not refs
            or refs[0] != identity
            or (card["kind"] == "project_memory_hint" and len(refs) != 2)
            or any(ref not in source_ids for ref in refs)
        ):
            raise MemoryInspectionEvidenceError("invalid memory card provenance")
        result.append(
            {
                "id": identity,
                "source_id": refs[1]
                if card["kind"] == "project_memory_hint" and len(refs) == 2
                else None,
                "summary": card.get("summary"),
            }
        )
    return result, {}


def _validate_recorded_identity(
    payload: dict[str, Any], goal: aiosqlite.Row, receipt: aiosqlite.Row
) -> None:
    """Validate metadata when retained, without requiring fields absent in legacy cards."""
    expected = {
        "goal_id": goal["id"],
        "goal_run_id": goal["id"],
        "project_id": goal["project_id"],
        "root_task_id": goal["root_task_id"],
        "node_id": receipt["node_id"],
        "purpose": receipt["purpose"],
    }
    scopes = [payload]
    if isinstance(payload.get("project_memory"), dict):
        scopes.append(payload["project_memory"])
    for scope in scopes:
        if any(key in scope and scope[key] != value for key, value in expected.items()):
            raise MemoryInspectionEvidenceError("recorded memory identity mismatch")
        if "conversation_revision" in scope and (
            type(scope["conversation_revision"]) is not int
            or scope["conversation_revision"] != receipt["conversation_revision"]
        ):
            raise MemoryInspectionEvidenceError("recorded memory revision mismatch")


def _cursor_encode(scope: str, fences: list[int], anchor: str, key: tuple[str, str]) -> str:
    raw = json.dumps([1, scope, fences, anchor, list(key)], separators=(",", ":")).encode()
    value = base64.urlsafe_b64encode(zlib.compress(raw)).decode().rstrip("=")
    if len(value) > 512:
        raise MemoryInspectionEvidenceError("cursor exceeds public bound")
    return value


def _cursor_decode(value: str) -> tuple[str, list[int], str, tuple[str, str]]:
    try:
        if not value or len(value) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError
        compressed = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, 2049)
        if len(raw) > 2048 or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) != 5 or type(data[0]) is not int or data[0] != 1:
            raise ValueError
        _, scope, fences, anchor, key = data
        if (
            not isinstance(scope, str)
            or not _HASH.fullmatch(scope)
            or not isinstance(anchor, str)
            or not _HASH.fullmatch(anchor)
        ):
            raise ValueError
        if (
            not isinstance(fences, list)
            or len(fences) != 3
            or any(type(n) is not int or not 0 <= n < 2**63 for n in fences)
        ):
            raise ValueError
        if (
            not isinstance(key, list)
            or len(key) != 2
            or not _date(key[0])
            or not _identifier(key[1])
        ):
            raise ValueError
        return scope, fences, anchor, (key[0], key[1])
    except (ValueError, TypeError, zlib.error, RecursionError) as exc:
        raise MemoryInspectionCursorError("invalid memory receipt cursor") from exc


async def _fences(
    db: aiosqlite.Connection, goal: str, known: list[int] | None = None
) -> tuple[list[int], str]:
    fences, anchors = [], []
    for index, table in enumerate(_TABLES):
        condition = (
            "goal_run_id=?"
            if table != "agent_jobs"
            else """EXISTS(SELECT 1 FROM plan_nodes n JOIN tasks t ON t.id=n.task_id
                AND t.source='goal:'||n.goal_run_id WHERE n.worker_job_id=agent_jobs.id
                AND n.task_id=agent_jobs.task_id AND n.node_type='worker'
                AND n.required_skill=agent_jobs.required_skill AND n.goal_run_id=?)"""
        )
        if known is None:
            row = await _one(
                db,
                f"SELECT rowid,id,created_at FROM {table} WHERE {condition} ORDER BY rowid DESC LIMIT 1",
                (goal,),
            )
        else:
            row = await _one(
                db,
                f"SELECT rowid,id,created_at FROM {table} WHERE {condition} AND rowid=?",
                (goal, known[index]),
            )
        fences.append(int(row[0]) if row else 0)
        anchors.append(list(row) if row else None)
    return fences, hashlib.sha256(json.dumps(anchors, separators=(",", ":")).encode()).hexdigest()


async def read_memory_usage(
    db_path: Path, goal_id: str, *, limit: int = 20, cursor: str | None = None
) -> MemoryUsagePage | None:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise MemoryInspectionCursorError("invalid page limit")
    decoded = _cursor_decode(cursor) if cursor is not None else None
    if not _identifier(goal_id):
        return None
    async with aiosqlite.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        goal = await _one(
            db,
            """SELECT g.id,g.root_task_id,g.created_at,g.conversation_revision,p.project_id
            FROM goal_runs g LEFT JOIN goal_project_links p ON p.goal_run_id=g.id WHERE g.id=?""",
            (goal_id,),
        )
        if goal is None:
            return None
        scope = hashlib.sha256(
            json.dumps(
                [goal_id, goal["root_task_id"], goal["created_at"], goal["project_id"]]
            ).encode()
        ).hexdigest()
        if decoded and decoded[0] != scope:
            raise MemoryInspectionCursorError("memory receipt scope changed")
        fences, anchor = await _fences(db, goal_id, decoded[1] if decoded else None)
        if decoded and (fences != decoded[1] or anchor != decoded[2]):
            raise MemoryInspectionCursorError("memory receipt history changed")
        parameters: dict[str, Any] = {
            "goal": goal_id,
            "f0": fences[0],
            "f1": fences[1],
            "f2": fences[2],
            "count": limit + 1,
        }
        boundary = ""
        if decoded:
            boundary = "WHERE (created_at,id)<(:date,:identity)"
            parameters.update(date=decoded[3][0], identity=decoded[3][1])
        rows = list(
            await (
                await db.execute(
                    "SELECT * FROM ("
                    + " UNION ALL ".join(_QUERIES)
                    + f") {boundary} ORDER BY created_at DESC,id DESC LIMIT :count",
                    parameters,
                )
            ).fetchall()
        )
        page = MemoryUsagePage(
            schema_version="1.0",
            goal_id=goal_id,
            project_id=goal["project_id"],
            current_conversation_revision=goal["conversation_revision"],
            observed_at=datetime.now(UTC).isoformat(),
            availability="available" if rows else "no_records",
            history_coverage="recorded_receipts_only",
            entries=[],
            next_cursor=None,
        )
        sources = _Sources(db, goal["project_id"])
        for index, row in enumerate(rows[:limit]):
            payload = _object(row["payload"])
            if row["stage"] == "retrieved" and (
                payload.get("goal_id") != goal_id
                or payload.get("project_id") != goal["project_id"]
                or payload.get("conversation_revision") != row["conversation_revision"]
            ):
                raise MemoryInspectionEvidenceError("retrieval scope mismatch")
            _validate_recorded_identity(payload, goal, row)
            provenance = _object(row["provenance"]) if row["provenance"] is not None else None
            refs, memory = _references(payload, provenance)
            items, omitted = await sources.items(refs)
            recorded_at = _date(row["created_at"])
            status = _code(row["status"], 50)
            if recorded_at is None or status is None:
                raise MemoryInspectionEvidenceError("missing receipt metadata")
            try:
                entry = MemoryUsageEntry(
                    id=row["id"],
                    evidence_stage=row["stage"],
                    recorded_at=recorded_at,
                    completed_at=_date(row["completed_at"]),
                    status=status,
                    purpose=_code(row["purpose"]),
                    conversation_revision=row["conversation_revision"],
                    task_id=row["task_id"],
                    node_id=row["node_id"],
                    model_call_id=row["model_call_id"],
                    worker_job_id=row["worker_job_id"],
                    context_id=row["context_id"],
                    model_id=row["model_id"]
                    if isinstance(row["model_id"], str)
                    and "://" not in row["model_id"]
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", row["model_id"])
                    else None,
                    retrieval=_retrieval(memory),
                    items=items,
                    omitted_item_count=omitted,
                )
            except ValidationError as exc:
                raise MemoryInspectionEvidenceError("invalid memory receipt metadata") from exc
            next_cursor = (
                _cursor_encode(scope, fences, anchor, (row["created_at"], row["id"]))
                if index + 1 < len(rows)
                else None
            )
            page.entries.append(entry)
            page.next_cursor = next_cursor
            # Keep the first oversized receipt, removing individual excerpts/items with
            # explicit counts. Later receipts continue on the next page without skipping.
            if len(page.model_dump_json().encode()) > MAX_PAGE_BYTES and len(page.entries) > 1:
                page.entries.pop()
                previous = page.entries[-1]
                page.next_cursor = _cursor_encode(
                    scope, fences, anchor, (previous.recorded_at, previous.id)
                )
                break
            while len(page.model_dump_json().encode()) > MAX_PAGE_BYTES and entry.items:
                entry.items.pop()
                entry.omitted_item_count += 1
            if len(page.model_dump_json().encode()) > MAX_PAGE_BYTES:
                raise MemoryInspectionEvidenceError("memory receipt exceeds response bound")
        return page
