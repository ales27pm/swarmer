"""Bounded public proposals; source descriptions contain bindings, never source text."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.services.memory_concepts import (
    ConceptDefinition,
    ConceptLabel,
    SymbolicClaim,
    claim_fingerprint,
)

SymbolicScope = Annotated[
    str, StringConstraints(pattern=r"^(general|project:[A-Za-z0-9][A-Za-z0-9_.:-]{0,199})$")
]
SymbolicIdentifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
]
SymbolicHash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
SymbolicConceptId = Annotated[str, StringConstraints(pattern=r"^urn:swarmer:concept:[a-f0-9]{32}$")]


class SymbolicPublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SymbolicConceptCreate(SymbolicPublicModel):
    scope: SymbolicScope
    namespace: SymbolicIdentifier
    scheme_id: SymbolicIdentifier
    labels: list[ConceptLabel] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_labels(self) -> Self:
        ConceptDefinition(concept_id="urn:swarmer:concept:" + "0" * 32, **self.model_dump())
        return self


class SymbolicSourceBinding(SymbolicPublicModel):
    memory_id: SymbolicIdentifier
    revision: int = Field(ge=1, le=2**53 - 1)
    view_id: SymbolicIdentifier
    field: Literal["content", "summary"]
    field_sha256: SymbolicHash
    document_sha256: SymbolicHash


class SymbolicSourceDescription(SymbolicPublicModel):
    memory_id: SymbolicIdentifier
    scope: SymbolicScope
    revision: int = Field(ge=1, le=2**53 - 1)
    view_id: SymbolicIdentifier
    language: str = Field(min_length=1, max_length=100)
    bindings: list[SymbolicSourceBinding] = Field(min_length=1, max_length=2)
    grants_authority: Literal[False] = False


class SymbolicProposalCreate(SymbolicPublicModel):
    claim: SymbolicClaim
    sources: list[SymbolicSourceBinding] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def bounded_unique_sources(self) -> Self:
        # The fingerprint has tighter structural bounds than the whole request.
        # Reject here so malformed claims become public validation errors before SQL.
        claim_fingerprint(self.claim)
        identities = [(source.memory_id, source.field) for source in self.sources]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate source field")
        if len(self.model_dump_json().encode("utf-8")) > 64 * 1024:
            raise ValueError("symbolic proposal byte budget exceeded")
        return self


class SymbolicResolvedSource(SymbolicPublicModel):
    binding: SymbolicSourceBinding
    scope: SymbolicScope
    origin: Literal["user_statement", "source_document"]
    validation_status: Literal["unvalidated"] = "unvalidated"


class SymbolicProposalRecord(SymbolicPublicModel):
    proposal_id: SymbolicIdentifier
    claim: SymbolicClaim
    claim_sha256: SymbolicHash
    sources: list[SymbolicResolvedSource] = Field(min_length=1, max_length=32)
    concept_ids: list[SymbolicConceptId] = Field(max_length=35)
    lifecycle: Literal["active", "superseded"]
    validation_status: Literal["unvalidated"] = "unvalidated"
    grants_authority: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=64)


class SymbolicRelationCreate(SymbolicPublicModel):
    target_proposal_id: SymbolicIdentifier
    relationship: Literal["supersedes", "contradicts", "related_to"]


class SymbolicRelationRecord(SymbolicPublicModel):
    id: SymbolicIdentifier
    proposal_id: SymbolicIdentifier
    target_proposal_id: SymbolicIdentifier
    relationship: Literal["supersedes", "contradicts", "related_to"]
    created_at: str = Field(min_length=1, max_length=64)
    grants_authority: Literal[False] = False


class SymbolicUnavailableProposal(SymbolicPublicModel):
    proposal_id: SymbolicIdentifier
    reason: Literal["stale_source", "missing_source", "invalid_source"]


class SymbolicMemoryPage(SymbolicPublicModel):
    memory_id: SymbolicIdentifier
    scope: SymbolicScope
    proposals: list[SymbolicProposalRecord] = Field(max_length=50)
    unavailable: list[SymbolicUnavailableProposal] = Field(max_length=50)
    relations: list[SymbolicRelationRecord] = Field(max_length=100)
    has_more: bool
    grants_authority: Literal[False] = False

    @model_validator(mode="after")
    def bounded_page(self) -> Self:
        if len(json.dumps(self.model_dump(), ensure_ascii=False).encode("utf-8")) > 128 * 1024:
            raise ValueError("symbolic page byte budget exceeded")
        return self
