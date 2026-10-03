"""Schema 32: empty expiring local-selection receipts and erasure dependencies."""

from __future__ import annotations

import aiosqlite

from app.services.memory_view_vector_schema import _sql, _validate_schema

LOCAL_CONTEXT_STATEMENTS = (
    """CREATE TABLE memory_local_context_receipts (
        id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
        purpose TEXT NOT NULL CHECK(purpose IN ('goal_plan','tool_proposal')),
        goal_id TEXT, binding_json TEXT NOT NULL, catalogs_json TEXT NOT NULL,
        context_json TEXT NOT NULL, context_sha256 TEXT NOT NULL,
        max_context_bytes INTEGER NOT NULL CHECK(max_context_bytes BETWEEN 0 AND 16384),
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        accepted_target_kind TEXT, accepted_target_id TEXT, accepted_subject_id TEXT,
        proposal_sha256 TEXT, accepted_at TEXT,
        CHECK((accepted_target_kind IS NULL AND accepted_target_id IS NULL AND accepted_subject_id IS NULL AND proposal_sha256 IS NULL AND accepted_at IS NULL)
           OR (accepted_target_kind IS NOT NULL AND accepted_target_id IS NOT NULL AND accepted_subject_id IS NOT NULL AND proposal_sha256 IS NOT NULL AND accepted_at IS NOT NULL))
    )""",
    """CREATE INDEX idx_memory_local_context_device_expiry
        ON memory_local_context_receipts(device_id,expires_at)""",
    """CREATE TABLE memory_local_context_sources (
        receipt_id TEXT NOT NULL REFERENCES memory_local_context_receipts(id) ON DELETE CASCADE,
        memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
        PRIMARY KEY(receipt_id,memory_id)
    )""",
    """CREATE INDEX idx_memory_local_context_source
        ON memory_local_context_sources(memory_id,receipt_id)""",
)


async def _validate_local_schema(db: aiosqlite.Connection) -> None:
    rows = await (
        await db.execute("""SELECT type,name,sql FROM sqlite_master
        WHERE tbl_name IN ('memory_local_context_receipts','memory_local_context_sources')
           OR name IN ('idx_memory_local_context_device_expiry','idx_memory_local_context_source')""")
    ).fetchall()
    expected = {
        ("table", "memory_local_context_receipts"): _sql(LOCAL_CONTEXT_STATEMENTS[0]),
        ("index", "idx_memory_local_context_device_expiry"): _sql(LOCAL_CONTEXT_STATEMENTS[1]),
        ("table", "memory_local_context_sources"): _sql(LOCAL_CONTEXT_STATEMENTS[2]),
        ("index", "idx_memory_local_context_source"): _sql(LOCAL_CONTEXT_STATEMENTS[3]),
        ("index", "sqlite_autoindex_memory_local_context_receipts_1"): None,
        ("index", "sqlite_autoindex_memory_local_context_sources_1"): None,
    }
    if {(r[0], r[1]): _sql(r[2]) for r in rows} != expected:
        raise RuntimeError("local context schema differs from schema 32")


async def migrate_local_context_receipts(db: aiosqlite.Connection) -> None:
    if db.in_transaction:
        raise RuntimeError("local context migration requires its own transaction")
    await db.execute("BEGIN IMMEDIATE")
    try:
        version_row = await (await db.execute("PRAGMA user_version")).fetchone()
        if version_row is None:
            raise RuntimeError("local context schema version unavailable")
        version = version_row[0]
        await _validate_schema(db)
        if version == 31:
            existing = await (
                await db.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'memory_local_context_%' OR name LIKE 'idx_memory_local_context_%'"
                )
            ).fetchall()
            if existing:
                raise RuntimeError("schema 31 already contains local context objects")
            for statement in LOCAL_CONTEXT_STATEMENTS:
                await db.execute(statement)
            await _validate_local_schema(db)
            await db.execute("PRAGMA user_version=32")
        elif version == 32:
            await _validate_local_schema(db)
        else:
            raise RuntimeError("local context migration requires schema 31")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def forget_local_contexts_locked(db: aiosqlite.Connection, memory_id: str) -> None:
    if not db.in_transaction:
        raise RuntimeError("local context erasure requires a transaction")
    # No connection-local foreign-key assumption. Delete every copied context,
    # including multi-source/related-claim context, and all of its dependency edges.
    rows = await (
        await db.execute(
            "SELECT receipt_id FROM memory_local_context_sources WHERE memory_id=?", (memory_id,)
        )
    ).fetchall()
    for (receipt_id,) in rows:
        await db.execute(
            "DELETE FROM memory_local_context_sources WHERE receipt_id=?", (receipt_id,)
        )
        await db.execute("DELETE FROM memory_local_context_receipts WHERE id=?", (receipt_id,))
