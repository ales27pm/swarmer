"""Schema 31: empty, rebuildable per-view vectors and view-specific intentions.

The old canonical vector cache and every existing outbox row remain unchanged.
Migration neither proves historical vectors nor schedules or performs inference.
"""

from __future__ import annotations

import re

import aiosqlite

MEMORY_VIEW_VECTOR_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS memory_view_embeddings (
        memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL CHECK(revision>0),
        view_id TEXT NOT NULL REFERENCES memory_text_views(id) ON DELETE CASCADE,
        provider TEXT NOT NULL CHECK(length(provider)>0),
        source_id TEXT, source_sha256 TEXT NOT NULL, view_sha256 TEXT NOT NULL,
        pipeline_signature TEXT NOT NULL, item_revision TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions>0), vector_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(memory_id,revision,view_id,provider)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_memory_view_embeddings_provider
        ON memory_view_embeddings(provider,memory_id,revision,view_id)""",
)
MEMORY_INDEX_DEDUPE_SQL = """CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_index_dedupe
    ON memory_index_outbox(
        memory_id,revision,operation,COALESCE(view_id,''),COALESCE(provider,''))"""

# Keep the committed schema-29/30 expression explicit. Its replacement changes
# no table, row ID, AUTOINCREMENT counter, owner, generation, or request state.
_LEGACY_DEDUPE_SQL = """CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_index_dedupe
    ON memory_index_outbox(memory_id,revision,operation,COALESCE(provider,''))"""


def _sql(sql: str | None) -> str | None:
    if sql is None:
        return None
    sql = re.sub(r"^(CREATE (?:TABLE|(?:UNIQUE )?INDEX)) IF NOT EXISTS ", r"\1 ", sql)
    # Preserve quoted literals: COALESCE(view_id,' ') is not the reviewed ''.
    return re.sub(
        r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\s+",
        lambda match: "" if match[0].isspace() else match[0],
        sql,
    )


async def _dedupe_sql(db: aiosqlite.Connection) -> str | None:
    row = await (
        await db.execute(
            """SELECT sql FROM sqlite_master
            WHERE type='index' AND name='idx_memory_index_dedupe'
              AND tbl_name='memory_index_outbox'"""
        )
    ).fetchone()
    return _sql(row[0]) if row else None


async def _validate_schema(db: aiosqlite.Connection) -> None:
    rows = await (
        await db.execute(
            """SELECT type,name,sql FROM sqlite_master
            WHERE tbl_name='memory_view_embeddings'
               OR name='idx_memory_view_embeddings_provider'"""
        )
    ).fetchall()
    actual = {(row[0], row[1]): _sql(row[2]) for row in rows}
    expected = {
        ("table", "memory_view_embeddings"): _sql(MEMORY_VIEW_VECTOR_STATEMENTS[0]),
        ("index", "sqlite_autoindex_memory_view_embeddings_1"): None,
        ("index", "idx_memory_view_embeddings_provider"): _sql(MEMORY_VIEW_VECTOR_STATEMENTS[1]),
    }
    if actual != expected or await _dedupe_sql(db) != _sql(MEMORY_INDEX_DEDUPE_SQL):
        raise RuntimeError("memory view vector schema differs from schema 31")


async def initialize_view_vector_schema_locked(db: aiosqlite.Connection) -> None:
    """Install empty storage in the caller's transaction; never alter old data."""
    if not db.in_transaction:
        raise RuntimeError("memory view vector schema requires the caller's transaction")
    dedupe = await _dedupe_sql(db)
    if dedupe not in {_sql(_LEGACY_DEDUPE_SQL), _sql(MEMORY_INDEX_DEDUPE_SQL)}:
        raise RuntimeError("unrecognized memory index deduplication schema")
    for statement in MEMORY_VIEW_VECTOR_STATEMENTS:
        await db.execute(statement)
    if dedupe != _sql(MEMORY_INDEX_DEDUPE_SQL):
        await db.execute("DROP INDEX idx_memory_index_dedupe")
        await db.execute(MEMORY_INDEX_DEDUPE_SQL)
    await _validate_schema(db)


async def migrate_memory_view_vectors(db: aiosqlite.Connection) -> None:
    """Commit exactly 30→31, or verify an already installed schema 31."""
    if db.in_transaction:
        raise RuntimeError("memory view vector migration needs its own transaction")
    await db.execute("BEGIN IMMEDIATE")
    try:
        row = await (await db.execute("PRAGMA user_version")).fetchone()
        version = int(row[0]) if row else -1
        if version == 31:
            await _validate_schema(db)
        elif version == 30:
            existing = await (
                await db.execute(
                    """SELECT name FROM sqlite_master
                    WHERE name IN ('memory_view_embeddings',
                                   'idx_memory_view_embeddings_provider')"""
                )
            ).fetchone()
            if existing is not None:
                raise RuntimeError("schema 30 already contains memory view vector objects")
            await initialize_view_vector_schema_locked(db)
            await db.execute("PRAGMA user_version=31")
        else:
            raise RuntimeError("memory view vector migration requires schema 30")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
