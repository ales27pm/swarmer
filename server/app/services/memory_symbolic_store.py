"""SQL-backed symbolic proposals tied to exact original evidence, never authority."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite
from pydantic import ValidationError

from app.services.audit_log import append_audit_event
from app.services.memory_concepts import (
    ConceptDefinition,
    ConceptLabel,
    IdentityTerm,
    LanguageAnnotation,
    SymbolicClaim,
    claim_fingerprint,
    new_concept_id,
    source_bytes_sha256,
)
from app.services.memory_symbolic_contracts import (
    SymbolicConceptCreate,
    SymbolicMemoryPage,
    SymbolicProposalCreate,
    SymbolicProposalRecord,
    SymbolicRelationCreate,
    SymbolicRelationRecord,
    SymbolicResolvedSource,
    SymbolicSourceBinding,
    SymbolicSourceDescription,
    SymbolicUnavailableProposal,
)
from app.services.memory_text_views import _source, text_view_sha256

_SOURCE_FIELDS: tuple[Literal["content", "summary"], ...] = ("content", "summary")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS memory_symbolic_concepts(
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, namespace TEXT NOT NULL, scheme_id TEXT NOT NULL,
        curation_status TEXT NOT NULL CHECK(curation_status='proposed'),
        grants_authority INTEGER NOT NULL CHECK(grants_authority=0), created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_symbolic_labels(
        concept_id TEXT NOT NULL REFERENCES memory_symbolic_concepts(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL, text TEXT NOT NULL, language TEXT NOT NULL, language_origin TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('pref','alt','hidden')),
        PRIMARY KEY(concept_id,ordinal), UNIQUE(concept_id,language,text))""",
    """CREATE INDEX IF NOT EXISTS idx_symbolic_labels ON memory_symbolic_labels(language,text)""",
    """CREATE TABLE IF NOT EXISTS memory_symbolic_proposals(
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, namespace TEXT NOT NULL, scheme_id TEXT NOT NULL,
        claim_json TEXT NOT NULL, claim_sha256 TEXT NOT NULL,
        validation_status TEXT NOT NULL CHECK(validation_status='unvalidated'),
        grants_authority INTEGER NOT NULL CHECK(grants_authority=0), created_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_symbolic_proposal_scope ON memory_symbolic_proposals(scope,namespace,scheme_id)""",
    """CREATE TABLE IF NOT EXISTS memory_symbolic_sources(
        proposal_id TEXT NOT NULL REFERENCES memory_symbolic_proposals(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL, memory_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        view_id TEXT NOT NULL REFERENCES memory_text_views(id),
        field TEXT NOT NULL CHECK(field IN ('content','summary')), field_sha256 TEXT NOT NULL,
        document_sha256 TEXT NOT NULL, scope TEXT NOT NULL,
        origin TEXT NOT NULL CHECK(origin='source_document'),
        PRIMARY KEY(proposal_id,ordinal), UNIQUE(proposal_id,memory_id,field))""",
    """CREATE INDEX IF NOT EXISTS idx_symbolic_source_memory ON memory_symbolic_sources(memory_id,proposal_id)""",
    """CREATE TABLE IF NOT EXISTS memory_symbolic_links(
        proposal_id TEXT NOT NULL REFERENCES memory_symbolic_proposals(id) ON DELETE CASCADE,
        concept_id TEXT NOT NULL REFERENCES memory_symbolic_concepts(id),
        PRIMARY KEY(proposal_id,concept_id))""",
    """CREATE TABLE IF NOT EXISTS memory_symbolic_relations(
        id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL REFERENCES memory_symbolic_proposals(id) ON DELETE CASCADE,
        target_proposal_id TEXT NOT NULL REFERENCES memory_symbolic_proposals(id) ON DELETE CASCADE,
        relationship TEXT NOT NULL CHECK(relationship IN ('supersedes','contradicts','related_to')),
        created_at TEXT NOT NULL, CHECK(proposal_id<>target_proposal_id),
        UNIQUE(proposal_id,target_proposal_id,relationship))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_symbolic_superseded_once
        ON memory_symbolic_relations(target_proposal_id) WHERE relationship='supersedes'""",
)


class SymbolicStoreError(ValueError):
    def __init__(self, code: str, status_code: int = 409) -> None:
        super().__init__(code)
        self.code, self.status_code = code, status_code


def _scope(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(
        r"general|project:[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value
    ):
        raise SymbolicStoreError("unsupported_scope", 422)


def _locked(db: aiosqlite.Connection) -> None:
    if not db.in_transaction:
        raise RuntimeError("symbolic mutation requires the caller's transaction")


async def initialize_symbolic_schema_locked(db: aiosqlite.Connection) -> None:
    _locked(db)
    for statement in _SCHEMA:
        await db.execute(statement)


async def forget_symbolic_memory_locked(db: aiosqlite.Connection, memory_id: str) -> None:
    """Erase derived claims (including copies of literals), retaining independent concepts."""
    _locked(db)
    ids = [
        row[0]
        for row in await (
            await db.execute(
                "SELECT DISTINCT proposal_id FROM memory_symbolic_sources WHERE memory_id=?",
                (memory_id,),
            )
        ).fetchall()
    ]
    if not ids:
        return
    encoded = json.dumps(ids)
    await db.execute(
        """DELETE FROM memory_symbolic_relations WHERE proposal_id IN (SELECT value FROM json_each(?))
        OR target_proposal_id IN (SELECT value FROM json_each(?))""",
        (encoded, encoded),
    )
    for table in ("memory_symbolic_links", "memory_symbolic_sources"):
        await db.execute(
            f"DELETE FROM {table} WHERE proposal_id IN (SELECT value FROM json_each(?))", (encoded,)
        )
    await db.execute(
        "DELETE FROM memory_symbolic_proposals WHERE id IN (SELECT value FROM json_each(?))",
        (encoded,),
    )


async def _one(db: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> aiosqlite.Row | None:
    cursor = await db.execute(sql, args)
    cursor.row_factory = aiosqlite.Row
    return await cursor.fetchone()


async def _original(db: aiosqlite.Connection, memory_id: str, scope: str) -> aiosqlite.Row:
    # Authoritative scope and sensitivity are checked before any text is exposed.
    item = await _one(
        db, "SELECT scope,kind,sensitivity FROM memory_items WHERE id=?", (memory_id,)
    )
    if item is None:
        raise SymbolicStoreError("source_missing", 404)
    if item["scope"] != scope or item["sensitivity"] != "normal":
        raise SymbolicStoreError("source_not_available", 404)
    head = await _one(
        db, "SELECT * FROM memory_text_heads WHERE memory_id=? AND deleted=0", (memory_id,)
    )
    if (
        head is None
        or await _source(db, memory_id, head["revision"], head["index_view_id"]) is None
    ):
        raise SymbolicStoreError("source_invalid")
    row = await _one(
        db,
        """SELECT * FROM memory_text_views
        WHERE memory_id=? AND revision=? AND role='original'""",
        (memory_id, head["revision"]),
    )
    if (
        row is None
        or row["scope"] != scope
        or row["kind"] != item["kind"]
        or row["sensitivity"] != "normal"
        or row["source_id"] != head["source_id"]
        or row["source_sha256"] != head["source_sha256"]
        or row["text_sha256"] != text_view_sha256(row["content"], row["summary"])
        or row["text_sha256"] != row["source_sha256"]
    ):
        raise SymbolicStoreError("source_invalid")
    return row


def _binding(row: aiosqlite.Row, field: Literal["content", "summary"]) -> SymbolicSourceBinding:
    return SymbolicSourceBinding(
        memory_id=row["memory_id"],
        revision=row["revision"],
        view_id=row["id"],
        field=field,
        field_sha256=source_bytes_sha256(row[field].encode("utf-8")),
        document_sha256=row["source_sha256"],
    )


async def _bound(db: aiosqlite.Connection, binding: SymbolicSourceBinding, scope: str) -> None:
    row = await _original(db, binding.memory_id, scope)
    if row[binding.field] is None or _binding(row, binding.field) != binding:
        raise SymbolicStoreError("source_changed")


def _concept_terms(claim: SymbolicClaim) -> dict[str, str]:
    found: dict[str, str] = {}
    terms = [claim.subject, claim.predicate, claim.object]
    terms.extend(condition.argument for condition in claim.effective_conditions)
    for term in terms:
        if isinstance(term, IdentityTerm) and term.identity.startswith("urn:swarmer:concept:"):
            if term.identity in found and found[term.identity] != term.namespace:
                raise SymbolicStoreError("concept_namespace_mismatch")
            found[term.identity] = term.namespace
    return found


async def _concepts(db: aiosqlite.Connection, claim: SymbolicClaim) -> list[str]:
    terms = _concept_terms(claim)
    for concept_id, namespace in terms.items():
        row = await _one(db, "SELECT * FROM memory_symbolic_concepts WHERE id=?", (concept_id,))
        if (
            row is None
            or (row["scope"], row["namespace"], row["scheme_id"])
            != (claim.scope, claim.namespace, claim.scheme_id)
            or namespace != claim.namespace
            or row["curation_status"] != "proposed"
            or row["grants_authority"] != 0
        ):
            raise SymbolicStoreError("concept_not_available")
    return sorted(terms)


def _source_binding(row: aiosqlite.Row) -> SymbolicSourceBinding:
    return SymbolicSourceBinding(**{key: row[key] for key in SymbolicSourceBinding.model_fields})


async def _proposal(
    db: aiosqlite.Connection, proposal_id: str, scope: str, *, derive_lifecycle: bool = True
) -> SymbolicProposalRecord:
    row = await _one(
        db, "SELECT * FROM memory_symbolic_proposals WHERE id=? AND scope=?", (proposal_id, scope)
    )
    if row is None:
        raise SymbolicStoreError("proposal_not_found", 404)
    try:
        claim = SymbolicClaim.model_validate_json(row["claim_json"])
        if (
            (claim.scope, claim.namespace, claim.scheme_id, claim_fingerprint(claim))
            != (row["scope"], row["namespace"], row["scheme_id"], row["claim_sha256"])
            or row["validation_status"] != "unvalidated"
            or row["grants_authority"] != 0
        ):
            raise ValueError("inconsistent proposal")
        cursor = await db.execute(
            "SELECT * FROM memory_symbolic_sources WHERE proposal_id=? ORDER BY ordinal",
            (proposal_id,),
        )
        cursor.row_factory = aiosqlite.Row
        sources: list[SymbolicResolvedSource] = []
        bound_sources = await cursor.fetchall()
        # Apply visibility to every source first, even if an earlier source is stale.
        # A hidden second source must never consume a pagination slot or reveal a claim.
        for source in bound_sources:
            visibility = await _one(
                db, "SELECT scope,sensitivity FROM memory_items WHERE id=?", (source["memory_id"],)
            )
            if visibility is not None and (
                visibility["scope"] != scope or visibility["sensitivity"] != "normal"
            ):
                raise SymbolicStoreError("source_not_available", 404)
        for source in bound_sources:
            if source["scope"] != scope or source["origin"] != "source_document":
                raise ValueError("inconsistent source")
            binding = _source_binding(source)
            await _bound(db, binding, scope)
            sources.append(
                SymbolicResolvedSource(binding=binding, scope=scope, origin="source_document")
            )
        concepts = await _concepts(db, claim)
        links = [
            item[0]
            for item in await (
                await db.execute(
                    "SELECT concept_id FROM memory_symbolic_links WHERE proposal_id=? ORDER BY concept_id",
                    (proposal_id,),
                )
            ).fetchall()
        ]
        if concepts != links:
            raise ValueError("inconsistent concepts")
        superseded = False
        if derive_lifecycle:
            replacement_row = await _one(
                db,
                "SELECT proposal_id FROM memory_symbolic_relations WHERE target_proposal_id=? AND relationship='supersedes'",
                (proposal_id,),
            )
            if replacement_row is not None:
                try:
                    # Validate the replacement's own evidence; do not recursively walk history.
                    replacement = await _proposal(
                        db, replacement_row["proposal_id"], scope, derive_lifecycle=False
                    )
                    superseded = (replacement.claim.namespace, replacement.claim.scheme_id) == (
                        claim.namespace,
                        claim.scheme_id,
                    )
                except SymbolicStoreError:
                    pass  # Stale or inaccessible evidence cannot affect a visible lifecycle.
        return SymbolicProposalRecord(
            proposal_id=proposal_id,
            claim=claim,
            claim_sha256=row["claim_sha256"],
            sources=sources,
            concept_ids=concepts,
            lifecycle="superseded" if superseded else "active",
            created_at=row["created_at"],
        )
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, SymbolicStoreError):
            raise
        raise SymbolicStoreError("proposal_invalid") from exc


def _relation(row: aiosqlite.Row) -> SymbolicRelationRecord:
    return SymbolicRelationRecord(
        **{
            key: row[key]
            for key in ("id", "proposal_id", "target_proposal_id", "relationship", "created_at")
        }
    )


class MemorySymbolicStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def get_concept(self, concept_id: str, *, scope: str) -> ConceptDefinition:
        _scope(scope)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            row = await _one(
                db,
                "SELECT * FROM memory_symbolic_concepts WHERE id=? AND scope=?",
                (concept_id, scope),
            )
            if row is None:
                raise SymbolicStoreError("concept_not_found", 404)
            if row["curation_status"] != "proposed" or row["grants_authority"] != 0:
                raise SymbolicStoreError("concept_invalid")
            cursor = await db.execute(
                "SELECT * FROM memory_symbolic_labels WHERE concept_id=? ORDER BY ordinal",
                (concept_id,),
            )
            cursor.row_factory = aiosqlite.Row
            try:
                return ConceptDefinition(
                    concept_id=row["id"],
                    scope=row["scope"],
                    namespace=row["namespace"],
                    scheme_id=row["scheme_id"],
                    labels=[
                        ConceptLabel(
                            text=label["text"],
                            role=label["role"],
                            language=LanguageAnnotation(
                                tag=label["language"], origin=label["language_origin"]
                            ),
                        )
                        for label in await cursor.fetchall()
                    ],
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise SymbolicStoreError("concept_invalid") from exc

    async def create_concept(
        self, request: SymbolicConceptCreate, actor_id: str
    ) -> ConceptDefinition:
        request = SymbolicConceptCreate.model_validate(request.model_dump())
        record = ConceptDefinition(concept_id=new_concept_id(), **request.model_dump())
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """INSERT INTO memory_symbolic_concepts
                VALUES(?,?,?,?,'proposed',0,?)""",
                (record.concept_id, record.scope, record.namespace, record.scheme_id, now),
            )
            await db.executemany(
                "INSERT INTO memory_symbolic_labels VALUES(?,?,?,?,?,?)",
                [
                    (
                        record.concept_id,
                        i,
                        label.text,
                        label.language.tag,
                        label.language.origin,
                        label.role,
                    )
                    for i, label in enumerate(record.labels)
                ],
            )
            await append_audit_event(
                db,
                "memory.symbolic.concept_proposed",
                {"concept_id": record.concept_id},
                actor_type="device",
                actor_id=actor_id,
            )
            await db.commit()
        return record

    async def describe_source(self, memory_id: str, *, scope: str) -> SymbolicSourceDescription:
        _scope(scope)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            row = await _original(db, memory_id, scope)
            return SymbolicSourceDescription(
                memory_id=memory_id,
                scope=scope,
                revision=row["revision"],
                view_id=row["id"],
                language=row["language"],
                bindings=[
                    _binding(row, field) for field in _SOURCE_FIELDS if row[field] is not None
                ],
            )

    async def create_proposal(
        self, request: SymbolicProposalCreate, actor_id: str
    ) -> SymbolicProposalRecord:
        request = SymbolicProposalCreate.model_validate(request.model_dump())
        fingerprint = claim_fingerprint(request.claim)
        proposal_id = "msp_" + uuid4().hex
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            for binding in request.sources:
                await _bound(db, binding, request.claim.scope)
            concepts = await _concepts(db, request.claim)
            await db.execute(
                """INSERT INTO memory_symbolic_proposals
                VALUES(?,?,?,?,?,?,'unvalidated',0,?)""",
                (
                    proposal_id,
                    request.claim.scope,
                    request.claim.namespace,
                    request.claim.scheme_id,
                    request.claim.model_dump_json(),
                    fingerprint,
                    now,
                ),
            )
            await db.executemany(
                "INSERT INTO memory_symbolic_sources VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        proposal_id,
                        i,
                        source.memory_id,
                        source.revision,
                        source.view_id,
                        source.field,
                        source.field_sha256,
                        source.document_sha256,
                        request.claim.scope,
                        "source_document",
                    )
                    for i, source in enumerate(request.sources)
                ],
            )
            await db.executemany(
                "INSERT INTO memory_symbolic_links VALUES(?,?)",
                [(proposal_id, identity) for identity in concepts],
            )
            await append_audit_event(
                db,
                "memory.symbolic.claim_proposed",
                {
                    "proposal_id": proposal_id,
                    "claim_sha256": fingerprint,
                    "source_count": len(request.sources),
                },
                actor_type="device",
                actor_id=actor_id,
            )
            record = await _proposal(db, proposal_id, request.claim.scope)
            await db.commit()
        return record

    async def relate(
        self, proposal_id: str, request: SymbolicRelationCreate, actor_id: str
    ) -> SymbolicRelationRecord:
        request = SymbolicRelationCreate.model_validate(request.model_dump())
        if proposal_id == request.target_proposal_id:
            raise SymbolicStoreError("relation_self_reference")
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            row = await _one(
                db, "SELECT scope FROM memory_symbolic_proposals WHERE id=?", (proposal_id,)
            )
            if row is None:
                raise SymbolicStoreError("proposal_not_found", 404)
            source = await _proposal(db, proposal_id, row["scope"])
            target = await _proposal(db, request.target_proposal_id, row["scope"])
            if (source.claim.namespace, source.claim.scheme_id) != (
                target.claim.namespace,
                target.claim.scheme_id,
            ):
                raise SymbolicStoreError("relation_scope_mismatch")
            existing = await _one(
                db,
                """SELECT * FROM memory_symbolic_relations
                WHERE proposal_id=? AND target_proposal_id=? AND relationship=?""",
                (proposal_id, request.target_proposal_id, request.relationship),
            )
            if existing is not None:
                return _relation(existing)
            count = await _one(
                db,
                "SELECT COUNT(*) AS n FROM memory_symbolic_relations WHERE proposal_id=?",
                (proposal_id,),
            )
            if count and count["n"] >= 32:
                raise SymbolicStoreError("relation_limit")
            if request.relationship == "supersedes":
                prior_replacement = await _one(
                    db,
                    """SELECT 1 FROM memory_symbolic_relations WHERE relationship='supersedes'
                    AND target_proposal_id IN (?,?) LIMIT 1""",
                    (proposal_id, request.target_proposal_id),
                )
                cycle = await _one(
                    db,
                    """WITH RECURSIVE previous(id) AS (
                    SELECT target_proposal_id FROM memory_symbolic_relations
                    WHERE proposal_id=? AND relationship='supersedes'
                    UNION SELECT r.target_proposal_id FROM memory_symbolic_relations r JOIN previous p
                    ON r.proposal_id=p.id WHERE r.relationship='supersedes')
                    SELECT 1 FROM previous WHERE id=? LIMIT 1""",
                    (request.target_proposal_id, proposal_id),
                )
                if cycle or prior_replacement:
                    raise SymbolicStoreError("supersession_conflict")
            record = SymbolicRelationRecord(
                id="msr_" + uuid4().hex,
                proposal_id=proposal_id,
                target_proposal_id=request.target_proposal_id,
                relationship=request.relationship,
                created_at=now,
            )
            await db.execute(
                "INSERT INTO memory_symbolic_relations VALUES(?,?,?,?,?)",
                (record.id, proposal_id, request.target_proposal_id, request.relationship, now),
            )
            await append_audit_event(
                db,
                "memory.symbolic.claim_related",
                {
                    "relation_id": record.id,
                    "proposal_id": proposal_id,
                    "target_proposal_id": request.target_proposal_id,
                    "relationship": request.relationship,
                },
                actor_type="device",
                actor_id=actor_id,
            )
            await db.commit()
        return record

    async def list_for_memory(
        self, memory_id: str, *, scope: str, limit: int = 50
    ) -> SymbolicMemoryPage:
        _scope(scope)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise SymbolicStoreError("invalid_limit", 422)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            await _original(db, memory_id, scope)
            cursor = await db.execute(
                """SELECT DISTINCT p.id FROM memory_symbolic_proposals p
                JOIN memory_symbolic_sources s ON s.proposal_id=p.id WHERE s.memory_id=? AND p.scope=?
                AND NOT EXISTS (
                    SELECT 1 FROM memory_symbolic_sources evidence
                    JOIN memory_items item ON item.id=evidence.memory_id
                    WHERE evidence.proposal_id=p.id
                      AND (item.scope<>? OR item.sensitivity<>'normal')
                )
                ORDER BY p.created_at,p.id LIMIT ?""",
                (memory_id, scope, scope, limit + 1),
            )
            page = SymbolicMemoryPage(
                memory_id=memory_id,
                scope=scope,
                proposals=[],
                unavailable=[],
                relations=[],
                has_more=False,
            )
            has_more = False
            async for row in cursor:
                try:
                    record = await _proposal(db, row[0], scope)
                    addition = record
                    missing = None
                except SymbolicStoreError as exc:
                    if exc.code == "source_not_available":
                        continue  # sensitive or foreign evidence never consumes a visible slot
                    reasons: dict[
                        str, Literal["stale_source", "missing_source", "invalid_source"]
                    ] = {"source_changed": "stale_source", "source_missing": "missing_source"}
                    missing = SymbolicUnavailableProposal(
                        proposal_id=row[0], reason=reasons.get(exc.code, "invalid_source")
                    )
                    addition = None
                if len(page.proposals) + len(page.unavailable) == limit:
                    has_more = True
                    break
                candidate = page.model_copy(
                    update={
                        "proposals": page.proposals + ([addition] if addition else []),
                        "unavailable": page.unavailable + ([missing] if missing else []),
                    }
                )
                if not _page_fits(candidate):
                    has_more = True
                    break
                page = candidate
            admitted = {proposal.proposal_id for proposal in page.proposals}
            if admitted:
                cursor = await db.execute(
                    """SELECT * FROM memory_symbolic_relations
                    WHERE proposal_id IN (SELECT value FROM json_each(?))
                    AND target_proposal_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,id""",
                    (json.dumps(sorted(admitted)), json.dumps(sorted(admitted))),
                )
                cursor.row_factory = aiosqlite.Row
                async for row in cursor:
                    if len(page.relations) == 100:
                        has_more = True
                        break
                    candidate = page.model_copy(
                        update={"relations": page.relations + [_relation(row)]}
                    )
                    if not _page_fits(candidate):
                        has_more = True
                        break
                    page = candidate
            return SymbolicMemoryPage.model_validate({**page.model_dump(), "has_more": has_more})


def _page_fits(page: SymbolicMemoryPage) -> bool:
    return len(json.dumps(page.model_dump(), ensure_ascii=False).encode("utf-8")) <= 128 * 1024
