"""Symbolic proposals share real memory lifecycle without inventing knowledge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import MemoryCreate
from app.services import state_service
from app.services.memory_concepts import (
    ConceptLabel,
    IdentityTerm,
    LanguageAnnotation,
    SymbolicClaim,
)
from app.services.memory_symbolic_contracts import (
    SymbolicConceptCreate,
    SymbolicProposalCreate,
    SymbolicRelationCreate,
)
from app.services.state_service import StateService
from tests.test_memory_canonical_store import ReviewedNormalizer

SYMBOLIC_TABLES = (
    "memory_symbolic_concepts",
    "memory_symbolic_labels",
    "memory_symbolic_proposals",
    "memory_symbolic_sources",
    "memory_symbolic_relations",
    "memory_symbolic_links",
)


class InjectedFault(RuntimeError):
    pass


class NoModelCalls:
    provider_name = "forbidden-fixture"
    normalization_signature = "fixture"
    presentation_signature = "fixture"
    calls = 0

    async def embed(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("migration must not embed")

    async def normalize(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("migration must not normalize")

    async def present(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("migration must not present")


async def database_snapshot(path: Path) -> dict[str, Any]:
    async with aiosqlite.connect(path) as db:
        objects = await (
            await db.execute("""SELECT rowid,type,name,tbl_name,sql
            FROM sqlite_master ORDER BY rowid""")
        ).fetchall()
        tables = {}
        for _rowid, kind, name, _table, sql in objects:
            if kind != "table":
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            columns = await (await db.execute(f"PRAGMA table_xinfo({quoted})")).fetchall()
            without_rowid = "WITHOUT ROWID" in (sql or "").upper()
            selector = "*" if without_rowid else "rowid,*"
            ordering = "1" if without_rowid else "rowid"
            rows = await (
                await db.execute(f"SELECT {selector} FROM {quoted} ORDER BY {ordering}")
            ).fetchall()
            tables[name] = {"columns": columns, "rows": rows}
        return {
            "objects": objects,
            "tables": tables,
            "version": await (await db.execute("PRAGMA user_version")).fetchone(),
            "foreign_keys": await (await db.execute("PRAGMA foreign_key_check")).fetchall(),
            "integrity": await (await db.execute("PRAGMA integrity_check")).fetchall(),
        }


async def schema29_fixture(tmp_path: Path, monkeypatch) -> StateService:
    state = StateService(tmp_path / "state.db")

    async def leave_schema29(db):
        assert not db.in_transaction
        version = await (await db.execute("PRAGMA user_version")).fetchone()
        assert version is not None and version[0] == 29

    with monkeypatch.context() as patch:
        patch.setattr(StateService, "_migrate_symbolic_memory", staticmethod(leave_schema29))
        await state.initialize()
    await state.create_memory(
        MemoryCreate(content="Keep the existing source.", scope="project:p"), "phone"
    )
    state.embedding_service = NoModelCalls()
    state.memory_normalizer = NoModelCalls()
    state.memory_presenter = NoModelCalls()
    return state


@pytest.mark.asyncio
async def test_schema29_to30_preserves_all_existing_rows_columns_sql_and_sequences(
    tmp_path, monkeypatch
):
    state = await schema29_fixture(tmp_path, monkeypatch)
    before = await database_snapshot(state.db_path)
    assert before["version"] == (29,) and len(before["tables"]) == 66
    assert not set(SYMBOLIC_TABLES) & set(before["tables"])
    await state.initialize()
    after = await database_snapshot(state.db_path)
    assert after["version"] == (30,)
    assert set(after["tables"]) - set(before["tables"]) == set(SYMBOLIC_TABLES)
    assert {name: after["tables"][name] for name in before["tables"]} == before["tables"]
    current_objects = {obj[2]: obj for obj in after["objects"]}
    assert all(current_objects[obj[2]] == obj for obj in before["objects"])
    assert all(after["tables"][name]["rows"] == [] for name in SYMBOLIC_TABLES)
    assert after["foreign_keys"] == [] and after["integrity"] == [("ok",)]
    assert (
        state.embedding_service.calls
        == state.memory_normalizer.calls
        == state.memory_presenter.calls
        == 0
    )
    await state.initialize()
    assert await database_snapshot(state.db_path) == after


@pytest.mark.asyncio
async def test_symbolic_migration_failure_rolls_back_new_schema_and_version(tmp_path, monkeypatch):
    state = await schema29_fixture(tmp_path, monkeypatch)
    before = await database_snapshot(state.db_path)
    initialize = state_service.initialize_symbolic_schema_locked

    async def fail_after_ddl(db):
        await initialize(db)
        raise InjectedFault("after symbolic schema creation")

    with monkeypatch.context() as patch:
        patch.setattr(state_service, "initialize_symbolic_schema_locked", fail_after_ddl)
        with pytest.raises(InjectedFault):
            await state.initialize()
    assert await database_snapshot(state.db_path) == before
    await state.initialize()
    assert (await database_snapshot(state.db_path))["version"] == (30,)


@pytest.mark.asyncio
async def test_symbolic_forget_runs_inside_domain_transaction_before_views_are_removed(
    tmp_path, monkeypatch
):
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    memory = await state.create_memory(MemoryCreate(content="A source to remove."), "phone")
    before = await database_snapshot(state.db_path)
    forget = state_service.forget_symbolic_memory_locked
    calls = []

    async def checked_forget(db, memory_id):
        assert db.in_transaction
        assert memory_id == memory["id"]
        assert (
            await (
                await db.execute("SELECT id FROM memory_items WHERE id=?", (memory_id,))
            ).fetchone()
            is None
        )
        assert (
            await (
                await db.execute("SELECT id FROM memory_text_views WHERE memory_id=?", (memory_id,))
            ).fetchone()
            is not None
        )
        calls.append(memory_id)
        await forget(db, memory_id)
        raise InjectedFault("after symbolic purge")

    with monkeypatch.context() as patch:
        patch.setattr(state_service, "forget_symbolic_memory_locked", checked_forget)
        with pytest.raises(InjectedFault):
            await state.delete_memory(memory["id"], "phone")
    assert calls == [memory["id"]]
    assert await database_snapshot(state.db_path) == before
    assert await state.delete_memory(memory["id"], "phone")
    assert await state.get_memory(memory["id"]) is None
    assert not await state.delete_memory(memory["id"], "phone")


async def populated_symbolic_state(tmp_path, canonical):
    state = StateService(
        tmp_path / "state.db",
        canonical_language="en" if canonical else "legacy",
        memory_normalizer=ReviewedNormalizer() if canonical else None,
    )
    await state.initialize()
    removed = await state.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement."), "phone"
    )
    retained = await state.create_memory(MemoryCreate(content="Garder les dates exactes."), "phone")
    concept = await state.symbolic_memory.create_concept(
        SymbolicConceptCreate(
            scope="general",
            namespace="software",
            scheme_id="test",
            labels=[
                ConceptLabel(
                    text="Règle",
                    language=LanguageAnnotation(tag="fr-CA", origin="declared"),
                )
            ],
        ),
        actor_id="phone",
    )
    claim = SymbolicClaim(
        scope="general",
        namespace="software",
        scheme_id="test",
        kind="fact",
        subject=IdentityTerm(namespace="software", identity=concept.concept_id),
        predicate=IdentityTerm(namespace="software", identity="applies_to"),
        object=IdentityTerm(namespace="software", identity="report"),
        polarity="affirmed",
        modality="possible",
    )
    original = await state.symbolic_memory.describe_source(removed["id"], scope="general")
    other = await state.symbolic_memory.describe_source(retained["id"], scope="general")
    removed_proposal = await state.symbolic_memory.create_proposal(
        SymbolicProposalCreate(claim=claim, sources=original.bindings + other.bindings),
        actor_id="phone",
    )
    retained_proposal = await state.symbolic_memory.create_proposal(
        SymbolicProposalCreate(claim=claim, sources=other.bindings), actor_id="phone"
    )
    for source, target in (
        (removed_proposal, retained_proposal),
        (retained_proposal, removed_proposal),
    ):
        await state.symbolic_memory.relate(
            source.proposal_id,
            SymbolicRelationCreate(
                target_proposal_id=target.proposal_id, relationship="related_to"
            ),
            actor_id="phone",
        )
    return state, removed, retained, concept, removed_proposal, retained_proposal


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [False, True])
async def test_initialized_symbolic_store_is_persisted_and_delete_purges_only_linked_claims(
    tmp_path, canonical
):
    (
        state,
        removed,
        retained,
        concept,
        removed_proposal,
        retained_proposal,
    ) = await populated_symbolic_state(tmp_path, canonical)
    restarted = StateService(state.db_path)
    await restarted.initialize()
    page = await restarted.symbolic_memory.list_for_memory(removed["id"], scope="general")
    assert page.proposals == [removed_proposal]
    assert page.grants_authority is False
    assert removed_proposal.validation_status == "unvalidated"
    assert removed_proposal.claim.modality == "possible"
    before = await database_snapshot(state.db_path)

    assert await restarted.delete_memory(removed["id"], "phone")
    after = await database_snapshot(state.db_path)
    assert await restarted.get_memory(removed["id"]) is None
    assert await restarted.get_memory(retained["id"]) == retained
    retained_page = await restarted.symbolic_memory.list_for_memory(retained["id"], scope="general")
    assert retained_page.proposals == [retained_proposal]
    assert not retained_page.relations and not retained_page.unavailable
    for table in ("memory_symbolic_concepts", "memory_symbolic_labels"):
        assert after["tables"][table] == before["tables"][table]
    async with aiosqlite.connect(state.db_path) as db:
        assert await (await db.execute("SELECT id FROM memory_symbolic_proposals")).fetchall() == [
            (retained_proposal.proposal_id,)
        ]
        assert await (
            await db.execute("SELECT proposal_id,memory_id FROM memory_symbolic_sources")
        ).fetchall() == [(retained_proposal.proposal_id, retained["id"])]
        assert await (
            await db.execute("SELECT proposal_id,concept_id FROM memory_symbolic_links")
        ).fetchall() == [(retained_proposal.proposal_id, concept.concept_id)]
        assert await (await db.execute("SELECT * FROM memory_symbolic_relations")).fetchall() == []
        assert (
            await (
                await db.execute(
                    "SELECT 1 FROM memory_text_views WHERE memory_id=?", (removed["id"],)
                )
            ).fetchall()
            == []
        )
    assert after["foreign_keys"] == [] and after["integrity"] == [("ok",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [False, True])
async def test_delete_failure_restores_populated_symbolic_data_and_original_memory(
    tmp_path, monkeypatch, canonical
):
    (
        state,
        removed,
        _retained,
        _concept,
        _removed_proposal,
        _retained_proposal,
    ) = await populated_symbolic_state(tmp_path, canonical)
    before = await database_snapshot(state.db_path)

    async def failed_final_audit(db, *args, **kwargs):
        assert db.in_transaction
        assert (
            await (
                await db.execute(
                    "SELECT 1 FROM memory_symbolic_sources WHERE memory_id=?", (removed["id"],)
                )
            ).fetchall()
            == []
        )
        assert (
            await (
                await db.execute(
                    "SELECT 1 FROM memory_text_views WHERE memory_id=?", (removed["id"],)
                )
            ).fetchall()
            == []
        )
        raise InjectedFault("after populated symbolic and source purge")

    monkeypatch.setattr(state_service, "append_audit_event", failed_final_audit)
    with pytest.raises(InjectedFault):
        await state.delete_memory(removed["id"], "phone")
    assert await database_snapshot(state.db_path) == before
