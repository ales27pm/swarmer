"""Real SQLite provenance, visibility, proposal identity and transactional safety."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import aiosqlite
import pytest
from pydantic import ValidationError

from app.models import MemoryCreate, MemoryUpdate
from app.services import memory_symbolic_store as store_module
from app.services.memory_concepts import (
    ConceptLabel,
    EffectiveCondition,
    IdentityTerm,
    LanguageAnnotation,
    SymbolicClaim,
    TypedLiteral,
)
from app.services.memory_symbolic_contracts import (
    SymbolicConceptCreate,
    SymbolicProposalCreate,
    SymbolicRelationCreate,
    SymbolicSourceBinding,
)
from app.services.memory_symbolic_store import SymbolicStoreError
from app.services.memory_text_views import text_view_sha256
from app.services.state_service import StateService
from tests.test_memory_canonical_store import ReviewedNormalizer


async def service(tmp_path, *, canonical=False):
    state = StateService(
        tmp_path / "state.db",
        canonical_language="en" if canonical else "legacy",
        memory_normalizer=ReviewedNormalizer() if canonical else None,
    )
    await state.initialize()
    return state


def identity(value="cache", namespace="software"):
    return IdentityTerm(namespace=namespace, identity=value)


def claim(**changes):
    values = {
        "scope": "general",
        "namespace": "software",
        "scheme_id": "engineering",
        "kind": "fact",
        "subject": identity(),
        "predicate": identity("may_explain"),
        "object": identity("delay"),
        "polarity": "affirmed",
        "modality": "possible",
    }
    values.update(changes)
    return SymbolicClaim(**values)


async def memory(state, content="The cache may explain the delay.", **kwargs):
    item = await state.create_memory(MemoryCreate(content=content, **kwargs), "phone")
    return item, await state.symbolic_memory.describe_source(item["id"], scope=item["scope"])


async def propose(state, bindings, **claim_changes):
    return await state.symbolic_memory.create_proposal(
        SymbolicProposalCreate(claim=claim(**claim_changes), sources=bindings), "phone"
    )


async def concept(state, **changes):
    fields = {
        "scope": "general",
        "namespace": "software",
        "scheme_id": "engineering",
        "labels": [
            ConceptLabel(text="Cache", language=LanguageAnnotation(tag="fr", origin="declared"))
        ],
    }
    fields.update(changes)
    return await state.symbolic_memory.create_concept(SymbolicConceptCreate(**fields), "phone")


async def rows(state, sql, args=()):
    async with aiosqlite.connect(state.db_path) as db:
        return await (await db.execute(sql, args)).fetchall()


async def mutate(state, sql, args=()):
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(sql, args)
        await db.commit()


async def snapshot(state):
    tables = (
        "memory_symbolic_concepts",
        "memory_symbolic_labels",
        "memory_symbolic_proposals",
        "memory_symbolic_sources",
        "memory_symbolic_links",
        "memory_symbolic_relations",
        "audit_events",
    )
    return {table: await rows(state, f"SELECT * FROM {table}") for table in tables}


@pytest.mark.asyncio
async def test_original_field_bytes_and_document_hash_are_distinct_and_do_not_expose_text(tmp_path):
    state = await service(tmp_path)
    text = "Ne pas modifier Cafe\u0301/rapport.csv ; 30 ms, peut-être."
    item, source = await memory(state, text, summary="Conserver CASE et accent.")
    assert source.language == "und"
    assert source.revision == 1 and source.grants_authority is False
    assert len(source.bindings) == 2
    assert source.bindings[0].field_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert source.bindings[0].document_sha256 == text_view_sha256(text, item["summary"])
    assert source.bindings[0].field_sha256 != source.bindings[0].document_sha256
    assert text not in source.model_dump_json() and item["summary"] not in source.model_dump_json()
    result = await propose(
        state,
        source.bindings,
        polarity="negated",
        modality="forbidden",
        object=TypedLiteral(datatype="path", lexical_value="Cafe\u0301/rapport.csv"),
        effective_conditions=[
            EffectiveCondition(relation="only_after", argument=identity("approval"))
        ],
    )
    assert result.claim.polarity == "negated" and result.claim.modality == "forbidden"
    assert result.claim.object.lexical_value == "Cafe\u0301/rapport.csv"
    assert result.sources[0].origin == "source_document"
    assert result.validation_status == "unvalidated" and result.grants_authority is False
    assert all(source.validation_status == "unvalidated" for source in result.sources)
    events = await rows(
        state, "SELECT payload_json FROM audit_events WHERE event_type LIKE 'memory.symbolic.%'"
    )
    assert all(text not in entry[0] and "Cafe" not in entry[0] for entry in events)


@pytest.mark.asyncio
async def test_identical_claim_hash_is_not_observation_identity_or_deduplication(tmp_path):
    state = await service(tmp_path)
    item, source = await memory(state)
    first = await propose(state, source.bindings)
    second = await propose(state, source.bindings)
    assert first.proposal_id != second.proposal_id
    assert first.claim_sha256 == second.claim_sha256
    page = await state.symbolic_memory.list_for_memory(item["id"], scope="general")
    assert {p.proposal_id for p in page.proposals} == {first.proposal_id, second.proposal_id}
    assert not page.has_more


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 2),
        ("view_id", "mtv_forged"),
        ("field", "summary"),
        ("field_sha256", "0" * 64),
        ("document_sha256", "0" * 64),
        ("memory_id", "mem_missing"),
    ],
)
async def test_forged_source_binding_cannot_create_a_proposal(tmp_path, field, value):
    state = await service(tmp_path)
    _, source = await memory(state)
    forged = SymbolicSourceBinding.model_validate({**source.bindings[0].model_dump(), field: value})
    before = await snapshot(state)
    with pytest.raises(SymbolicStoreError):
        await propose(state, [forged])
    assert await snapshot(state) == before


@pytest.mark.asyncio
async def test_stale_source_is_refused_and_pin_only_change_keeps_evidence_valid(tmp_path):
    state = await service(tmp_path)
    item, source = await memory(state)
    first = await propose(state, source.bindings)
    await state.update_memory(item["id"], MemoryUpdate(pinned=True), "phone")
    await propose(state, source.bindings)
    await state.update_memory(
        item["id"], MemoryUpdate(content="The cache does not explain it."), "phone"
    )
    with pytest.raises(SymbolicStoreError, match="source_changed"):
        await propose(state, source.bindings)
    page = await state.symbolic_memory.list_for_memory(item["id"], scope="general")
    assert not page.proposals
    assert {entry.reason for entry in page.unavailable} == {"stale_source"}
    assert first.proposal_id in {entry.proposal_id for entry in page.unavailable}
    assert "may_explain" not in page.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["receipt", "journal", "original", "original_kind"])
async def test_canonical_source_requires_current_receipt_and_exact_original(tmp_path, tamper):
    state = await service(tmp_path, canonical=True)
    item, source = await memory(state, "Ne pas envoyer automatiquement.")
    assert source.language == "fr"
    assert (
        source.bindings[0].field_sha256
        == hashlib.sha256(b"Ne pas envoyer automatiquement.").hexdigest()
    )
    first = await propose(state, source.bindings)
    if tamper == "receipt":
        await mutate(state, "UPDATE memory_canonical_receipts SET status='failed'")
    elif tamper == "journal":
        await mutate(state, "UPDATE memory_source_journal SET content='Different source'")
    elif tamper == "original":
        await mutate(state, "UPDATE memory_text_views SET content='Forged' WHERE role='original'")
    else:
        await mutate(state, "UPDATE memory_text_views SET kind='instruction' WHERE role='original'")
    with pytest.raises(SymbolicStoreError, match="source_invalid"):
        await state.symbolic_memory.describe_source(item["id"], scope="general")
    with pytest.raises(SymbolicStoreError, match="source_invalid"):
        await propose(state, source.bindings)
    assert await rows(state, "SELECT id FROM memory_symbolic_proposals") == [(first.proposal_id,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "legacy", "project:"])
async def test_unsupported_scope_is_explicitly_refused(tmp_path, scope):
    state = await service(tmp_path)
    item = await state.create_memory(MemoryCreate(content="Legacy text", scope=scope), "phone")
    with pytest.raises(SymbolicStoreError, match="unsupported_scope") as exc:
        await state.symbolic_memory.describe_source(item["id"], scope=scope)
    assert exc.value.status_code == 422
    with pytest.raises(ValidationError):
        claim(scope=scope)


@pytest.mark.asyncio
async def test_scope_and_sensitivity_visibility_is_checked_before_limit_even_if_other_source_stale(
    tmp_path,
    monkeypatch,
):
    state = await service(tmp_path)
    target, target_source = await memory(state)
    hidden, hidden_source = await memory(state, "PRIVATE-CONTENT")
    stale, stale_source = await memory(state, "Changing source")
    await propose(
        state,
        target_source.bindings + stale_source.bindings + hidden_source.bindings,
        object=identity("PRIVATE-CLAIM"),
    )
    visible = await propose(state, target_source.bindings)
    await state.update_memory(stale["id"], MemoryUpdate(content="Updated source"), "phone")
    await mutate(state, "UPDATE memory_items SET sensitivity='secret' WHERE id=?", (hidden["id"],))
    validated = []
    original = store_module._proposal

    async def observe(db, proposal_id, scope, **kwargs):
        validated.append(proposal_id)
        return await original(db, proposal_id, scope, **kwargs)

    monkeypatch.setattr(store_module, "_proposal", observe)
    page = await state.symbolic_memory.list_for_memory(target["id"], scope="general", limit=1)
    assert [p.proposal_id for p in page.proposals] == [visible.proposal_id]
    assert not page.unavailable and not page.has_more and "PRIVATE" not in page.model_dump_json()
    assert validated == [visible.proposal_id]  # SQL visibility precedes candidate dispatch.
    with pytest.raises(SymbolicStoreError, match="source_not_available"):
        await state.symbolic_memory.describe_source(hidden["id"], scope="general")
    with pytest.raises(SymbolicStoreError, match="source_not_available"):
        await state.symbolic_memory.list_for_memory(target["id"], scope="project:other")
    with pytest.raises(SymbolicStoreError, match="source_not_available"):
        await propose(state, target_source.bindings, scope="project:other")


@pytest.mark.asyncio
async def test_candidate_validation_fanout_is_bounded_to_limit_plus_one(tmp_path, monkeypatch):
    state = await service(tmp_path)
    item, source = await memory(state)
    for _ in range(8):
        await propose(state, source.bindings)
    validated = []
    original = store_module._proposal

    async def observe(db, proposal_id, scope, **kwargs):
        validated.append(proposal_id)
        return await original(db, proposal_id, scope, **kwargs)

    monkeypatch.setattr(store_module, "_proposal", observe)
    page = await state.symbolic_memory.list_for_memory(item["id"], scope="general", limit=2)
    assert len(page.proposals) == 2 and page.has_more
    assert len(validated) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["subject", "predicate", "object", "condition"])
async def test_persisted_concepts_are_consumed_in_all_identity_positions(tmp_path, location):
    state = await service(tmp_path)
    _, source = await memory(state)
    entry = await concept(state)
    assert entry.curation_status == "proposed" and not entry.grants_authority
    changes = (
        {location: identity(entry.concept_id)}
        if location != "condition"
        else {
            "effective_conditions": [
                EffectiveCondition(relation="if", argument=identity(entry.concept_id))
            ]
        }
    )
    result = await propose(state, source.bindings, **changes)
    assert result.concept_ids == [entry.concept_id]
    assert await rows(state, "SELECT proposal_id,concept_id FROM memory_symbolic_links") == [
        (result.proposal_id, entry.concept_id)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["unknown", "scope", "namespace", "scheme", "term_namespace"])
async def test_concept_reference_cannot_cross_catalog_boundaries(tmp_path, mismatch):
    state = await service(tmp_path)
    _, source = await memory(state)
    changes = {"scope": "project:elsewhere"} if mismatch == "scope" else {}
    if mismatch == "namespace":
        changes["namespace"] = "other"
    if mismatch == "scheme":
        changes["scheme_id"] = "other"
    entry = await concept(state, **changes)
    ref = "urn:swarmer:concept:" + "f" * 32 if mismatch == "unknown" else entry.concept_id
    term = identity(ref, "other" if mismatch == "term_namespace" else "software")
    before = await snapshot(state)
    with pytest.raises(SymbolicStoreError, match="concept_not_available"):
        await propose(state, source.bindings, subject=term)
    assert await snapshot(state) == before


@pytest.mark.asyncio
async def test_contradictions_and_supersession_remain_explicit_unvalidated_and_acyclic(tmp_path):
    state = await service(tmp_path)
    item, source = await memory(state)
    first = await propose(state, source.bindings)
    second = await propose(state, source.bindings, polarity="negated")
    third = await propose(state, source.bindings, modality="unknown")

    async def relate(a, b, relationship):
        return await state.symbolic_memory.relate(
            a.proposal_id,
            SymbolicRelationCreate(target_proposal_id=b.proposal_id, relationship=relationship),
            "phone",
        )

    contradiction = await relate(second, first, "contradicts")
    assert not contradiction.grants_authority
    edge = await relate(second, first, "supersedes")
    assert await relate(second, first, "supersedes") == edge
    await relate(third, second, "supersedes")
    for a, b in ((first, third), (third, first)):
        with pytest.raises(SymbolicStoreError, match="supersession_conflict"):
            await relate(a, b, "supersedes")
    with pytest.raises(SymbolicStoreError, match="relation_self_reference"):
        await relate(first, first, "related_to")
    page = await state.symbolic_memory.list_for_memory(item["id"], scope="general")
    assert {p.proposal_id: p.lifecycle for p in page.proposals} == {
        first.proposal_id: "superseded",
        second.proposal_id: "superseded",
        third.proposal_id: "active",
    }
    assert all(
        p.validation_status == "unvalidated" and not p.grants_authority for p in page.proposals
    )
    assert len(page.relations) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stale", "secret", "scope", "invalid_receipt"])
async def test_ineligible_replacement_cannot_change_visible_lifecycle(tmp_path, change):
    state = await service(tmp_path, canonical=change == "invalid_receipt")
    first_memory, first_source = await memory(state, "First evidence")
    second_memory, second_source = await memory(state, "Ne pas envoyer automatiquement.")
    first = await propose(state, first_source.bindings)
    second = await propose(state, second_source.bindings, polarity="negated")
    await state.symbolic_memory.relate(
        second.proposal_id,
        SymbolicRelationCreate(target_proposal_id=first.proposal_id, relationship="supersedes"),
        "phone",
    )
    assert (
        await state.symbolic_memory.list_for_memory(first_memory["id"], scope="general")
    ).proposals[0].lifecycle == "superseded"
    if change == "stale":
        await state.update_memory(second_memory["id"], MemoryUpdate(content="Different"), "phone")
    elif change == "invalid_receipt":
        await mutate(
            state,
            "UPDATE memory_canonical_receipts SET status='failed' WHERE memory_id=?",
            (second_memory["id"],),
        )
    else:
        column, value = (
            ("sensitivity", "secret") if change == "secret" else ("scope", "project:other")
        )
        await mutate(
            state, f"UPDATE memory_items SET {column}=? WHERE id=?", (value, second_memory["id"])
        )
    page = await state.symbolic_memory.list_for_memory(first_memory["id"], scope="general")
    assert page.proposals[0].lifecycle == "active" and not page.relations


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["scope", "namespace", "scheme_id"])
async def test_relation_cannot_cross_scope_namespace_or_scheme(tmp_path, difference):
    state = await service(tmp_path)
    _, source = await memory(state)
    first = await propose(state, source.bindings)
    changes = {difference: "project:other" if difference == "scope" else "other"}
    if difference == "scope":
        _, source = await memory(state, "Other project", scope="project:other")
    second = await propose(state, source.bindings, **changes)
    with pytest.raises(SymbolicStoreError):
        await state.symbolic_memory.relate(
            first.proposal_id,
            SymbolicRelationCreate(
                target_proposal_id=second.proposal_id, relationship="related_to"
            ),
            "phone",
        )
    assert not await rows(state, "SELECT * FROM memory_symbolic_relations")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["concept", "proposal", "relation"])
async def test_audit_failure_rolls_back_entire_mutation(tmp_path, monkeypatch, operation):
    state = await service(tmp_path)
    _, source = await memory(state)
    first = await propose(state, source.bindings)
    second = await propose(state, source.bindings)
    before = await snapshot(state)
    original = store_module.append_audit_event

    async def fail_after_audit(db, *args, **kwargs):
        assert db.in_transaction
        await original(db, *args, **kwargs)
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(store_module, "append_audit_event", fail_after_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        if operation == "concept":
            await concept(state)
        elif operation == "proposal":
            await propose(state, source.bindings)
        else:
            await state.symbolic_memory.relate(
                first.proposal_id,
                SymbolicRelationCreate(
                    target_proposal_id=second.proposal_id, relationship="contradicts"
                ),
                "phone",
            )
    assert await snapshot(state) == before


@pytest.mark.asyncio
async def test_page_byte_limit_paginates_without_validation_failure(tmp_path):
    state = await service(tmp_path)
    item, source = await memory(state)
    for index in range(36):
        await propose(
            state,
            source.bindings,
            subject=identity(str(index)),
            object=TypedLiteral(datatype="code", lexical_value="a" * 3900),
        )
    page = await state.symbolic_memory.list_for_memory(item["id"], scope="general", limit=50)
    assert 1 < len(page.proposals) < 36 and page.has_more
    assert len(json.dumps(page.model_dump(), ensure_ascii=False).encode()) <= 128 * 1024
    short = await state.symbolic_memory.list_for_memory(item["id"], scope="general", limit=1)
    assert len(short.proposals) == 1 and short.has_more


def test_request_rejects_authority_origin_duplicates_and_fingerprint_overflow():
    binding = SymbolicSourceBinding(
        memory_id="mem_test",
        revision=1,
        view_id="mtv_test",
        field="content",
        field_sha256="a" * 64,
        document_sha256="b" * 64,
    )
    request = SymbolicProposalCreate(claim=claim(), sources=[binding]).model_dump()
    for name, value in (
        ("validation_status", "accepted"),
        ("grants_authority", True),
        ("origin", "user_statement"),
    ):
        with pytest.raises(ValidationError):
            SymbolicProposalCreate.model_validate({**request, name: value})
    forged = deepcopy(request)
    forged["sources"][0]["origin"] = "tool_result"
    with pytest.raises(ValidationError):
        SymbolicProposalCreate.model_validate(forged)
    with pytest.raises(ValidationError, match="duplicate source field"):
        SymbolicProposalCreate(claim=claim(), sources=[binding, binding])
    huge = claim(
        object=TypedLiteral(datatype="code", lexical_value="a" * 4000),
        applicability={"extra": "b" * 4000},
    )
    with pytest.raises(ValidationError, match="byte budget"):
        SymbolicProposalCreate(claim=huge, sources=[binding])


@pytest.mark.asyncio
async def test_concept_read_after_restart_is_scoped_and_revalidates_labels(tmp_path):
    state = await service(tmp_path)
    created = await concept(state)
    restarted = StateService(state.db_path)
    await restarted.initialize()
    assert (
        await restarted.symbolic_memory.get_concept(created.concept_id, scope="general") == created
    )
    for concept_id, scope in ((created.concept_id, "project:other"), ("missing", "general")):
        with pytest.raises(SymbolicStoreError, match="concept_not_found") as exc:
            await restarted.symbolic_memory.get_concept(concept_id, scope=scope)
        assert exc.value.status_code == 404
    await mutate(
        state, "UPDATE memory_symbolic_labels SET text='' WHERE concept_id=?", (created.concept_id,)
    )
    with pytest.raises(SymbolicStoreError, match="concept_invalid"):
        await restarted.symbolic_memory.get_concept(created.concept_id, scope="general")
