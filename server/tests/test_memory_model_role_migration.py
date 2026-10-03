"""Schema 27→28 keeps historical receipts and their incoming references intact."""

import re
from pathlib import Path

import aiosqlite
import pytest

from app.services.model_request_execution import MEMORY_MODEL_ROLES
from app.services.state_service import SCHEMA, SCHEMA_VERSION, StateService

LEGACY_CHECK = "CHECK(role IN ('planner','evaluator','summarizer','synthesizer'))"


async def _legacy(path: Path):
    # Freeze the historical four-role constraint independently of the new schema.
    schema = re.sub(
        r"(CREATE TABLE IF NOT EXISTS goal_model_calls \([\s\S]*?role TEXT NOT NULL )CHECK\(role IN \([^)]*\)\)",
        lambda match: match[1] + LEGACY_CHECK,
        SCHEMA,
        count=1,
    )
    async with aiosqlite.connect(path) as db:
        await db.executescript(schema)
        await db.execute("PRAGMA user_version=27")
        await db.execute("""INSERT INTO tasks(id,title,input,mode,source,status,created_at,updated_at)
            VALUES('task','Task','Task','normal','test','completed','2026-10-01','2026-10-01')""")
        await db.execute("""INSERT INTO goal_runs(id,root_task_id,objective,status,autonomy_profile,
            planner_source,max_steps,max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
            created_at,updated_at) VALUES('goal','task','Task','completed','assisted','ubuntu_local',
            8,1,1,600,100,'2026-10-01','2026-10-01')""")
        await db.execute("INSERT INTO coding_projects VALUES('project','2026-10-01','2026-10-01')")
        await db.execute("""INSERT INTO goal_contexts(id,goal_run_id,root_task_id,purpose,context_json,
            provenance_json,approx_token_count,created_at)
            VALUES('context','goal','task','planner','{}','[]',0,'2026-10-01')""")
        for rowid, call_id, role, status in (
            (91, "old_completed", "planner", "completed"),
            (203, "old_started", "evaluator", "started"),
        ):
            await db.execute(
                """INSERT INTO goal_model_calls(rowid,id,goal_run_id,role,
                provider_source,model_id,context_id,input_digest,output_digest,status,created_at,
                owner_instance_id,lease_expires_at,lease_generation,conversation_revision)
                VALUES(?,?,'goal',?,'ubuntu_local','model','context','input','output',?,
                '2026-10-01','owner','2099-01-01',3,2)""",
                (rowid, call_id, role, status),
            )
        await db.execute("""INSERT INTO project_context_compactions(cache_key,project_id,goal_run_id,
            fingerprint,version,provider_identity,model_call_id,status,summary_json,created_at)
            VALUES('cache','project','goal','fingerprint',2,'provider','old_completed',
            'completed','{"notes":[]}','2026-10-01')""")
        await db.execute("CREATE INDEX custom_call_role ON goal_model_calls(role)")
        await db.execute("CREATE TABLE migration_copy_audit(n INTEGER NOT NULL)")
        await db.execute("INSERT INTO migration_copy_audit VALUES(0)")
        await db.execute("""CREATE TRIGGER custom_call_insert AFTER INSERT ON goal_model_calls
            BEGIN UPDATE migration_copy_audit SET n=n+1; END""")
        await db.commit()


async def _snapshot(path):
    async with aiosqlite.connect(path) as db:
        return {
            table: await (
                await db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")
            ).fetchall()
            for table in ("goal_model_calls", "project_context_compactions", "migration_copy_audit")
        }


async def _insert_role(db, role, call_id=None):
    return await db.execute(
        """INSERT INTO goal_model_calls(id,goal_run_id,role,provider_source,
        input_digest,status,created_at) VALUES(?,'goal',?,'ubuntu_local','input','completed',
        '2026-10-03')""",
        (call_id or role, role),
    )


@pytest.mark.asyncio
async def test_27_to_28_preserves_rows_rowids_indexes_triggers_and_compaction_links(tmp_path):
    path = tmp_path / "old.db"
    await _legacy(path)
    before = await _snapshot(path)
    async with aiosqlite.connect(path) as db:
        objects = await (
            await db.execute("""SELECT type,name,sql FROM sqlite_master
            WHERE tbl_name='goal_model_calls' AND type IN ('index','trigger') ORDER BY name""")
        ).fetchall()
        foreign_keys = await (
            await db.execute("PRAGMA foreign_key_list(goal_model_calls)")
        ).fetchall()
    state = StateService(path)
    await state.initialize()
    await state.initialize()
    assert await _snapshot(path) == before
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute("PRAGMA user_version")).fetchone() == (SCHEMA_VERSION,)
        assert await (await db.execute("PRAGMA foreign_key_check")).fetchall() == []
        assert (
            await (await db.execute("PRAGMA foreign_key_list(goal_model_calls)")).fetchall()
            == foreign_keys
        )
        assert (
            await (
                await db.execute("""SELECT type,name,sql FROM sqlite_master
            WHERE tbl_name='goal_model_calls' AND type IN ('index','trigger') ORDER BY name""")
            ).fetchall()
            == objects
        )
        for role in sorted(MEMORY_MODEL_ROLES):
            await _insert_role(db, role)
        assert await (await db.execute("SELECT n FROM migration_copy_audit")).fetchone() == (5,)
        with pytest.raises(aiosqlite.IntegrityError):
            await _insert_role(db, "unapproved_model_role")
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute("""INSERT INTO goal_model_calls(id,goal_run_id,role,provider_source,
                input_digest,status,created_at) VALUES('concurrent','goal','planner','ubuntu_local',
                'input','started','2026-10-03')""")


@pytest.mark.asyncio
async def test_fresh_database_has_new_roles_and_foreign_key_enforcement(tmp_path):
    path = tmp_path / "fresh.db"
    state = StateService(path)
    await state.initialize()
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        assert await (await db.execute("PRAGMA user_version")).fetchone() == (SCHEMA_VERSION,)
        for role in sorted(MEMORY_MODEL_ROLES):
            with pytest.raises(aiosqlite.IntegrityError, match="FOREIGN KEY"):
                await _insert_role(db, role)


@pytest.mark.asyncio
async def test_failed_rebuild_rolls_back_table_and_version_then_retries(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    await _legacy(path)
    before = await _snapshot(path)
    execute = aiosqlite.Connection.execute

    def fail_rename(self, sql, parameters=None):
        if "ALTER TABLE goal_model_calls_v28 RENAME TO goal_model_calls" in sql:
            raise RuntimeError("injected interruption before rename")
        return execute(self, sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "execute", fail_rename)
        with pytest.raises(RuntimeError, match="injected interruption"):
            await StateService(path).initialize()
    assert await _snapshot(path) == before
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute("PRAGMA user_version")).fetchone() == (27,)
        assert await (await db.execute("PRAGMA foreign_key_check")).fetchall() == []
        assert not await (
            await db.execute("SELECT name FROM sqlite_master WHERE name='goal_model_calls_v28'")
        ).fetchall()
    await StateService(path).initialize()
    assert await _snapshot(path) == before
