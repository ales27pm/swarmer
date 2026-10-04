"""Schema 33: empty server-bound worker reports, never inferred validation."""

from __future__ import annotations

import aiosqlite

from app.services.memory_local_context_schema import _validate_local_schema
from app.services.memory_view_vector_schema import _sql, _validate_schema

PROJECT_EXECUTION_STATEMENTS = (
    """CREATE TABLE project_execution_acceptances (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        producer_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
        lease_id TEXT NOT NULL,
        lease_generation INTEGER NOT NULL CHECK(lease_generation > 0),
        claimed_at TEXT NOT NULL, accepted_at TEXT NOT NULL,
        project_id TEXT REFERENCES coding_projects(id) ON DELETE CASCADE,
        goal_run_id TEXT REFERENCES goal_runs(id) ON DELETE CASCADE,
        node_id TEXT REFERENCES plan_nodes(id) ON DELETE CASCADE,
        conversation_revision INTEGER CHECK(conversation_revision >= 0),
        run_id TEXT NOT NULL CHECK(length(run_id)=32),
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
        result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64),
        receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
        source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
        observation_status TEXT NOT NULL CHECK(observation_status IN ('complete','incomplete')),
        producer_json TEXT NOT NULL CHECK(length(CAST(producer_json AS BLOB)) <= 2048),
        UNIQUE(job_id,lease_generation), UNIQUE(run_id),
        CHECK((project_id IS NULL AND goal_run_id IS NULL AND node_id IS NULL AND conversation_revision IS NULL)
           OR (project_id IS NOT NULL AND goal_run_id IS NOT NULL AND node_id IS NOT NULL AND conversation_revision IS NOT NULL))
    )""",
    """CREATE INDEX idx_project_execution_acceptance_project
        ON project_execution_acceptances(project_id,accepted_at,id)""",
    """CREATE TABLE project_execution_revision_links (
        acceptance_id TEXT PRIMARY KEY REFERENCES project_execution_acceptances(id) ON DELETE CASCADE,
        revision_id TEXT NOT NULL UNIQUE REFERENCES project_revisions(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL REFERENCES coding_projects(id) ON DELETE CASCADE,
        source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
        snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
        linked_at TEXT NOT NULL
    )""",
    """CREATE INDEX idx_project_execution_revision_project
        ON project_execution_revision_links(project_id,revision_id)""",
)


async def validate_project_execution_schema(db: aiosqlite.Connection) -> None:
    await _validate_schema(db)
    await _validate_local_schema(db)
    rows = await (
        await db.execute("""SELECT type,name,sql FROM sqlite_master
        WHERE tbl_name IN ('project_execution_acceptances','project_execution_revision_links')
           OR name LIKE 'idx_project_execution_%' OR name LIKE 'project_execution_%'""")
    ).fetchall()
    expected = {
        ("table", "project_execution_acceptances"): _sql(PROJECT_EXECUTION_STATEMENTS[0]),
        ("index", "idx_project_execution_acceptance_project"): _sql(
            PROJECT_EXECUTION_STATEMENTS[1]
        ),
        ("table", "project_execution_revision_links"): _sql(PROJECT_EXECUTION_STATEMENTS[2]),
        ("index", "idx_project_execution_revision_project"): _sql(PROJECT_EXECUTION_STATEMENTS[3]),
        **{
            ("index", f"sqlite_autoindex_project_execution_acceptances_{n}"): None
            for n in (1, 2, 3)
        },
        **{
            ("index", f"sqlite_autoindex_project_execution_revision_links_{n}"): None
            for n in (1, 2)
        },
    }
    if {(r[0], r[1]): _sql(r[2]) for r in rows} != expected:
        raise RuntimeError("project execution schema differs from schema 33")


async def migrate_project_execution_receipts(db: aiosqlite.Connection) -> None:
    if db.in_transaction:
        raise RuntimeError("project execution migration requires its own transaction")
    await db.execute("BEGIN IMMEDIATE")
    try:
        row = await (await db.execute("PRAGMA user_version")).fetchone()
        version = row[0] if row is not None else None
        await _validate_schema(db)
        await _validate_local_schema(db)
        if version == 32:
            existing = await (
                await db.execute("""SELECT name FROM sqlite_master
                WHERE name LIKE 'project_execution_%' OR name LIKE 'idx_project_execution_%'""")
            ).fetchall()
            if existing:
                raise RuntimeError("schema 32 already contains project execution objects")
            for statement in PROJECT_EXECUTION_STATEMENTS:
                await db.execute(statement)
            await validate_project_execution_schema(db)
            await db.execute("PRAGMA user_version=33")
        elif version == 33:
            await validate_project_execution_schema(db)
        else:
            raise RuntimeError("project execution migration requires schema 32")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def validate_project_execution_prefix(db: aiosqlite.Connection, version: int) -> None:
    """Fail before ordinary startup recovery mutates a known 32/33 database."""
    if version == 33:
        await validate_project_execution_schema(db)
        return
    if version != 32:
        raise RuntimeError("project execution prefix requires schema 32 or 33")
    await _validate_schema(db)
    await _validate_local_schema(db)
    existing = await (
        await db.execute("""SELECT name FROM sqlite_master
        WHERE name LIKE 'project_execution_%' OR name LIKE 'idx_project_execution_%'""")
    ).fetchall()
    if existing:
        raise RuntimeError("schema 32 already contains project execution objects")
