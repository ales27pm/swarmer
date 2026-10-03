"""Explicitly selected, scoped symbolic evidence. No inference or implicit promotion."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import AsyncIterator
from typing import Any

import aiosqlite

from app.services.memory_concepts import ConceptDefinition, ConceptLabel, LanguageAnnotation
from app.services.memory_symbolic_contracts import (
    SymbolicCatalog,
    SymbolicEvidence,
    SymbolicMatch,
    SymbolicProposalRecord,
)
from app.services.memory_symbolic_store import (
    SymbolicStoreError,
    _one,
    _proposal,
    _relation,
    _scope,
)


def _contains(query: str, value: str, *, identity: bool = False) -> bool:
    if not value:
        return False
    if not identity:
        return bool(re.search(r"(?<![\w])" + re.escape(value) + r"(?![\w])", query))
    # Opaque identity continuations include punctuation, combining marks and
    # non-Latin characters. Only explicit prose/quoting delimiters bound a token.
    delimiters = "`\"'()[]{},;<>"
    start = query.find(value)
    while start >= 0:
        end = start + len(value)
        left = start == 0 or query[start - 1].isspace() or query[start - 1] in delimiters
        right = end == len(query) or query[end].isspace() or query[end] in delimiters
        if left and right:
            return True
        start = query.find(value, start + 1)
    return False


def _token(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


async def _concept(db: aiosqlite.Connection, concept_id: str, scope: str) -> ConceptDefinition:
    row = await _one(
        db, "SELECT * FROM memory_symbolic_concepts WHERE id=? AND scope=?", (concept_id, scope)
    )
    if row is None or row["curation_status"] != "proposed" or row["grants_authority"] != 0:
        raise SymbolicStoreError("symbolic_unavailable")
    cursor = await db.execute(
        "SELECT * FROM memory_symbolic_labels WHERE concept_id=? ORDER BY ordinal", (concept_id,)
    )
    cursor.row_factory = aiosqlite.Row
    return ConceptDefinition(
        concept_id=row["id"],
        scope=row["scope"],
        namespace=row["namespace"],
        scheme_id=row["scheme_id"],
        labels=[
            ConceptLabel(
                text=label["text"],
                role=label["role"],
                language=LanguageAnnotation(tag=label["language"], origin=label["language_origin"]),
            )
            for label in await cursor.fetchall()
        ],
    )


async def _evidence(
    db: aiosqlite.Connection,
    proposal_id: str,
    scope: str,
    matches: list[SymbolicMatch],
    *,
    proposal: SymbolicProposalRecord | None = None,
    concepts: list[ConceptDefinition] | None = None,
) -> SymbolicEvidence:
    proposal = proposal if proposal is not None else await _proposal(db, proposal_id, scope)
    if proposal.lifecycle != "active":
        raise SymbolicStoreError("symbolic_superseded")
    concepts = (
        concepts
        if concepts is not None
        else [await _concept(db, ident, scope) for ident in proposal.concept_ids]
    )
    cursor = await db.execute(
        "SELECT * FROM memory_symbolic_relations WHERE proposal_id=? OR target_proposal_id=? ORDER BY id",
        (proposal_id, proposal_id),
    )
    cursor.row_factory = aiosqlite.Row
    relations = []
    async for row in cursor:
        other_id = (
            row["target_proposal_id"] if row["proposal_id"] == proposal_id else row["proposal_id"]
        )
        try:
            other = await _proposal(db, other_id, scope)
            if (other.claim.namespace, other.claim.scheme_id) != (
                proposal.claim.namespace,
                proposal.claim.scheme_id,
            ):
                continue
        except SymbolicStoreError:
            continue
        relations.append(_relation(row))
        if len(relations) > 100:
            raise SymbolicStoreError("symbolic_result_too_large", 409)
    if any(
        match not in _matches(match.value, proposal.claim.model_dump(), concepts)
        for match in matches
    ):
        raise SymbolicStoreError("symbolic_match_invalid")
    payload = {
        "schema_version": "symbolic-evidence-v1",
        "catalog": {"namespace": proposal.claim.namespace, "scheme_id": proposal.claim.scheme_id},
        "proposal": proposal.model_dump(),
        "matches": [m.model_dump() for m in matches],
        "concepts": [c.model_dump() for c in concepts],
        "relations": [r.model_dump() for r in relations],
        "read_token": "0" * 64,
        "validation_status": "unvalidated",
        "grants_authority": False,
    }
    if (
        len(matches) > 64
        or len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 128 * 1024
    ):
        raise SymbolicStoreError("symbolic_result_too_large", 409)
    evidence = SymbolicEvidence.model_validate(payload)
    return evidence.model_copy(
        update={"read_token": _token(evidence.model_dump(exclude={"read_token"}))}
    )


def _matches(
    query: str, proposal: dict[str, Any], concepts: list[ConceptDefinition]
) -> list[SymbolicMatch]:
    result = []
    normalized = unicodedata.normalize("NFC", query)
    for concept in concepts:
        for label in concept.labels:
            if _contains(normalized, label.text):
                result.append(
                    SymbolicMatch(
                        channel="concept_label",
                        value=label.text,
                        concept_id=concept.concept_id,
                        language=label.language.tag,
                    )
                )
    terms = [(name, proposal[name]) for name in ("subject", "predicate", "object")]
    terms += [
        (f"effective_conditions.{i}", condition["argument"])
        for i, condition in enumerate(proposal.get("effective_conditions", []))
    ]
    for field, term in terms:
        value = term.get("identity") if term["type"] == "identity" else term.get("lexical_value")
        if value and _contains(query, value, identity=True):
            result.append(SymbolicMatch(channel="exact_identity", value=value, field=field))
    version = proposal.get("version")
    if version and _contains(query, version, identity=True):
        result.append(SymbolicMatch(channel="exact_identity", value=version, field="version"))
    return result


async def _iter_evidence(
    db: aiosqlite.Connection,
    query: str,
    *,
    allowed_scopes: tuple[str, ...],
    catalogs: tuple[SymbolicCatalog, ...],
    kind: str | None = None,
    memory_ids: tuple[str, ...] | None = None,
) -> AsyncIterator[SymbolicEvidence]:
    if not db.in_transaction:
        raise RuntimeError("symbolic search requires a read transaction")
    if not catalogs or not allowed_scopes:
        return
    for scope in allowed_scopes:
        _scope(scope)
    catalog_keys = {(c.namespace, c.scheme_id) for c in catalogs}
    normalized_query = unicodedata.normalize("NFC", query)

    def identity_candidate(raw: str) -> bool:
        try:
            return bool(_matches(query, json.loads(raw), []))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return True  # Malformed visible candidates take the explicit qualification-error path.

    def label_candidate(raw: str) -> bool:
        return (
            _contains(normalized_query, unicodedata.normalize("NFC", raw))
            if isinstance(raw, str)
            else True
        )

    def concept_candidates(raw: str) -> str:
        try:
            claim = json.loads(raw)
            terms = [claim[key] for key in ("subject", "predicate", "object")]
            terms.extend(item["argument"] for item in claim.get("effective_conditions", []))
            return json.dumps(
                [
                    term["identity"]
                    for term in terms
                    if term.get("type") == "identity"
                    and isinstance(term.get("identity"), str)
                    and term["identity"].startswith("urn:swarmer:concept:")
                ]
            )
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return "[]"

    await db.create_function(
        "symbolic_concept_candidates", 1, concept_candidates, deterministic=True
    )
    await db.create_function(
        "symbolic_identity_candidate", 1, identity_candidate, deterministic=True
    )
    await db.create_function("symbolic_label_candidate", 1, label_candidate, deterministic=True)
    # Materialize authorized candidates before text reaches either prefilter.
    # Prefilters grant nothing: every possible hit still receives full current-source qualification.
    selected_ids = json.dumps(memory_ids) if memory_ids is not None else None
    cursor = await db.execute(
        """WITH scoped AS MATERIALIZED (
        SELECT p.* FROM memory_symbolic_proposals p
        WHERE p.scope IN (SELECT value FROM json_each(?))
        AND EXISTS(SELECT 1 FROM json_each(?) c
          WHERE json_extract(c.value,'$.namespace')=p.namespace
          AND json_extract(c.value,'$.scheme_id')=p.scheme_id)
        AND NOT EXISTS(SELECT 1 FROM memory_symbolic_sources s LEFT JOIN memory_items m ON m.id=s.memory_id
          WHERE s.proposal_id=p.id AND (m.id IS NULL OR m.scope<>p.scope OR m.sensitivity<>'normal'))
        AND (? IS NULL OR EXISTS(SELECT 1 FROM memory_symbolic_sources s JOIN memory_items m ON m.id=s.memory_id
          WHERE s.proposal_id=p.id AND m.kind=?))
        AND (? IS NULL OR EXISTS(SELECT 1 FROM memory_symbolic_sources s WHERE s.proposal_id=p.id
          AND s.memory_id IN (SELECT value FROM json_each(?))))
        ) SELECT p.id,p.scope,p.namespace,p.scheme_id FROM scoped p
        WHERE symbolic_identity_candidate(p.claim_json) OR EXISTS(
          SELECT 1 FROM memory_symbolic_concepts c
          JOIN memory_symbolic_labels labels ON labels.concept_id=c.id
          WHERE c.id IN (SELECT value FROM json_each(symbolic_concept_candidates(p.claim_json))) AND c.scope=p.scope AND c.namespace=p.namespace AND c.scheme_id=p.scheme_id
          AND symbolic_label_candidate(labels.text)) ORDER BY p.id""",
        (
            json.dumps(allowed_scopes),
            json.dumps([c.model_dump() for c in catalogs]),
            kind,
            kind,
            selected_ids,
            selected_ids,
        ),
    )
    concept_cache: dict[tuple[str, str], ConceptDefinition] = {}
    cursor.row_factory = aiosqlite.Row
    async for row in cursor:
        if (row["namespace"], row["scheme_id"]) not in catalog_keys:
            continue
        try:
            proposal = await _proposal(db, row["id"], row["scope"])
            if proposal.lifecycle != "active":
                continue
            concepts = []
            for ident in proposal.concept_ids:
                key = (ident, row["scope"])
                if key not in concept_cache:
                    if len(concept_cache) >= 128:
                        concept_cache.pop(next(iter(concept_cache)))
                    concept_cache[key] = await _concept(db, ident, row["scope"])
                concepts.append(concept_cache[key])
            matches = _matches(query, proposal.claim.model_dump(), concepts)
            if not matches:
                continue
            evidence = await _evidence(
                db, row["id"], row["scope"], matches, proposal=proposal, concepts=concepts
            )
        except SymbolicStoreError as exc:
            if exc.status_code == 404 or exc.code == "source_changed":
                continue
            if exc.code == "symbolic_result_too_large":
                raise
            raise SymbolicStoreError("symbolic_unavailable", 409) from exc
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise SymbolicStoreError("symbolic_unavailable", 409) from exc
        yield evidence


def _rank(item: SymbolicEvidence) -> tuple[int, str]:
    return -len(item.matches), item.proposal.proposal_id


async def search_symbolic_evidence(
    db: aiosqlite.Connection,
    query: str,
    *,
    allowed_scopes: tuple[str, ...],
    catalogs: tuple[SymbolicCatalog, ...],
    kind: str | None = None,
    limit: int = 4,
) -> list[SymbolicEvidence]:
    ranked: list[SymbolicEvidence] = []
    async for item in _iter_evidence(
        db, query, allowed_scopes=allowed_scopes, catalogs=catalogs, kind=kind
    ):
        ranked.append(item)
        ranked.sort(key=_rank)
        del ranked[limit:]
    return ranked


async def search_symbolic_memories(
    db: aiosqlite.Connection,
    query: str,
    *,
    allowed_scopes: tuple[str, ...],
    catalogs: tuple[SymbolicCatalog, ...],
    kind: str | None = None,
    limit: int = 6,
) -> tuple[list[str], list[SymbolicEvidence]]:
    """Two snapshot passes bound memory while retaining every claim of selected memories.

    First retain only the best logical memory IDs. Then reread the same transaction
    for their complete evidence; an excessive response fails explicitly, never slices a claim.
    """
    top: dict[str, tuple[int, str]] = {}
    async for item in _iter_evidence(
        db, query, allowed_scopes=allowed_scopes, catalogs=catalogs, kind=kind
    ):
        ids = list(dict.fromkeys(source.binding.memory_id for source in item.proposal.sources))
        rows = await (
            await db.execute(
                "SELECT id FROM memory_items WHERE id IN (SELECT value FROM json_each(?)) AND (? IS NULL OR kind=?)",
                (json.dumps(ids), kind, kind),
            )
        ).fetchall()
        for row in rows:
            ident = str(row[0])
            top[ident] = min(top.get(ident, _rank(item)), _rank(item))
        top = dict(sorted(top.items(), key=lambda pair: (pair[1], pair[0]))[:limit])
    if not top:
        return [], []
    evidence = []
    counts = dict.fromkeys(top, 0)
    total_bytes = 0
    async for item in _iter_evidence(
        db,
        query,
        allowed_scopes=allowed_scopes,
        catalogs=catalogs,
        kind=kind,
        memory_ids=tuple(top),
    ):
        selected = set(top).intersection(
            source.binding.memory_id for source in item.proposal.sources
        )
        if not selected:
            continue
        for ident in selected:
            counts[ident] += 1
            if counts[ident] > 50:
                raise SymbolicStoreError("symbolic_result_too_large", 409)
        total_bytes += len(item.model_dump_json().encode("utf-8")) * len(selected)
        if total_bytes > 128 * 1024:
            raise SymbolicStoreError("symbolic_result_too_large", 409)
        evidence.append(item)
    evidence.sort(key=_rank)
    return list(top), evidence


async def revalidate_symbolic_evidence(
    db: aiosqlite.Connection,
    evidence: SymbolicEvidence,
    *,
    allowed_scopes: tuple[str, ...],
    catalogs: tuple[SymbolicCatalog, ...],
) -> bool:
    if not db.in_transaction:
        raise RuntimeError("symbolic revalidation requires a read transaction")
    try:
        evidence = SymbolicEvidence.model_validate(evidence.model_dump())
        if evidence.proposal.claim.scope not in allowed_scopes or evidence.catalog not in catalogs:
            return False
        current = await _evidence(
            db, evidence.proposal.proposal_id, evidence.proposal.claim.scope, evidence.matches
        )
        return current == evidence
    except (SymbolicStoreError, ValueError, TypeError, KeyError, RecursionError):
        return False
