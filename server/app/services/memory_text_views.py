"""Versioned private text views and durable, fenced SQL embedding intentions.

The domain transaction owns creation/deletion. This is deliberately separate
from the MessageBoard publication outbox: neither text nor vectors are events.
Each current original/canonical view has its own provider projection in schema31.
The canonical (or legacy original) index view also maintains the compatibility cache.
An unknown dispatched request fences this projection queue across memories and
revisions. It is not a global GPU lock or evidence that remote inference stopped;
other model callers retain their own admission and cancellation contracts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from app.services.memory_text_fingerprints import (
    _hash as _hash,  # noqa: PLC0414 - compatibility re-export
)
from app.services.memory_text_fingerprints import (
    _json as _json,  # noqa: PLC0414 - compatibility re-export
)
from app.services.memory_text_fingerprints import (
    text_view_sha256 as text_view_sha256,  # noqa: PLC0414 - compatibility re-export
)
from app.services.memory_vectors import memory_vector
from app.services.memory_view_qualification import qualify_search_rows

MEMORY_TEXT_VIEW_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS memory_text_heads (
        memory_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
        item_revision TEXT, source_id TEXT, source_sha256 TEXT, view_set_sha256 TEXT,
        index_view_id TEXT, deleted INTEGER NOT NULL CHECK(deleted IN (0,1)), updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS memory_text_views (
        id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        role TEXT NOT NULL CHECK(role IN ('original','canonical')), language TEXT NOT NULL,
        pipeline_signature TEXT NOT NULL, content TEXT NOT NULL, summary TEXT,
        text_sha256 TEXT NOT NULL, source_id TEXT, source_sha256 TEXT NOT NULL,
        scope TEXT NOT NULL, kind TEXT NOT NULL, sensitivity TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(memory_id,revision,role)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_text_view_head ON memory_text_views(memory_id,revision)",
    """CREATE TABLE IF NOT EXISTS memory_index_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
        view_id TEXT, provider TEXT,
        operation TEXT NOT NULL CHECK(operation IN ('upsert','delete')),
        status TEXT NOT NULL CHECK(status IN ('pending','claimed','completed','obsolete')),
        owner TEXT, generation INTEGER NOT NULL DEFAULT 0, lease_expires_at TEXT,
        request_state TEXT NOT NULL DEFAULT 'not_sent'
            CHECK(request_state IN ('not_sent','in_flight','known')),
        dispatched_at TEXT, response_received_at TEXT,
        claim_count INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
        error_category TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((operation='upsert' AND view_id IS NOT NULL) OR
              (operation='delete' AND view_id IS NULL AND provider IS NULL))
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_index_dedupe ON memory_index_outbox(
        memory_id,revision,operation,COALESCE(provider,''))""",
    """CREATE INDEX IF NOT EXISTS idx_memory_index_pending ON memory_index_outbox(
        status,available_at,lease_expires_at,id)""",
    """CREATE INDEX IF NOT EXISTS idx_memory_index_inflight ON memory_index_outbox(id)
        WHERE request_state='in_flight'""",
)
MEMORY_TEXT_VIEWS_SCHEMA = ";\n".join(MEMORY_TEXT_VIEW_STATEMENTS) + ";\n"
ERROR_CATEGORIES = frozenset(
    {
        "provider_unavailable",
        "provider_changed",
        "invalid_vector",
        "source_conflict",
        "lease_lost",
        "cancelled",
        "projection_error",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectionClaim:
    event_id: int
    memory_id: str
    revision: int
    view_id: str | None
    provider: str | None
    operation: Literal["upsert", "delete"]
    owner: str
    generation: int
    lease_expires_at: str


@dataclass(frozen=True, slots=True)
class ProjectionSource:
    memory_id: str
    revision: int
    view_id: str
    content: str
    summary: str | None
    scope: str
    kind: str
    sensitivity: str
    view_sha256: str
    source_sha256: str
    pipeline_signature: str
    item_revision: str
    source_id: str | None = None
    role: str = "original"
    language: str = "und"
    is_index_view: bool = False


def _time(value: str | None = None) -> str:
    parsed = datetime.fromisoformat(value) if value is not None else datetime.now(UTC)
    if parsed.tzinfo is None:
        raise ValueError("projection timestamp must have a timezone")
    return parsed.astimezone(UTC).isoformat()


def _later(now: str, seconds: int) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat()


def _locked(db: aiosqlite.Connection) -> None:
    if not db.in_transaction:
        raise RuntimeError("memory projection mutation requires the domain transaction")


async def _one(db: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
    cursor = await db.execute(sql, args)
    row = await cursor.fetchone()
    return (
        dict(zip((column[0] for column in cursor.description or ()), row, strict=True))
        if row
        else None
    )


async def _row(db: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> aiosqlite.Row | None:
    cursor = await db.execute(sql, args)
    cursor.row_factory = aiosqlite.Row
    return await cursor.fetchone()


async def initialize_text_view_schema_locked(db: aiosqlite.Connection) -> None:
    _locked(db)
    # executescript commits an existing transaction, so execute each DDL separately.
    for statement in MEMORY_TEXT_VIEW_STATEMENTS:
        await db.execute(statement)


async def _enqueue(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    revision: int,
    view_id: str | None,
    provider: str | None,
    operation: str,
    now: str,
) -> None:
    if operation == "upsert" and provider is None:
        existing = await _one(
            db,
            """SELECT id FROM memory_index_outbox WHERE memory_id=? AND revision=?
                AND view_id=? AND operation='upsert' AND status<>'obsolete' LIMIT 1""",
            (memory_id, revision, view_id),
        )
        if existing is not None:
            return
    await db.execute(
        """INSERT OR IGNORE INTO memory_index_outbox(
            memory_id,revision,view_id,provider,operation,status,available_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,'pending',?,?,?)""",
        (memory_id, revision, view_id, provider, operation, now, now, now),
    )


async def record_text_views_locked(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    item_revision: str,
    original_content: str,
    original_summary: str | None,
    original_language: str,
    source_id: str | None,
    source_sha256: str,
    canonical_content: str | None = None,
    canonical_summary: str | None = None,
    normalization_signature: str | None = None,
    embedding_provider: str | None = None,
    now: str | None = None,
    migration_seed: bool = False,
) -> int:
    _locked(db)
    timestamp = _time(now)
    if source_sha256 != text_view_sha256(original_content, original_summary):
        raise ValueError("original text digest does not match")
    if not original_language or len(original_language) > 64:
        raise ValueError("original language is invalid")
    if embedding_provider == "":
        raise ValueError("embedding identity is empty")
    if canonical_content is not None and (not normalization_signature or not source_id):
        raise ValueError("canonical view requires its source and pipeline signature")
    item = await _one(db, "SELECT * FROM memory_items WHERE id=?", (memory_id,))
    selected_content = canonical_content if canonical_content is not None else original_content
    selected_summary = canonical_summary if canonical_content is not None else original_summary
    if (
        item is None
        or item["updated_at"] != item_revision
        or item["content"] != selected_content
        or item["summary"] != selected_summary
    ):
        raise ValueError("memory text changed before view creation")
    common = {
        "source_id": source_id,
        "source_sha256": source_sha256,
        "scope": item["scope"],
        "kind": item["kind"],
        "sensitivity": item["sensitivity"],
    }
    views = [
        {
            "role": "original",
            "language": original_language,
            "pipeline_signature": "original-v1",
            "content": original_content,
            "summary": original_summary,
            **common,
        }
    ]
    if canonical_content is not None:
        views.append(
            {
                "role": "canonical",
                "language": "en",
                "pipeline_signature": normalization_signature,
                "content": canonical_content,
                "summary": canonical_summary,
                **common,
            }
        )
    digest = _hash(views)
    head = await _one(db, "SELECT * FROM memory_text_heads WHERE memory_id=?", (memory_id,))
    if migration_seed and head is not None:
        raise ValueError("migration seed requires a memory without a text head")
    if head is not None and not head["deleted"] and head["view_set_sha256"] == digest:
        await refresh_text_view_head_locked(db, memory_id, item_revision, timestamp)
        for view in reversed(views):
            await _enqueue(
                db,
                memory_id=memory_id,
                revision=head["revision"],
                view_id="mtv_" + _hash([memory_id, head["revision"], view]),
                provider=embedding_provider,
                operation="upsert",
                now=timestamp,
            )
        return int(head["revision"])
    revision = int(head["revision"]) + 1 if head else 1
    view_id = ""
    view_ids = []
    for view in views:
        view_id = "mtv_" + _hash([memory_id, revision, view])
        view_ids.append(view_id)
        await db.execute(
            """INSERT INTO memory_text_views(id,memory_id,revision,role,language,pipeline_signature,
                content,summary,text_sha256,source_id,source_sha256,scope,kind,sensitivity,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                view_id,
                memory_id,
                revision,
                view["role"],
                view["language"],
                view["pipeline_signature"],
                view["content"],
                view["summary"],
                text_view_sha256(str(view["content"]), view["summary"]),
                source_id,
                source_sha256,
                item["scope"],
                item["kind"],
                item["sensitivity"],
                timestamp,
            ),
        )
    await db.execute(
        """INSERT INTO memory_text_heads(memory_id,revision,item_revision,source_id,source_sha256,
            view_set_sha256,index_view_id,deleted,updated_at) VALUES(?,?,?,?,?,?,?,0,?)
            ON CONFLICT(memory_id) DO UPDATE SET revision=excluded.revision,
            item_revision=excluded.item_revision,source_id=excluded.source_id,
            source_sha256=excluded.source_sha256,view_set_sha256=excluded.view_set_sha256,
            index_view_id=excluded.index_view_id,deleted=0,updated_at=excluded.updated_at""",
        (memory_id, revision, item_revision, source_id, source_sha256, digest, view_id, timestamp),
    )
    if not migration_seed:
        await db.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
        await db.execute("DELETE FROM memory_view_embeddings WHERE memory_id=?", (memory_id,))
    await db.execute(
        """UPDATE memory_index_outbox SET status='obsolete',
            owner=CASE WHEN request_state='in_flight' THEN owner ELSE NULL END,
            lease_expires_at=CASE WHEN request_state='in_flight' THEN lease_expires_at ELSE NULL END,
            error_category=NULL,updated_at=? WHERE memory_id=? AND revision<>?
            AND status IN ('pending','claimed')""",
        (timestamp, memory_id, revision),
    )
    # Historical migration prefixes still seed exactly the old index intent.
    # Normal schema31 writes enqueue both views, retaining canonical-first order.
    for selected_view_id in [view_id] if migration_seed else reversed(view_ids):
        await _enqueue(
            db,
            memory_id=memory_id,
            revision=revision,
            view_id=selected_view_id,
            provider=embedding_provider,
            operation="upsert",
            now=timestamp,
        )
    return revision


async def refresh_text_view_head_locked(
    db: aiosqlite.Connection, memory_id: str, item_revision: str, now: str | None = None
) -> None:
    _locked(db)
    previous = await _one(
        db,
        "SELECT item_revision FROM memory_text_heads WHERE memory_id=? AND deleted=0",
        (memory_id,),
    )
    await db.execute(
        """UPDATE memory_text_heads SET item_revision=?,updated_at=?
            WHERE memory_id=? AND deleted=0 AND EXISTS(
              SELECT 1 FROM memory_items m JOIN memory_text_views v
                ON v.id=memory_text_heads.index_view_id
              WHERE m.id=memory_text_heads.memory_id AND m.updated_at=?
                AND m.content=v.content AND m.summary IS v.summary
                AND m.scope=v.scope AND m.kind=v.kind AND m.sensitivity=v.sensitivity)""",
        (item_revision, _time(now), memory_id, item_revision),
    )
    if previous is not None:
        await db.execute(
            """UPDATE memory_view_embeddings SET item_revision=? WHERE memory_id=? AND item_revision=?
                AND EXISTS(SELECT 1 FROM memory_text_heads h JOIN memory_text_views v
                  ON v.memory_id=h.memory_id AND v.revision=h.revision
                  WHERE h.memory_id=memory_view_embeddings.memory_id AND h.deleted=0
                    AND h.item_revision=? AND h.revision=memory_view_embeddings.revision
                    AND v.id=memory_view_embeddings.view_id
                    AND v.text_sha256=memory_view_embeddings.view_sha256
                    AND v.source_id IS memory_view_embeddings.source_id
                    AND v.source_sha256=memory_view_embeddings.source_sha256
                    AND v.pipeline_signature=memory_view_embeddings.pipeline_signature)""",
            (item_revision, memory_id, previous["item_revision"], item_revision),
        )


async def delete_text_views_locked(
    db: aiosqlite.Connection, *, memory_id: str, now: str | None = None
) -> None:
    _locked(db)
    timestamp = _time(now)
    head = await _one(db, "SELECT * FROM memory_text_heads WHERE memory_id=?", (memory_id,))
    revision = (
        int(head["revision"])
        if head and head["deleted"]
        else int(head["revision"]) + 1
        if head
        else 1
    )
    await db.execute("DELETE FROM memory_view_embeddings WHERE memory_id=?", (memory_id,))
    await db.execute("DELETE FROM memory_text_views WHERE memory_id=?", (memory_id,))
    await db.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
    await db.execute(
        """INSERT INTO memory_text_heads(memory_id,revision,deleted,updated_at) VALUES(?,?,1,?)
            ON CONFLICT(memory_id) DO UPDATE SET revision=excluded.revision,deleted=1,
            item_revision=NULL,source_id=NULL,source_sha256=NULL,view_set_sha256=NULL,
            index_view_id=NULL,updated_at=excluded.updated_at""",
        (memory_id, revision, timestamp),
    )
    await db.execute(
        """UPDATE memory_index_outbox SET status='obsolete',
            owner=CASE WHEN request_state='in_flight' THEN owner ELSE NULL END,
            lease_expires_at=CASE WHEN request_state='in_flight' THEN lease_expires_at ELSE NULL END,
            error_category=NULL,updated_at=? WHERE memory_id=? AND operation='upsert'
            AND status IN ('pending','claimed')""",
        (timestamp, memory_id),
    )
    await _enqueue(
        db,
        memory_id=memory_id,
        revision=revision,
        view_id=None,
        provider=None,
        operation="delete",
        now=timestamp,
    )


def _claim(row: dict[str, Any]) -> ProjectionClaim:
    return ProjectionClaim(
        int(row["id"]),
        str(row["memory_id"]),
        int(row["revision"]),
        row["view_id"],
        row["provider"],
        row["operation"],
        str(row["owner"]),
        int(row["generation"]),
        str(row["lease_expires_at"]),
    )


async def claim_projection_batch(
    db_path: Path,
    owner: str,
    *,
    limit: int = 16,
    lease_seconds: int = 60,
    memory_id: str | None = None,
    revision: int | None = None,
    provider: str | None = None,
    now: str | None = None,
) -> list[ProjectionClaim]:
    if (
        not owner
        or len(owner) > 200
        or not 1 <= limit <= 16
        or not 1 <= lease_seconds <= 900
        or provider == ""
        or (revision is not None and (type(revision) is not int or revision < 1 or not memory_id))
    ):
        raise ValueError("invalid projection claim parameters")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        timestamp = _time(now)  # after acquiring the writer lock
        cursor = await db.execute(
            """SELECT o.* FROM memory_index_outbox o JOIN memory_text_heads h ON h.memory_id=o.memory_id
                WHERE ((o.status='pending' AND o.available_at<=?) OR
                  (o.status='claimed' AND o.lease_expires_at<=?))
                AND o.request_state<>'in_flight'
                AND (o.operation='delete' OR NOT EXISTS(
                    SELECT 1 FROM memory_index_outbox uncertain WHERE uncertain.request_state='in_flight'))
                AND h.revision=o.revision AND ((o.operation='delete' AND h.deleted=1) OR
                  (o.operation='upsert' AND h.deleted=0 AND EXISTS(SELECT 1 FROM memory_text_views v
                    WHERE v.id=o.view_id AND v.memory_id=o.memory_id AND v.revision=o.revision)))
                AND (? IS NULL OR o.memory_id=?)
                AND (? IS NULL OR o.revision=?)
                AND (o.operation='delete' OR (? IS NULL AND o.provider IS NOT NULL) OR
                     (? IS NOT NULL AND (o.provider=? OR o.provider IS NULL)))
                ORDER BY o.id LIMIT ?""",
            (
                timestamp,
                timestamp,
                memory_id,
                memory_id,
                revision,
                revision,
                provider,
                provider,
                provider,
                limit * 2,
            ),
        )
        names = [column[0] for column in cursor.description or ()]
        selected = [dict(zip(names, row, strict=True)) for row in await cursor.fetchall()]
        claims: list[ProjectionClaim] = []
        for row in selected:
            if len(claims) == limit:
                break
            identity = row["provider"] or (provider if row["operation"] == "upsert" else None)
            # A formerly unconfigured intention may already have an explicit-provider sibling.
            duplicate = (
                await _one(
                    db,
                    """SELECT id FROM memory_index_outbox
                WHERE memory_id=? AND revision=? AND view_id=? AND operation='upsert' AND provider=? AND id<>?""",
                    (row["memory_id"], row["revision"], row["view_id"], identity, row["id"]),
                )
                if identity
                else None
            )
            if row["provider"] is None and duplicate is not None:
                await db.execute(
                    "UPDATE memory_index_outbox SET status='obsolete',updated_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
                continue
            row.update(
                provider=identity,
                owner=owner,
                generation=int(row["generation"]) + 1,
                lease_expires_at=_later(timestamp, lease_seconds),
            )
            await db.execute(
                """UPDATE memory_index_outbox SET status='claimed',provider=?,owner=?,
                generation=?,lease_expires_at=?,claim_count=claim_count+1,updated_at=?,
                request_state='not_sent',dispatched_at=NULL,response_received_at=NULL WHERE id=?""",
                (identity, owner, row["generation"], row["lease_expires_at"], timestamp, row["id"]),
            )
            claims.append(_claim(row))
        await db.commit()
        return claims


async def _owns(
    db: aiosqlite.Connection,
    claim: ProjectionClaim,
    now: str,
    *,
    allow_expired: bool = False,
    allow_obsolete: bool = False,
    require_settled: bool = False,
) -> bool:
    row = await _one(
        db,
        """SELECT id FROM memory_index_outbox WHERE id=? AND memory_id=?
        AND revision=? AND view_id IS ? AND provider IS ? AND operation=?
        AND (status='claimed' OR (? AND status='obsolete')) AND owner=? AND generation=?
        AND (? OR lease_expires_at>?) AND (?=0 OR request_state<>'in_flight')""",
        (
            claim.event_id,
            claim.memory_id,
            claim.revision,
            claim.view_id,
            claim.provider,
            claim.operation,
            allow_obsolete,
            claim.owner,
            claim.generation,
            allow_expired,
            now,
            require_settled,
        ),
    )
    return row is not None


async def reserve_projection_batch(
    db_path: Path,
    owner: str,
    *,
    snapshots: list[dict[str, Any]],
    provider: str,
    lease_seconds: int = 60,
    now: str | None = None,
) -> list[ProjectionClaim]:
    """Reserve an explicit backfill page atomically before its single HTTP call.

    An empty result means no item was reserved. Snapshots retain caller order;
    a changed source, live claim or unknown request refuses the whole page.
    """
    if (
        not owner
        or len(owner) > 200
        or not provider
        or not 1 <= len(snapshots) <= 100
        or not 1 <= lease_seconds <= 900
        or len({(item["id"], item["projection_view_id"]) for item in snapshots}) != len(snapshots)
    ):
        raise ValueError("invalid explicit projection page")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        timestamp = _time(now)
        if await _one(
            db, "SELECT id FROM memory_index_outbox WHERE request_state='in_flight' LIMIT 1", ()
        ):
            return []
        claims: list[ProjectionClaim] = []
        for item in snapshots:
            memory_id = str(item["id"])
            revision = int(item["projection_revision"])
            view_id = str(item["projection_view_id"])
            source = await _source(db, memory_id, revision, view_id)
            if (
                source is None
                or source.item_revision != item["updated_at"]
                or source.source_sha256 != item["projection_source_sha256"]
                or source.content != item["content"]
                or source.summary != item["summary"]
            ):
                return []
            busy = await _one(
                db,
                """SELECT id FROM memory_index_outbox WHERE memory_id=?
                AND revision=? AND view_id=? AND operation='upsert' AND (provider=? OR provider IS NULL)
                AND status='claimed' AND lease_expires_at>?""",
                (memory_id, revision, view_id, provider, timestamp),
            )
            if busy is not None:
                return []
            await _enqueue(
                db,
                memory_id=memory_id,
                revision=revision,
                view_id=view_id,
                provider=provider,
                operation="upsert",
                now=timestamp,
            )
            row = await _one(
                db,
                """SELECT * FROM memory_index_outbox WHERE memory_id=?
                AND revision=? AND provider=? AND operation='upsert' AND view_id=?""",
                (memory_id, revision, provider, view_id),
            )
            if row is None or row["status"] == "obsolete":
                return []
            row.update(
                owner=owner,
                generation=int(row["generation"]) + 1,
                lease_expires_at=_later(timestamp, lease_seconds),
            )
            await db.execute(
                """UPDATE memory_index_outbox SET status='claimed',owner=?,generation=?,
                lease_expires_at=?,request_state='not_sent',dispatched_at=NULL,response_received_at=NULL,
                claim_count=claim_count+1,updated_at=? WHERE id=?""",
                (owner, row["generation"], row["lease_expires_at"], timestamp, row["id"]),
            )
            # Prevent an older unbound intent from admitting a second projection.
            await db.execute(
                """UPDATE memory_index_outbox SET status='obsolete',owner=NULL,
                lease_expires_at=NULL,updated_at=? WHERE memory_id=? AND revision=? AND view_id=?
                AND operation='upsert' AND provider IS NULL AND request_state<>'in_flight'""",
                (timestamp, memory_id, revision, view_id),
            )
            claims.append(_claim(row))
        await db.commit()
        return claims


def _check_claim_batch(claims: list[ProjectionClaim]) -> None:
    if (
        not 1 <= len(claims) <= 100
        or len({c.event_id for c in claims}) != len(claims)
        or len({c.owner for c in claims}) != 1
        or len({c.provider for c in claims}) != 1
        or any(c.operation != "upsert" or c.provider is None for c in claims)
    ):
        raise ValueError("invalid projection request batch")


async def mark_projection_batch_dispatched(
    db_path: Path,
    claims: list[ProjectionClaim],
    *,
    now: str | None = None,
) -> bool:
    """Persist uncertainty before crossing HTTP; never clear it on lease expiry."""
    _check_claim_batch(claims)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        timestamp = _time(now)
        if await _one(
            db, "SELECT id FROM memory_index_outbox WHERE request_state='in_flight' LIMIT 1", ()
        ):
            return False
        for claim in claims:
            if not await _owns(db, claim, timestamp):
                return False
            state = await _one(
                db, "SELECT request_state FROM memory_index_outbox WHERE id=?", (claim.event_id,)
            )
            if state is None or state["request_state"] != "not_sent":
                return False
            if await _source(db, claim.memory_id, claim.revision, claim.view_id) is None:
                return False
        for claim in claims:
            await db.execute(
                """UPDATE memory_index_outbox SET request_state='in_flight',
                dispatched_at=?,response_received_at=NULL,updated_at=? WHERE id=?""",
                (timestamp, timestamp, claim.event_id),
            )
        await db.commit()
        return True


async def mark_projection_dispatched(
    db_path: Path,
    claim: ProjectionClaim,
    *,
    now: str | None = None,
) -> bool:
    return await mark_projection_batch_dispatched(db_path, [claim], now=now)


async def mark_projection_batch_response_received(
    db_path: Path,
    claims: list[ProjectionClaim],
    *,
    now: str | None = None,
) -> bool:
    """Only the dispatching owner can record a received HTTP outcome, even late.

    This is not proof of semantic/vector validity and never acknowledges an
    index. Obsolete text stays obsolete; late responses cannot restore it.
    """
    _check_claim_batch(claims)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        timestamp = _time(now)
        for claim in claims:
            if not await _owns(db, claim, timestamp, allow_expired=True, allow_obsolete=True):
                return False
            state = await _one(
                db, "SELECT request_state FROM memory_index_outbox WHERE id=?", (claim.event_id,)
            )
            if state is None or state["request_state"] not in {"in_flight", "known"}:
                return False
        for claim in claims:
            await db.execute(
                """UPDATE memory_index_outbox SET request_state='known',
                response_received_at=COALESCE(response_received_at,?),updated_at=? WHERE id=?""",
                (timestamp, timestamp, claim.event_id),
            )
        await db.commit()
        return True


async def mark_projection_response_received(
    db_path: Path,
    claim: ProjectionClaim,
    *,
    now: str | None = None,
) -> bool:
    return await mark_projection_batch_response_received(db_path, [claim], now=now)


async def _source(
    db: aiosqlite.Connection, memory_id: str, revision: int, view_id: str | None
) -> ProjectionSource | None:
    item = await _row(db, "SELECT * FROM memory_items WHERE id=?", (memory_id,))
    if item is None:
        return None
    qualified = (await qualify_search_rows(db, [item])).get(memory_id)
    # Retrieval deliberately preserves canonical qualification errors. Projection
    # requires a fully qualified original/receipt even for its canonical view.
    if qualified is None or qualified.view_token is None or qualified.original is None:
        return None
    row = await _one(
        db,
        """SELECT v.*,h.item_revision,h.index_view_id FROM memory_text_views v
        JOIN memory_text_heads h ON h.memory_id=v.memory_id AND h.revision=v.revision
        WHERE h.memory_id=? AND h.revision=? AND v.id=? AND h.deleted=0""",
        (memory_id, revision, view_id),
    )
    if row is None:
        return None
    return ProjectionSource(
        memory_id,
        revision,
        str(view_id),
        row["content"],
        row["summary"],
        row["scope"],
        row["kind"],
        row["sensitivity"],
        row["text_sha256"],
        row["source_sha256"],
        row["pipeline_signature"],
        row["item_revision"],
        row["source_id"],
        row["role"],
        row["language"],
        row["id"] == row["index_view_id"],
    )


async def read_projection_source(
    db_path: Path, claim: ProjectionClaim, *, now: str | None = None
) -> ProjectionSource | None:
    if claim.operation != "upsert":
        return None
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN")
        if not await _owns(db, claim, _time(now)):
            return None
        return await _source(db, claim.memory_id, claim.revision, claim.view_id)


async def finish_projection_locked(
    db: aiosqlite.Connection,
    claim: ProjectionClaim,
    *,
    vector: list[float] | None,
    now: str | None = None,
) -> bool:
    _locked(db)
    timestamp = _time(now)
    if not await _owns(db, claim, timestamp, require_settled=True):
        return False
    if claim.operation == "delete":
        head = await _one(
            db,
            "SELECT revision,deleted FROM memory_text_heads WHERE memory_id=?",
            (claim.memory_id,),
        )
        if head is None or not head["deleted"] or head["revision"] != claim.revision:
            return False
        await db.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (claim.memory_id,))
        await db.execute("DELETE FROM memory_view_embeddings WHERE memory_id=?", (claim.memory_id,))
    else:
        checked_vector = memory_vector(vector)
        if checked_vector is None:
            raise ValueError("projection vector is invalid")
        source = await _source(db, claim.memory_id, claim.revision, claim.view_id)
        if source is None or not claim.provider:
            return False
        await db.execute(
            """INSERT OR REPLACE INTO memory_view_embeddings(
                memory_id,revision,view_id,provider,source_id,source_sha256,view_sha256,
                pipeline_signature,item_revision,dimensions,vector_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                claim.memory_id,
                claim.revision,
                claim.view_id,
                claim.provider,
                source.source_id,
                source.source_sha256,
                source.view_sha256,
                source.pipeline_signature,
                source.item_revision,
                len(checked_vector),
                _json(checked_vector),
                timestamp,
            ),
        )
        if source.is_index_view:
            await db.execute(
                """INSERT OR REPLACE INTO memory_embeddings(
                memory_id,provider,dimensions,vector_json,updated_at) VALUES(?,?,?,?,?)""",
                (
                    claim.memory_id,
                    claim.provider,
                    len(checked_vector),
                    _json(checked_vector),
                    source.item_revision,
                ),
            )
    await db.execute(
        """UPDATE memory_index_outbox SET status='completed',owner=NULL,
        lease_expires_at=NULL,error_category=NULL,updated_at=? WHERE id=?""",
        (timestamp, claim.event_id),
    )
    return True


async def complete_current_projection_locked(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    item_revision: str,
    provider: str,
    revision: int,
    view_id: str,
    source_sha256: str,
    now: str | None = None,
) -> bool:
    """Acknowledge explicit backfill after its vector INSERT, without stealing a lease.

    Caller MUST roll back its vector INSERT when this returns False.
    """
    _locked(db)
    timestamp = _time(now)
    source = await _source(db, memory_id, revision, view_id)
    if (
        source is None
        or source.item_revision != item_revision
        or source.source_sha256 != source_sha256
    ):
        return False
    vector = await _one(
        db,
        """SELECT * FROM memory_view_embeddings WHERE memory_id=? AND revision=? AND view_id=? AND provider=?""",
        (memory_id, revision, view_id, provider),
    )
    busy = await _one(
        db,
        """SELECT id FROM memory_index_outbox WHERE request_state='in_flight' OR (memory_id=? AND revision=? AND view_id=?
        AND operation='upsert' AND (provider=? OR provider IS NULL)
        AND status='claimed' AND lease_expires_at>?)""",
        (memory_id, revision, view_id, provider, timestamp),
    )
    if (
        vector is None
        or vector["item_revision"] != item_revision
        or busy is not None
        or vector["source_id"] != source.source_id
        or vector["source_sha256"] != source.source_sha256
        or vector["view_sha256"] != source.view_sha256
        or vector["pipeline_signature"] != source.pipeline_signature
    ):
        return False
    try:
        if memory_vector(json.loads(vector["vector_json"]), vector["dimensions"]) is None:
            return False
    except (TypeError, ValueError):
        return False
    # Keep provider binding explicit; an unconfigured intent is superseded by the verified projection.
    await _enqueue(
        db,
        memory_id=memory_id,
        revision=revision,
        view_id=view_id,
        provider=provider,
        operation="upsert",
        now=timestamp,
    )
    await db.execute(
        """UPDATE memory_index_outbox SET status='completed',owner=NULL,
        lease_expires_at=NULL,error_category=NULL,updated_at=? WHERE memory_id=? AND revision=? AND view_id=?
        AND operation='upsert' AND provider=? AND status<>'obsolete'""",
        (timestamp, memory_id, revision, view_id, provider),
    )
    await db.execute(
        """UPDATE memory_index_outbox SET status='obsolete',owner=NULL,
        lease_expires_at=NULL,error_category=NULL,updated_at=? WHERE memory_id=? AND revision=? AND view_id=?
        AND operation='upsert' AND provider IS NULL AND status IN ('pending','claimed')""",
        (timestamp, memory_id, revision, view_id),
    )
    return True


async def fail_projection_claim(
    db_path: Path,
    claim: ProjectionClaim,
    *,
    error_category: str,
    retry_seconds: int = 30,
    now: str | None = None,
) -> bool:
    if error_category not in ERROR_CATEGORIES or not 0 <= retry_seconds <= 86_400:
        raise ValueError("invalid projection failure category or delay")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        timestamp = _time(now)
        if not await _owns(db, claim, timestamp, require_settled=True):
            return False
        await db.execute(
            """UPDATE memory_index_outbox SET status='pending',owner=NULL,lease_expires_at=NULL,
            error_category=?,available_at=?,updated_at=? WHERE id=?""",
            (error_category, _later(timestamp, retry_seconds), timestamp, claim.event_id),
        )
        await db.commit()
        return True


async def retry_known_projection_failure(
    db_path: Path,
    *,
    memory_id: str,
    provider: str,
    revision: int | None = None,
    now: str | None = None,
) -> bool:
    """An explicit retry may wake a known failure, never an uncertain live lease."""
    if (
        not memory_id
        or not provider
        or (revision is not None and (type(revision) is not int or revision < 1))
    ):
        raise ValueError("explicit retry requires a memory and provider identity")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        timestamp = _time(now)
        cursor = await db.execute(
            """UPDATE memory_index_outbox SET available_at=?,updated_at=?
            WHERE memory_id=? AND provider=? AND operation='upsert'
              AND (? IS NULL OR revision=?)
              AND status='pending' AND owner IS NULL AND available_at>?
              AND request_state<>'in_flight'
              AND NOT EXISTS(SELECT 1 FROM memory_index_outbox unknown
                             WHERE unknown.request_state='in_flight')
              AND error_category IN ('provider_unavailable','provider_changed','invalid_vector',
                                     'source_conflict','projection_error')
              AND EXISTS(SELECT 1 FROM memory_text_heads h
                WHERE h.memory_id=memory_index_outbox.memory_id AND h.deleted=0
                  AND h.revision=memory_index_outbox.revision
                  AND EXISTS(SELECT 1 FROM memory_text_views v WHERE v.id=memory_index_outbox.view_id
                    AND v.memory_id=h.memory_id AND v.revision=h.revision))""",
            (timestamp, timestamp, memory_id, provider, revision, revision, timestamp),
        )
        await db.commit()
        return cursor.rowcount > 0


async def projection_status(
    db_path: Path,
    *,
    memory_id: str | None = None,
    provider: str | None = None,
    now: str | None = None,
) -> dict[str, int]:
    timestamp = _time(now)
    async with aiosqlite.connect(db_path) as db:
        row = await _one(
            db,
            """SELECT
            SUM(status='pending') AS pending,SUM(status='claimed') AS claimed,
            SUM(status='completed') AS completed,SUM(status='obsolete') AS obsolete,
            SUM(status IN ('pending','claimed') AND operation='upsert' AND ? IS NULL)
                AS configuration_missing,
            SUM(status IN ('pending','claimed') AND operation='upsert' AND provider IS NULL)
                AS unbound_provider,
            SUM(status IN ('pending','claimed') AND operation='upsert' AND provider IS NOT NULL
                AND ? IS NOT NULL AND provider<>?) AS provider_mismatch,
            SUM(status='claimed' AND lease_expires_at<=?) AS expired_claims,
            SUM(status IN ('pending','claimed') AND error_category='provider_unavailable')
                AS provider_unavailable,
            SUM(status IN ('pending','claimed') AND error_category='invalid_vector') AS invalid_vector
            ,SUM(request_state='in_flight') AS request_outcome_unknown
            FROM memory_index_outbox WHERE (? IS NULL OR memory_id=?)""",
            (provider, provider, provider, timestamp, memory_id, memory_id),
        )
    return {key: int(value or 0) for key, value in (row or {}).items()}
