"""Schema 31 preserves old state and installs empty, independently keyed vectors."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.services import memory_view_vector_schema as schema
from app.services import state_service
from app.services.memory_symbolic_store import initialize_symbolic_schema_locked
from app.services.memory_text_views import initialize_text_view_schema_locked
from app.services.state_service import SCHEMA, StateService
from tests.test_memory_symbolic_integration import NoModelCalls, database_snapshot

NOW = "2026-10-03T10:00:00+00:00"


async def schema30_database(tmp_path: Path) -> Path:
    """Use the real pre-31 DDL; do not downgrade a schema-31 database."""
    path = tmp_path / "schema30.db"
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA)
        await db.execute("BEGIN IMMEDIATE")
        await initialize_text_view_schema_locked(db)
        await initialize_symbolic_schema_locked(db)
        for rowid, memory_id, revision in ((7, "m1", 2), (101, "m2", 3)):
            await db.execute(
                """INSERT INTO memory_items(
                    rowid,id,scope,kind,content,created_at,updated_at)
                    VALUES(?,?,'general','fact','Existing canonical text',?,?)""",
                (rowid, memory_id, NOW, NOW),
            )
            for role in ("original", "canonical"):
                view_id = f"{memory_id}-{role}"
                await db.execute(
                    """INSERT INTO memory_text_views(
                        id,memory_id,revision,role,language,pipeline_signature,content,
                        text_sha256,source_sha256,scope,kind,sensitivity,created_at)
                        VALUES(?,?,?,?,?,'pipeline-v1',?,'view-digest','source-digest',
                            'general','fact','normal',?)""",
                    (
                        view_id,
                        memory_id,
                        revision,
                        role,
                        "fr" if role == "original" else "en",
                        "Texte source" if role == "original" else "Existing canonical text",
                        NOW,
                    ),
                )
            await db.execute(
                """INSERT INTO memory_text_heads(
                    memory_id,revision,item_revision,source_sha256,view_set_sha256,
                    index_view_id,deleted,updated_at) VALUES(?,?,?,'source-digest',
                    'set-digest',?,0,?)""",
                (memory_id, revision, NOW, f"{memory_id}-canonical", NOW),
            )
        await db.execute(
            """INSERT INTO memory_embeddings(rowid,memory_id,provider,dimensions,
                vector_json,updated_at) VALUES(8,'m1','embed-v1',2,'[1.0,0.0]',?)""",
            (NOW,),
        )
        for values in (
            (3, "m1", 2, "m1-canonical", "embed-v1", "pending", None, 0, None, "not_sent"),
            (
                11,
                "m2",
                3,
                "m2-canonical",
                "embed-v1",
                "claimed",
                "owner-live",
                4,
                "2099-01-01T00:00:00+00:00",
                "in_flight",
            ),
            (
                41,
                "retired",
                1,
                "removed-view",
                "embed-old",
                "obsolete",
                "owner-unknown",
                7,
                "2000-01-01T00:00:00+00:00",
                "in_flight",
            ),
            (62, "m1", 1, "old-view", None, "completed", None, 2, None, "known"),
        ):
            await db.execute(
                """INSERT INTO memory_index_outbox(
                    id,memory_id,revision,view_id,provider,status,owner,generation,
                    lease_expires_at,request_state,operation,dispatched_at,response_received_at,
                    claim_count,available_at,error_category,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'upsert',?,?,9,?,'provider_unavailable',?,?)""",
                (*values, NOW, NOW if values[-1] == "known" else None, NOW, NOW, NOW),
            )
        await db.execute(
            """INSERT INTO memory_index_outbox(id,memory_id,revision,operation,status,
                available_at,created_at,updated_at)
                VALUES(500,'discarded',1,'delete','pending',?,?,?)""",
            (NOW, NOW, NOW),
        )
        await db.execute("DELETE FROM memory_index_outbox WHERE id=500")
        await db.execute(
            """INSERT INTO memory_symbolic_concepts VALUES(
                'concept-existing','general','test','v1','proposed',0,?)""",
            (NOW,),
        )
        await db.execute(
            """INSERT INTO memory_symbolic_labels VALUES(
                'concept-existing',0,'Existing concept','en','declared','pref')"""
        )
        await db.execute("PRAGMA user_version=30")
        await db.commit()
    return path


async def migrate(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await schema.migrate_memory_view_vectors(db)
        assert not db.in_transaction


async def insert_vector(db, view_id="m1-original", provider="embed-v1"):
    await db.execute(
        """INSERT INTO memory_view_embeddings(
            memory_id,revision,view_id,provider,source_id,source_sha256,view_sha256,
            pipeline_signature,item_revision,dimensions,vector_json,updated_at)
            VALUES('m1',2,?,?,NULL,'source-digest','view-digest','pipeline-v1',?,2,'[0.0,1.0]',?)""",
        (view_id, provider, NOW, NOW),
    )


async def test_migration_preserves_every_old_row_rowid_object_and_autoincrement(tmp_path):
    path = await schema30_database(tmp_path)
    # A table rebuild or lease reset would execute one of these triggers.
    async with aiosqlite.connect(path) as db:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            await db.execute(
                f"""CREATE TRIGGER forbid_outbox_{operation.lower()}
                BEFORE {operation} ON memory_index_outbox BEGIN
                  SELECT RAISE(ABORT,'migration touched outbox data'); END"""
            )
        await db.commit()
    before = await database_snapshot(path)
    await migrate(path)
    after = await database_snapshot(path)
    assert before["version"] == (30,) and after["version"] == (31,)
    assert set(after["tables"]) - set(before["tables"]) == {"memory_view_embeddings"}
    assert after["tables"]["memory_view_embeddings"]["rows"] == []
    assert {name: after["tables"][name] for name in before["tables"]} == before["tables"]
    objects = {obj[2]: obj for obj in after["objects"]}
    assert all(
        objects[obj[2]] == obj for obj in before["objects"] if obj[2] != "idx_memory_index_dedupe"
    )
    assert set(objects) - {obj[2] for obj in before["objects"]} == {
        "memory_view_embeddings",
        "sqlite_autoindex_memory_view_embeddings_1",
        "idx_memory_view_embeddings_provider",
    }
    assert after["integrity"] == [("ok",)] and after["foreign_keys"] == []
    assert (
        next(
            row
            for row in after["tables"]["sqlite_sequence"]["rows"]
            if row[1] == "memory_index_outbox"
        )[2]
        == 500
    )
    await migrate(path)
    assert await database_snapshot(path) == after


async def test_actual_startup_from_committed_schema30_is_model_free_and_preserves_old_state(
    tmp_path, monkeypatch
):
    forbidden = NoModelCalls()
    state = StateService(tmp_path / "actual-prefix.db", forbidden)
    state.memory_normalizer = forbidden
    state.memory_presenter = forbidden

    async def stop_at_committed_schema30(db):
        assert not db.in_transaction
        row = await (await db.execute("PRAGMA user_version")).fetchone()
        assert row is not None and row[0] == 30

    with monkeypatch.context() as patch:
        patch.setattr(state_service, "migrate_memory_view_vectors", stop_at_committed_schema30)
        await state.initialize()
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            """INSERT INTO memory_items(rowid,id,scope,kind,content,created_at,updated_at)
                VALUES(71,'legacy-unheaded','general','fact','Original untouched',?,?)""",
            (NOW, NOW),
        )
        await db.execute(
            """INSERT INTO memory_embeddings(rowid,memory_id,provider,dimensions,
                vector_json,updated_at) VALUES(81,'legacy-unheaded','old-provider',1,'[1.0]',?)""",
            (NOW,),
        )
        await db.execute(
            """INSERT INTO memory_index_outbox(id,memory_id,revision,view_id,provider,
                operation,status,owner,generation,lease_expires_at,request_state,
                available_at,created_at,updated_at)
                VALUES(201,'retired',3,'old-view','old-provider','upsert','obsolete',
                    'unfinished-owner',7,?,'in_flight',?,?,?)""",
            (NOW, NOW, NOW, NOW),
        )
        await db.commit()
    before = await database_snapshot(state.db_path)
    await state.initialize()
    after = await database_snapshot(state.db_path)
    assert after["version"] == (31,)
    assert {name: after["tables"][name] for name in before["tables"]} == before["tables"]
    assert after["tables"]["memory_view_embeddings"]["rows"] == []
    assert set(after["tables"]) - set(before["tables"]) == {"memory_view_embeddings"}
    assert forbidden.calls == 0
    await state.initialize()
    assert await database_snapshot(state.db_path) == after
    assert forbidden.calls == 0


async def test_dual_view_intents_coexist_but_each_identity_and_delete_still_deduplicate(tmp_path):
    path = await schema30_database(tmp_path)
    await migrate(path)
    async with aiosqlite.connect(path) as db:
        args = ("m1", 2, "m1-original", "embed-v1", "upsert", NOW, NOW, NOW)
        sql = """INSERT INTO memory_index_outbox(memory_id,revision,view_id,provider,
            operation,status,available_at,created_at,updated_at)
            VALUES(?,?,?,?,?,'pending',?,?,?)"""
        cursor = await db.execute(sql, args)
        assert cursor.lastrowid == 501
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(sql, args)
        # Different providers remain independently projectable.
        await db.execute(sql, (*args[:3], "embed-v2", *args[4:]))
        deletion = ("m1", 4, None, None, "delete", NOW, NOW, NOW)
        await db.execute(sql, deletion)
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(sql, deletion)
        assert await (
            await db.execute(
                "SELECT COUNT(*) FROM memory_index_outbox WHERE memory_id='m1' AND revision=2"
            )
        ).fetchone() == (3,)


async def test_authoritative_vectors_are_per_view_and_provider_and_do_not_replace_old_cache(
    tmp_path,
):
    path = await schema30_database(tmp_path)
    await migrate(path)
    legacy = (await database_snapshot(path))["tables"]["memory_embeddings"]
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await insert_vector(db)
        await insert_vector(db, "m1-canonical")
        await insert_vector(db, provider="embed-v2")
        with pytest.raises(aiosqlite.IntegrityError):
            await insert_vector(db)
        await db.commit()
    populated = await database_snapshot(path)
    assert len(populated["tables"]["memory_view_embeddings"]["rows"]) == 3
    assert populated["tables"]["memory_embeddings"] == legacy
    await migrate(path)
    assert await database_snapshot(path) == populated
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("DELETE FROM memory_text_views WHERE id='m1-original'")
        assert await (
            await db.execute("SELECT view_id FROM memory_view_embeddings")
        ).fetchall() == [("m1-canonical",)]
        await db.execute("DELETE FROM memory_items WHERE id='m1'")
        assert await (await db.execute("SELECT * FROM memory_view_embeddings")).fetchall() == []


class InjectedFailure(BaseException):
    pass


@pytest.mark.parametrize("boundary", ["table", "provider_index", "drop_index", "dedupe", "version"])
async def test_every_ddl_failure_rolls_back_all_rows_schema_sequence_and_version(
    tmp_path, monkeypatch, boundary
):
    path = await schema30_database(tmp_path)
    before = await database_snapshot(path)
    statements = {
        "table": schema.MEMORY_VIEW_VECTOR_STATEMENTS[0],
        "provider_index": schema.MEMORY_VIEW_VECTOR_STATEMENTS[1],
        "drop_index": "DROP INDEX idx_memory_index_dedupe",
        "dedupe": schema.MEMORY_INDEX_DEDUPE_SQL,
        "version": "PRAGMA user_version=31",
    }
    async with aiosqlite.connect(path) as db:
        execute = db.execute

        async def fail_after_statement(sql, *args, **kwargs):
            result = await execute(sql, *args, **kwargs)
            if sql == statements[boundary]:
                raise InjectedFailure(boundary)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(db, "execute", fail_after_statement)
            with pytest.raises(InjectedFailure):
                await schema.migrate_memory_view_vectors(db)
            assert not db.in_transaction
    assert await database_snapshot(path) == before
    await migrate(path)
    assert (await database_snapshot(path))["version"] == (31,)


@pytest.mark.parametrize("version", [0, 27, 28, 29, 32])
async def test_wrong_version_is_rejected_without_schema_or_data_changes(tmp_path, version):
    path = await schema30_database(tmp_path)
    async with aiosqlite.connect(path) as db:
        await db.execute(f"PRAGMA user_version={version}")
    before = await database_snapshot(path)
    with pytest.raises(RuntimeError, match="requires schema 30"):
        await migrate(path)
    assert await database_snapshot(path) == before


async def test_migration_does_not_take_over_callers_transaction(tmp_path):
    path = await schema30_database(tmp_path)
    async with aiosqlite.connect(path) as db:
        with pytest.raises(RuntimeError, match="caller's transaction"):
            await schema.initialize_view_vector_schema_locked(db)
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE memory_items SET content='Uncommitted' WHERE id='m1'")
        with pytest.raises(RuntimeError, match="own transaction"):
            await schema.migrate_memory_view_vectors(db)
        assert db.in_transaction
        assert await (
            await db.execute("SELECT content FROM memory_items WHERE id='m1'")
        ).fetchone() == ("Uncommitted",)
        await db.rollback()


@pytest.mark.parametrize(
    "mutation",
    [
        "ALTER TABLE memory_view_embeddings ADD COLUMN unexpected TEXT",
        "DROP INDEX idx_memory_view_embeddings_provider",
        "CREATE INDEX unexpected_vector_index ON memory_view_embeddings(dimensions)",
        "CREATE TRIGGER unexpected_vector_trigger AFTER INSERT ON memory_view_embeddings BEGIN SELECT 1; END",
        "DROP INDEX idx_memory_index_dedupe",
    ],
)
async def test_schema31_reentry_rejects_changed_or_missing_schema_without_repair(
    tmp_path, mutation
):
    path = await schema30_database(tmp_path)
    await migrate(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(mutation)
        await db.commit()
    before = await database_snapshot(path)
    with pytest.raises(RuntimeError, match="differs from schema 31"):
        await migrate(path)
    assert await database_snapshot(path) == before


async def test_unknown_legacy_deduplication_is_rejected_without_changes(tmp_path):
    path = await schema30_database(tmp_path)
    async with aiosqlite.connect(path) as db:
        await db.execute("DROP INDEX idx_memory_index_dedupe")
        await db.execute(
            "CREATE UNIQUE INDEX idx_memory_index_dedupe ON memory_index_outbox(memory_id,revision)"
        )
        await db.commit()
    before = await database_snapshot(path)
    with pytest.raises(RuntimeError, match="unrecognized memory index"):
        await migrate(path)
    assert await database_snapshot(path) == before


async def test_schema31_validation_preserves_significant_sql_literal_whitespace(tmp_path):
    path = await schema30_database(tmp_path)
    await migrate(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("DROP INDEX idx_memory_index_dedupe")
        await db.execute("""CREATE UNIQUE INDEX idx_memory_index_dedupe
            ON memory_index_outbox(memory_id,revision,operation,
                COALESCE(view_id,' '),COALESCE(provider,''))""")
        await db.commit()
    before = await database_snapshot(path)
    with pytest.raises(RuntimeError, match="differs from schema 31"):
        await migrate(path)
    assert await database_snapshot(path) == before


async def test_schema30_cannot_adopt_a_preexisting_vector_table(tmp_path):
    path = await schema30_database(tmp_path)
    async with aiosqlite.connect(path) as db:
        await db.execute(schema.MEMORY_VIEW_VECTOR_STATEMENTS[0])
        await insert_vector(db)
        await db.commit()
    before = await database_snapshot(path)
    with pytest.raises(RuntimeError, match="already contains memory view vector"):
        await migrate(path)
    assert await database_snapshot(path) == before
