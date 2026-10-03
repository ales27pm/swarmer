"""Additive 31→32 migration, rollback boundaries, and malformed/future refusal."""

import aiosqlite
import pytest

from app.services import memory_local_context_schema as schema
from app.services.memory_view_vector_schema import migrate_memory_view_vectors
from tests.test_memory_symbolic_integration import database_snapshot
from tests.test_memory_view_vector_schema import schema30_database


async def prefix31(tmp_path):
    path = await schema30_database(tmp_path)
    async with aiosqlite.connect(path) as db:
        await migrate_memory_view_vectors(db)
    return path


@pytest.mark.asyncio
async def test_schema32_is_empty_additive_and_exact_reentry(tmp_path):
    path = await prefix31(tmp_path)
    before = await database_snapshot(path)
    async with aiosqlite.connect(path) as db:
        await schema.migrate_local_context_receipts(db)
    after = await database_snapshot(path)
    assert after["version"] == (32,)
    assert {k: after["tables"][k] for k in before["tables"]} == before["tables"]
    assert set(after["tables"]) - set(before["tables"]) == {
        "memory_local_context_receipts",
        "memory_local_context_sources",
    }
    async with aiosqlite.connect(path) as db:
        await schema.migrate_local_context_receipts(db)
    assert await database_snapshot(path) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", range(5))
async def test_every_schema32_boundary_rolls_back_even_base_exception(
    tmp_path, monkeypatch, boundary
):
    path = await prefix31(tmp_path)
    before = await database_snapshot(path)

    class Crash(BaseException):
        pass

    statements = (*schema.LOCAL_CONTEXT_STATEMENTS, "PRAGMA user_version=32")
    async with aiosqlite.connect(path) as db:
        original = db.execute

        async def fail(sql, *args, **kwargs):
            result = await original(sql, *args, **kwargs)
            if sql == statements[boundary]:
                raise Crash()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(db, "execute", fail)
            with pytest.raises(Crash):
                await schema.migrate_local_context_receipts(db)
        assert not db.in_transaction
    assert await database_snapshot(path) == before
    async with aiosqlite.connect(path) as db:
        await schema.migrate_local_context_receipts(db)
    assert (await database_snapshot(path))["version"] == (32,)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["partial31", "malformed32", "future33"])
async def test_invalid_prefix_refused_without_repair(tmp_path, change):
    path = await prefix31(tmp_path)
    async with aiosqlite.connect(path) as db:
        if change == "partial31":
            await db.execute(schema.LOCAL_CONTEXT_STATEMENTS[0])
        else:
            await schema.migrate_local_context_receipts(db)
            if change == "malformed32":
                await db.execute(
                    "CREATE TRIGGER extra_local_context AFTER INSERT ON memory_local_context_sources BEGIN SELECT 1; END"
                )
            else:
                await db.execute("PRAGMA user_version=33")
        await db.commit()
    before = await database_snapshot(path)
    async with aiosqlite.connect(path) as db:
        with pytest.raises(RuntimeError):
            await schema.migrate_local_context_receipts(db)
    assert await database_snapshot(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_keys", [False, True])
async def test_populated_schema32_reentry_and_erasure_with_either_fk_mode(tmp_path, foreign_keys):
    path = await prefix31(tmp_path)
    async with aiosqlite.connect(path) as db:
        await schema.migrate_local_context_receipts(db)
        await db.execute(
            "INSERT INTO memory_local_context_receipts(id,device_id,purpose,binding_json,catalogs_json,context_json,context_sha256,max_context_bytes,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("r1", "device", "tool_proposal", "{}", "[]", "{}", "digest", 16384, "now", "later"),
        )
        await db.executemany(
            "INSERT INTO memory_local_context_sources VALUES(?,?)", [("r1", "m1"), ("r1", "m2")]
        )
        await db.commit()
    before = await database_snapshot(path)
    async with aiosqlite.connect(path) as db:
        await schema.migrate_local_context_receipts(db)
    assert await database_snapshot(path) == before
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=" + ("ON" if foreign_keys else "OFF"))
        await db.execute("BEGIN IMMEDIATE")
        await schema.forget_local_contexts_locked(db, "m2")
        await db.execute("DELETE FROM memory_items WHERE id='m2'")
        await db.commit()
        assert (
            await (await db.execute("SELECT * FROM memory_local_context_receipts")).fetchall() == []
        )
        assert (
            await (await db.execute("SELECT * FROM memory_local_context_sources")).fetchall() == []
        )
        assert await (await db.execute("SELECT id FROM memory_items WHERE id='m1'")).fetchone() == (
            "m1",
        )
