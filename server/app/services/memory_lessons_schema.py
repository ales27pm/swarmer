"""Schema34: empty conditional lesson records; no historical promotion/backfill."""

from typing import Any

import aiosqlite

from app.services.memory_view_vector_schema import _sql
from app.services.project_execution_schema import validate_project_execution_schema

LESSON_STATEMENTS = (
    """CREATE TABLE memory_execution_profiles (
        id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
        status TEXT NOT NULL CHECK(status IN ('approved','revoked')),
        body_json TEXT NOT NULL CHECK(length(CAST(body_json AS BLOB))<=32768),
        body_sha256 TEXT NOT NULL CHECK(length(body_sha256)=64),
        actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(id,version)
    )""",
    """CREATE TABLE memory_procedure_lessons (
        id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
        project_id TEXT NOT NULL REFERENCES coding_projects(id) ON DELETE CASCADE,
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('candidate','assessed','quarantined','withdrawn')),
        body_json TEXT NOT NULL CHECK(length(CAST(body_json AS BLOB))<=16384),
        body_sha256 TEXT NOT NULL CHECK(length(body_sha256)=64),
        evidence_set_sha256 TEXT NOT NULL CHECK(length(evidence_set_sha256)=64),
        note_binding_sha256 TEXT CHECK(note_binding_sha256 IS NULL OR length(note_binding_sha256)=64),
        actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(id,version)
    )""",
    """CREATE INDEX idx_memory_procedure_project ON memory_procedure_lessons(project_id,id,version)""",
    """CREATE TABLE memory_procedure_evidence (
        lesson_id TEXT NOT NULL, lesson_version INTEGER NOT NULL,
        acceptance_id TEXT NOT NULL,
        evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
        PRIMARY KEY(lesson_id,lesson_version,acceptance_id),
        FOREIGN KEY(lesson_id,lesson_version) REFERENCES memory_procedure_lessons(id,version) ON DELETE CASCADE
    )""",
    """CREATE TABLE memory_lesson_requests (
        scope TEXT NOT NULL, request_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
        target_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(scope,request_id)
    )""",
)
TABLES = (
    "memory_execution_profiles",
    "memory_procedure_lessons",
    "memory_procedure_evidence",
    "memory_lesson_requests",
)


async def _objects(db: aiosqlite.Connection) -> list[Any]:
    return list(
        await (
            await db.execute("""SELECT type,name,sql FROM sqlite_master
        WHERE name LIKE 'memory_execution_profile%' OR name LIKE 'memory_procedure_%'
        OR name LIKE 'memory_lesson_%' OR name LIKE 'idx_memory_procedure_%'
        OR tbl_name IN ('memory_execution_profiles','memory_procedure_lessons','memory_procedure_evidence','memory_lesson_requests')""")
        ).fetchall()
    )


async def validate_lesson_schema(db: aiosqlite.Connection) -> None:
    await validate_project_execution_schema(db)
    expected = {
        ("table", TABLES[0]): _sql(LESSON_STATEMENTS[0]),
        ("table", TABLES[1]): _sql(LESSON_STATEMENTS[1]),
        ("index", "idx_memory_procedure_project"): _sql(LESSON_STATEMENTS[2]),
        ("table", TABLES[2]): _sql(LESSON_STATEMENTS[3]),
        ("table", TABLES[3]): _sql(LESSON_STATEMENTS[4]),
        **{("index", f"sqlite_autoindex_{name}_1"): None for name in TABLES},
    }
    if {(r[0], r[1]): _sql(r[2]) for r in await _objects(db)} != expected:
        raise RuntimeError("lesson schema differs from schema 34")


async def validate_lesson_prefix(db: aiosqlite.Connection, version: int) -> None:
    if version == 34:
        await validate_lesson_schema(db)
    elif version == 33:
        await validate_project_execution_schema(db)
        if await _objects(db):
            raise RuntimeError("schema 33 already contains lesson objects")
    else:
        raise RuntimeError("lesson prefix requires schema 33 or 34")


async def migrate_memory_lessons(db: aiosqlite.Connection) -> None:
    if db.in_transaction:
        raise RuntimeError("lesson migration requires its own transaction")
    await db.execute("BEGIN IMMEDIATE")
    try:
        row = await (await db.execute("PRAGMA user_version")).fetchone()
        version = int(row[0]) if row else -1
        await validate_lesson_prefix(db, version)
        if version == 33:
            for statement in LESSON_STATEMENTS:
                await db.execute(statement)
            await validate_lesson_schema(db)
            await db.execute("PRAGMA user_version=34")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
