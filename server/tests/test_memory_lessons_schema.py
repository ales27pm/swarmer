"""Schema34 migration preserves full schema33 data and fails before recovery DML."""

import aiosqlite
import pytest

from app.services import memory_lessons_schema as schema
from app.services.project_execution_schema import migrate_project_execution_receipts
from app.services.state_service import StateService
from tests.test_memory_lessons import assess_fixture
from tests.test_memory_symbolic_integration import database_snapshot
from tests.test_project_execution_schema import prefix32


async def prefix33(tmp_path):
    path = await prefix32(tmp_path)
    async with aiosqlite.connect(path) as db:
        await migrate_project_execution_receipts(db)
    return path


@pytest.mark.asyncio
async def test_migration_adds_empty_tables_and_preserves_all_rows_sequences_and_restart(tmp_path):
    path = await prefix33(tmp_path)
    before = await database_snapshot(path)
    async with aiosqlite.connect(path) as db:
        await schema.migrate_memory_lessons(db)
    after = await database_snapshot(path)
    assert after["version"] == (34,)
    assert {k: after["tables"][k] for k in before["tables"]} == before["tables"]
    assert set(after["tables"]) - set(before["tables"]) == set(schema.TABLES)
    assert all(after["tables"][name]["rows"] == [] for name in schema.TABLES)
    async with aiosqlite.connect(path) as db:
        await schema.migrate_memory_lessons(db)
    assert await database_snapshot(path) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", range(6))
async def test_every_schema34_commit_boundary_is_atomic_and_retryable(
    tmp_path, monkeypatch, boundary
):
    path = await prefix33(tmp_path)
    before = await database_snapshot(path)
    statements = (*schema.LESSON_STATEMENTS, "PRAGMA user_version=34")

    class Interrupted(BaseException):
        pass

    async with aiosqlite.connect(path) as db:
        original = db.execute

        async def stop(sql, *args, **kwargs):
            value = await original(sql, *args, **kwargs)
            if sql == statements[boundary]:
                raise Interrupted()
            return value

        with monkeypatch.context() as patch:
            patch.setattr(db, "execute", stop)
            with pytest.raises(Interrupted):
                await schema.migrate_memory_lessons(db)
        assert not db.in_transaction
    assert await database_snapshot(path) == before
    async with aiosqlite.connect(path) as db:
        await schema.migrate_memory_lessons(db)
    assert (await database_snapshot(path))["version"] == (34,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["contaminated33", "missing_index34", "foreign_trigger34", "future35"]
)
async def test_startup_refuses_bad_prefix_before_recovery_writes(tmp_path, monkeypatch, mode):
    path = await prefix33(tmp_path)
    async with aiosqlite.connect(path) as db:
        if mode == "contaminated33":
            await db.execute(schema.LESSON_STATEMENTS[0])
        elif mode == "future35":
            await db.execute("PRAGMA user_version=35")
        else:
            await schema.migrate_memory_lessons(db)
            if mode == "missing_index34":
                await db.execute("DROP INDEX idx_memory_procedure_project")
            else:
                await db.execute(
                    "CREATE TRIGGER wrong AFTER INSERT ON memory_procedure_evidence BEGIN SELECT 1; END"
                )
        await db.commit()
    before = await database_snapshot(path)

    async def forbidden(*args):
        raise AssertionError("startup recovery cannot run")

    monkeypatch.setattr(StateService, "_migrate_legacy_schema", forbidden)
    with pytest.raises(RuntimeError):
        await StateService(path).initialize()
    assert await database_snapshot(path) == before


@pytest.mark.asyncio
async def test_populated_receipt_and_lesson_restart_preserves_full_database(tmp_path):
    (service, manager, project, _, _, _), assessed = await assess_fixture(tmp_path)
    before = await database_snapshot(manager.db_path)
    await StateService(manager.db_path).initialize()
    assert await database_snapshot(manager.db_path) == before
    assert await service.get(project, assessed.lesson_id) == assessed


@pytest.mark.asyncio
async def test_real_populated33_upgrade_keeps_receipts_without_backfill(tmp_path):
    (_service, manager, _, _, _, _), _ = await assess_fixture(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        for name in (
            "memory_procedure_evidence",
            "memory_procedure_lessons",
            "memory_execution_profiles",
            "memory_lesson_requests",
        ):
            await db.execute(f"DROP TABLE {name}")
        await db.execute("PRAGMA user_version=33")
        await db.commit()
    before = await database_snapshot(manager.db_path)
    await StateService(manager.db_path).initialize()
    after = await database_snapshot(manager.db_path)
    assert {k: after["tables"][k] for k in before["tables"]} == before["tables"]
    assert before["tables"]["project_execution_acceptances"]["rows"]
    assert all(after["tables"][name]["rows"] == [] for name in schema.TABLES)
    assert after["foreign_keys"] == [] and after["integrity"] == [("ok",)]
