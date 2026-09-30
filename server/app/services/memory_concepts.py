"""Deterministic concept/claim proposals, without extraction, persistence or authority.

Labels follow the SKOS preferred/alternate/hidden distinction. Language tags are
checked for bounded RFC 5646 syntax, not against a downloaded IANA registry, and
are never inferred from labels. Concept IDs are opaque, never translation hashes.
Claim fingerprints describe identical symbolic inputs, not semantic equivalence
or evidence quality. Callers must authorize curated catalogs and source references.

Primary references:
https://www.w3.org/TR/skos-reference/#labels
https://www.rfc-editor.org/rfc/rfc5646.html
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Sequence
from datetime import date, datetime
from typing import Annotated, Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$"
_SCOPE = r"^(general|project:[A-Za-z0-9][A-Za-z0-9_.:-]{0,199})$"
_CONCEPT_ID = r"^urn:swarmer:concept:[a-f0-9]{32}$"
_GRANDFATHERED = {
    tag.lower(): tag
    for tag in (
        "en-GB-oed",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "sgn-BE-FR",
        "sgn-BE-NL",
        "sgn-CH-DE",
        "art-lojban",
        "cel-gaulish",
        "no-bok",
        "no-nyn",
        "zh-guoyu",
        "zh-hakka",
        "zh-min",
        "zh-min-nan",
        "zh-xiang",
    )
}
MAX_JSON_BYTES = 8_000
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 512


def source_bytes_sha256(value: bytes) -> str:
    """Hash the exact original bytes, never NFC/translation/case-normalized text."""
    if type(value) is not bytes:
        raise TypeError("source bytes required")
    return hashlib.sha256(value).hexdigest()


def new_concept_id() -> str:
    """Allocate once and persist; changing labels never regenerates this ID."""
    return f"urn:swarmer:concept:{uuid4().hex}"


def normalize_language_tag(tag: str) -> str:
    """Bounded, well-formed BCP47 syntax and conventional casing, no registry aliases."""
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", tag):
        raise ValueError("invalid language tag")
    lower = tag.lower()
    if lower in _GRANDFATHERED:
        return _GRANDFATHERED[lower]
    parts = lower.split("-")
    if any(not 1 <= len(part) <= 8 for part in parts):
        raise ValueError("invalid language tag")
    if parts[0] == "x":
        if len(parts) == 1:
            raise ValueError("invalid language tag")
        return lower
    language = parts[0]
    if not language.isalpha() or not 2 <= len(language) <= 8:
        raise ValueError("invalid language tag")
    index = 1
    normalized = [language]
    if (
        len(language) <= 3
        and index < len(parts)
        and len(parts[index]) == 3
        and parts[index].isalpha()
    ):
        normalized.append(parts[index])
        index += 1
        # Further extlangs are permanently reserved by RFC 5646 section 2.2.2.
    if index < len(parts) and len(parts[index]) == 4 and parts[index].isalpha():
        normalized.append(parts[index].title())
        index += 1
    if index < len(parts) and (
        (len(parts[index]) == 2 and parts[index].isalpha())
        or (len(parts[index]) == 3 and parts[index].isdigit())
    ):
        normalized.append(parts[index].upper())
        index += 1
    variants: set[str] = set()
    while index < len(parts) and (
        5 <= len(parts[index]) <= 8 or (len(parts[index]) == 4 and parts[index][0].isdigit())
    ):
        if parts[index] in variants:
            raise ValueError("duplicate language variant")
        variants.add(parts[index])
        normalized.append(parts[index])
        index += 1
    extensions: set[str] = set()
    extension_blocks: list[list[str]] = []
    while index < len(parts) and len(parts[index]) == 1 and parts[index] != "x":
        singleton = parts[index]
        if singleton in extensions:
            raise ValueError("duplicate language extension")
        extensions.add(singleton)
        block = [singleton]
        index += 1
        start = index
        while index < len(parts) and 2 <= len(parts[index]) <= 8:
            block.append(parts[index])
            index += 1
        if index == start:
            raise ValueError("empty language extension")
        extension_blocks.append(block)
    for block in sorted(extension_blocks, key=lambda value: value[0]):
        normalized.extend(block)
    if index < len(parts) and parts[index] == "x" and index + 1 < len(parts):
        normalized.extend(parts[index:])
        index = len(parts)
    if index != len(parts):
        raise ValueError("invalid language tag")
    return "-".join(normalized)


def normalized_json(value: Any) -> str:
    """Deterministic JSON keys; retain arrays, literal case, number types and Unicode."""
    count = 0

    def check(item: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if depth > MAX_JSON_DEPTH or count > MAX_JSON_NODES:
            raise ValueError("claim JSON nesting or size exceeded")
        if type(item) is dict:
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 200:
                    raise ValueError("invalid claim JSON key")
                key.encode("utf-8")
                check(child, depth + 1)
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
        elif type(item) is str:
            item.encode("utf-8")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("nonfinite claim JSON value")
        elif item is not None and type(item) not in {bool, int}:
            raise ValueError("non JSON claim value")

    try:
        check(value, 0)
        result = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        if len(result.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("claim JSON byte budget exceeded")
        return result
    except UnicodeError as exc:
        raise ValueError("invalid claim JSON encoding") from exc


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LanguageAnnotation(_Contract):
    tag: str
    origin: Literal["declared", "source_metadata", "detected"]

    @field_validator("tag")
    @classmethod
    def normalized_tag(cls, tag: str) -> str:
        return normalize_language_tag(tag)

    @model_validator(mode="after")
    def bounded_detection(self) -> Self:
        if self.origin == "detected" and not re.fullmatch(r"[a-z]{2,8}", self.tag):
            raise ValueError("detected language cannot assert a region or variant")
        return self


class ConceptLabel(_Contract):
    text: str = Field(min_length=1, max_length=400)
    language: LanguageAnnotation
    role: Literal["pref", "alt", "hidden"] = "pref"

    @field_validator("text")
    @classmethod
    def normalized_label(cls, text: str) -> str:
        if not text.strip() or not text.isprintable():
            raise ValueError("invalid concept label")
        text.encode("utf-8")
        return unicodedata.normalize("NFC", text)


class ConceptDefinition(_Contract):
    concept_id: str = Field(pattern=_CONCEPT_ID)
    scope: str = Field(pattern=_SCOPE)
    namespace: str = Field(pattern=_IDENTIFIER)
    scheme_id: str = Field(pattern=_IDENTIFIER)
    labels: list[ConceptLabel] = Field(min_length=1, max_length=64)
    curation_status: Literal["proposed", "curated"] = "proposed"
    grants_authority: Literal[False] = False

    @model_validator(mode="after")
    def disjoint_labels(self) -> Self:
        preferred: set[str] = set()
        terms: set[tuple[str, str]] = set()
        for label in self.labels:
            key = (label.language.tag.lower(), label.text)
            if key in terms:
                raise ValueError("duplicate or conflicting concept label roles")
            terms.add(key)
            if label.role == "pref":
                if key[0] in preferred:
                    raise ValueError("multiple preferred labels for one language")
                preferred.add(key[0])
        return self


class ConceptCandidate(_Contract):
    concept_id: str
    scope: str
    namespace: str
    scheme_id: str
    matched_roles: list[Literal["pref", "alt", "hidden"]]


class ConceptResolution(_Contract):
    status: Literal["unmatched", "candidate", "ambiguous"]
    candidates: list[ConceptCandidate]
    grants_authority: Literal[False] = False


def resolve_curated_label(
    text: str,
    language: str,
    *,
    scope: str,
    namespace: str,
    scheme_id: str,
    concepts: Sequence[ConceptDefinition],
) -> ConceptResolution:
    """Exact NFC label lookup only; return all curated same-scope candidates."""
    if len(concepts) > 1_000:
        raise ValueError("concept catalog budget exceeded")
    label = ConceptLabel(text=text, language=LanguageAnnotation(tag=language, origin="declared"))
    matches: list[ConceptCandidate] = []
    ids: set[str] = set()
    for original in concepts:
        concept = ConceptDefinition.model_validate(original.model_dump())
        if (concept.scope, concept.namespace, concept.scheme_id) != (scope, namespace, scheme_id):
            continue
        if concept.curation_status != "curated":
            continue
        if concept.concept_id in ids:
            raise ValueError("duplicate scoped concept identity")
        ids.add(concept.concept_id)
        roles = [
            candidate.role
            for candidate in concept.labels
            if candidate.text == label.text
            and candidate.language.tag.lower() == label.language.tag.lower()
        ]
        if roles:
            matches.append(
                ConceptCandidate(
                    concept_id=concept.concept_id,
                    scope=scope,
                    namespace=namespace,
                    scheme_id=scheme_id,
                    matched_roles=roles,
                )
            )
    matches.sort(key=lambda value: value.concept_id)
    return ConceptResolution(
        status="unmatched" if not matches else "candidate" if len(matches) == 1 else "ambiguous",
        candidates=matches,
    )


class IdentityTerm(_Contract):
    type: Literal["identity"] = "identity"
    namespace: str = Field(pattern=_IDENTIFIER)
    identity: str = Field(min_length=1, max_length=1_000)

    @field_validator("identity")
    @classmethod
    def exact_identity(cls, value: str) -> str:
        if not value.strip() or not value.isprintable():
            raise ValueError("invalid symbolic identity")
        value.encode("utf-8")
        return value


class TypedLiteral(_Contract):
    type: Literal["literal"] = "literal"
    datatype: Literal[
        "text",
        "code",
        "path",
        "url",
        "symbol",
        "integer",
        "decimal",
        "boolean",
        "date",
        "datetime",
        "version",
        "quantity",
    ]
    lexical_value: str = Field(min_length=1, max_length=4_000)
    language: LanguageAnnotation | None = None
    unit: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_lexical_form(self) -> Self:
        self.lexical_value.encode("utf-8")
        if not self.lexical_value.strip():
            raise ValueError("empty typed literal")
        if (self.datatype == "text") != (self.language is not None):
            raise ValueError("only text literals require a language annotation")
        if (self.datatype == "quantity") != (self.unit is not None):
            raise ValueError("only quantities require a unit")
        if self.unit is not None and (not self.unit.isprintable() or not self.unit.strip()):
            raise ValueError("invalid literal unit")
        value = self.lexical_value
        if self.datatype == "integer" and not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
            raise ValueError("invalid integer literal")
        if self.datatype in {"decimal", "quantity"} and not re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value
        ):
            raise ValueError("invalid decimal literal")
        if self.datatype == "boolean" and value not in {"true", "false"}:
            raise ValueError("invalid boolean literal")
        if self.datatype == "date":
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError("invalid date literal")
            date.fromisoformat(value)
        if self.datatype == "datetime":
            if not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})",
                value,
            ):
                raise ValueError("invalid datetime literal")
            datetime.fromisoformat(value)
        return self


ClaimTerm = Annotated[IdentityTerm | TypedLiteral, Field(discriminator="type")]


class EffectiveCondition(_Contract):
    relation: Literal[
        "before", "after", "only_after", "until", "while", "unless", "if", "only_if", "at"
    ]
    argument: ClaimTerm


class SymbolicClaim(_Contract):
    scope: str = Field(pattern=_SCOPE)
    namespace: str = Field(pattern=_IDENTIFIER)
    scheme_id: str = Field(pattern=_IDENTIFIER)
    kind: str = Field(pattern=_IDENTIFIER)
    subject: IdentityTerm
    predicate: IdentityTerm
    object: ClaimTerm
    polarity: Literal["affirmed", "negated"]
    modality: Literal[
        "asserted", "possible", "permitted", "required", "forbidden", "preferred", "unknown"
    ]
    applicability: dict[str, Any] = Field(default_factory=dict)
    version: str | None = Field(default=None, min_length=1, max_length=200)
    effective_conditions: list[EffectiveCondition] = Field(default_factory=list, max_length=32)

    @field_validator("applicability")
    @classmethod
    def bounded_applicability(cls, value: dict[str, Any]) -> dict[str, Any]:
        return dict(json.loads(normalized_json(value)))

    @field_validator("version")
    @classmethod
    def exact_version(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or not value.isprintable()):
            raise ValueError("invalid claim version")
        return value


def claim_fingerprint(claim: SymbolicClaim) -> str:
    """A comparison key, never a permission to merge records or discard sources."""
    snapshot = SymbolicClaim.model_validate(claim.model_dump())
    body = {"policy": "symbolic-claim-v1", "claim": snapshot.model_dump()}
    return hashlib.sha256(normalized_json(body).encode("utf-8")).hexdigest()


class EvidenceReference(_Contract):
    scope: str = Field(pattern=_SCOPE)
    source_id: str = Field(pattern=_IDENTIFIER)
    source_version: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    location: str | None = Field(default=None, min_length=1, max_length=500)
    relationship: Literal["supports", "contradicts", "context"]
    origin: Literal["user_statement", "assistant_claim", "tool_result", "source_document"]
    validation_status: Literal["unvalidated"] = "unvalidated"

    @field_validator("source_version", "location")
    @classmethod
    def bounded_source_metadata(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or not value.isprintable()):
            raise ValueError("invalid source reference metadata")
        return value


class ClaimProposal(_Contract):
    proposal_id: str = Field(pattern=_IDENTIFIER)
    claim: SymbolicClaim
    sources: list[EvidenceReference] = Field(min_length=1, max_length=32)
    validation_status: Literal["unvalidated"] = "unvalidated"
    grants_authority: Literal[False] = False

    @model_validator(mode="after")
    def scoped_sources(self) -> Self:
        if any(source.scope != self.claim.scope for source in self.sources):
            raise ValueError("claim source scope mismatch")
        return self
