"""Schema 27: append-only requirement/evidence mapping versions, migrated atomically."""

import aiosqlite


async def migrate_project_evidence(db: aiosqlite.Connection) -> None:
    # Called inside StateService's writer transaction, never executescript (implicit commit).
    await db.execute("""CREATE TABLE IF NOT EXISTS project_requirement_evidence (
        id TEXT PRIMARY KEY,
        goal_run_id TEXT NOT NULL REFERENCES goal_runs(id),
        project_id TEXT NOT NULL REFERENCES coding_projects(id),
        revision_id TEXT NOT NULL REFERENCES project_revisions(id),
        criterion_index INTEGER NOT NULL CHECK(criterion_index BETWEEN 0 AND 19),
        version INTEGER NOT NULL CHECK(version>=1),
        criterion_text TEXT NOT NULL,
        mapping_json TEXT NOT NULL CHECK(json_valid(mapping_json)),
        goal_sha256 TEXT NOT NULL CHECK(length(goal_sha256)=64),
        evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
        request_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
        actor_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(goal_run_id,criterion_index,version),
        UNIQUE(goal_run_id,request_id)
    )""")
    await db.execute("""CREATE INDEX IF NOT EXISTS idx_project_requirement_evidence_current
        ON project_requirement_evidence(goal_run_id,criterion_index,version DESC)""")
    await db.execute("""CREATE TRIGGER IF NOT EXISTS project_requirement_evidence_no_update
        BEFORE UPDATE ON project_requirement_evidence BEGIN
        SELECT RAISE(ABORT,'requirement evidence versions are immutable'); END""")
    await db.execute("""CREATE TRIGGER IF NOT EXISTS project_requirement_evidence_no_delete
        BEFORE DELETE ON project_requirement_evidence BEGIN
        SELECT RAISE(ABORT,'requirement evidence versions are immutable'); END""")
