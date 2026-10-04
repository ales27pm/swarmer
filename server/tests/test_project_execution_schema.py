"""Real 32→33 prefixes, complete durable snapshots, and fail-closed startup."""

import aiosqlite
import pytest

from app.services import project_execution_schema as schema
from app.services.memory_local_context_schema import migrate_local_context_receipts
from app.services.state_service import StateService
from tests.test_memory_local_context_schema import prefix31
from tests.test_memory_symbolic_integration import database_snapshot


async def prefix32(tmp_path):
    path = await prefix31(tmp_path)
    async with aiosqlite.connect(path) as db:
        await migrate_local_context_receipts(db)
        await db.execute(
            """INSERT INTO memory_local_context_receipts(id,device_id,purpose,binding_json,
            catalogs_json,context_json,context_sha256,max_context_bytes,created_at,expires_at)
            VALUES('existing','device','tool_proposal','{}','[]','{}','digest',0,'before','later')"""
        )
        await db.commit()
    return path


@pytest.mark.asyncio
async def test_schema33_empty_additive_all_rows_sequences_and_idempotent_restart(tmp_path):
    path = await prefix32(tmp_path)
    before = await database_snapshot(path)
    async with aiosqlite.connect(path) as db:
        await schema.migrate_project_execution_receipts(db)
    after = await database_snapshot(path)
    assert after["version"] == (33,)
    assert {key: after["tables"][key] for key in before["tables"]} == before["tables"]
    additions = set(after["tables"]) - set(before["tables"])
    assert additions == {"project_execution_acceptances", "project_execution_revision_links"}
    assert all(after["tables"][name]["rows"] == [] for name in additions)
    assert after["foreign_keys"] == [] and after["integrity"] == [("ok",)]
    async with aiosqlite.connect(path) as db:
        await schema.migrate_project_execution_receipts(db)
    assert await database_snapshot(path) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", range(5))
async def test_schema33_each_ddl_and_version_boundary_rolls_back_and_retries(
    tmp_path, monkeypatch, boundary
):
    path = await prefix32(tmp_path)
    before = await database_snapshot(path)
    statements = (*schema.PROJECT_EXECUTION_STATEMENTS, "PRAGMA user_version=33")

    class Crash(BaseException):
        pass

    async with aiosqlite.connect(path) as db:
        original = db.execute

        async def interrupted(sql, *args, **kwargs):
            result = await original(sql, *args, **kwargs)
            if sql == statements[boundary]:
                raise Crash()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(db, "execute", interrupted)
            with pytest.raises(Crash):
                await schema.migrate_project_execution_receipts(db)
        assert not db.in_transaction
    assert await database_snapshot(path) == before
    async with aiosqlite.connect(path) as db:
        await schema.migrate_project_execution_receipts(db)
    assert (await database_snapshot(path))["version"] == (33,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["partial32", "missing_index33", "extra_trigger33", "bad_memory32", "future34"]
)
async def test_invalid_prefix_refused_before_any_startup_recovery_dml(
    tmp_path, monkeypatch, change
):
    path = await prefix32(tmp_path)
    async with aiosqlite.connect(path) as db:
        if change == "partial32":
            await db.execute(schema.PROJECT_EXECUTION_STATEMENTS[0])
        elif change == "bad_memory32":
            await db.execute("DROP INDEX idx_memory_local_context_source")
        else:
            await schema.migrate_project_execution_receipts(db)
            if change == "missing_index33":
                await db.execute("DROP INDEX idx_project_execution_acceptance_project")
            elif change == "extra_trigger33":
                await db.execute(
                    "CREATE TRIGGER foreign_receipt AFTER INSERT ON project_execution_acceptances BEGIN SELECT 1; END"
                )
            else:
                await db.execute("PRAGMA user_version=34")
        await db.commit()
    before = await database_snapshot(path)

    async def forbidden(*args):
        raise AssertionError("ordinary startup recovery must not run")

    monkeypatch.setattr(StateService, "_migrate_legacy_schema", forbidden)
    with pytest.raises(RuntimeError):
        await StateService(path).initialize()
    assert await database_snapshot(path) == before


@pytest.mark.asyncio
async def test_new_database_initializes_and_restarts_at_current_schema(tmp_path):
    path = tmp_path / "new.db"
    state = StateService(path)
    await state.initialize()
    before = await database_snapshot(path)
    await state.initialize()
    assert await database_snapshot(path) == before
    assert before["version"] == (34,)


@pytest.mark.asyncio
async def test_migration_requires_own_transaction(tmp_path):
    path = await prefix32(tmp_path)
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN")
        with pytest.raises(RuntimeError, match="own transaction"):
            await schema.migrate_project_execution_receipts(db)
        assert db.in_transaction
