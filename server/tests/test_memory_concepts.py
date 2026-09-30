from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from app.services.memory_concepts import (
    ClaimProposal,
    ConceptDefinition,
    ConceptLabel,
    EffectiveCondition,
    EvidenceReference,
    IdentityTerm,
    LanguageAnnotation,
    SymbolicClaim,
    TypedLiteral,
    claim_fingerprint,
    new_concept_id,
    normalize_language_tag,
    normalized_json,
    resolve_curated_label,
    source_bytes_sha256,
)


def label(text: str, tag: str = "en", role: str = "pref", origin: str = "declared") -> ConceptLabel:
    return ConceptLabel(text=text, language=LanguageAnnotation(tag=tag, origin=origin), role=role)


def concept(text: str = "branch", **changes: Any) -> ConceptDefinition:
    return ConceptDefinition(
        **{
            "concept_id": new_concept_id(),
            "scope": "project:alpha",
            "namespace": "software",
            "scheme_id": "programming",
            "labels": [label(text)],
            "curation_status": "curated",
            **changes,
        }
    )


def identity(value: str) -> IdentityTerm:
    return IdentityTerm(namespace="software", identity=value)


def claim(**changes: Any) -> SymbolicClaim:
    return SymbolicClaim(
        **{
            "scope": "project:alpha",
            "namespace": "software",
            "scheme_id": "programming",
            "kind": "constraint",
            "subject": identity("deployment"),
            "predicate": identity("requires"),
            "object": identity("tests.completed"),
            "polarity": "affirmed",
            "modality": "required",
            "effective_conditions": [
                EffectiveCondition(relation="after", argument=identity("approval"))
            ],
            **changes,
        }
    )


@pytest.mark.parametrize(
    "original,expected",
    [
        ("fr-CA", "fr-CA"),
        ("FR-ca", "fr-CA"),
        ("fr", "fr"),
        ("en", "en"),
        ("zh-cmn-hans-cn", "zh-cmn-Hans-CN"),
        ("sr-latn-rs", "sr-Latn-RS"),
        ("de-CH-1901", "de-CH-1901"),
        ("en-US-u-ca-gregory", "en-US-u-ca-gregory"),
        ("en-x-PRIVATE", "en-x-private"),
        ("x-ABC", "x-abc"),
        ("i-KLINGON", "i-klingon"),
    ],
)
def test_language_tag_syntax_preserves_declared_precision(original: str, expected: str) -> None:
    assert normalize_language_tag(original) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "fr_CA",
        "fr--CA",
        "fr-",
        "x",
        "e",
        "fr-é",
        "en-u",
        "en-u-ca-u-nu",
        "de-1901-1901",
        "zh-cmn-yue",
        "fr CA",
        "../fr",
    ],
)
def test_malformed_duplicate_or_reserved_language_tags_are_rejected(tag: str) -> None:
    with pytest.raises(ValueError):
        normalize_language_tag(tag)


def test_detected_french_does_not_invent_a_region() -> None:
    detected = LanguageAnnotation(tag="fr", origin="detected")
    declared = LanguageAnnotation(tag="fr-CA", origin="declared")
    assert detected.tag == "fr" and declared.tag == "fr-CA"
    with pytest.raises(ValidationError, match="cannot assert a region"):
        LanguageAnnotation(tag="fr-CA", origin="detected")


def test_language_extension_order_does_not_create_two_preferred_labels() -> None:
    assert normalize_language_tag("en-b-ccc-a-ddd-x-private") == "en-a-ddd-b-ccc-x-private"
    with pytest.raises(ValidationError, match="multiple preferred"):
        concept(labels=[label("one", "en-b-ccc-a-ddd"), label("two", "en-a-ddd-b-ccc")])


def test_source_bytes_hash_is_exact_while_labels_use_nfc() -> None:
    decomposed, composed = "re\u0301seau", "réseau"
    assert label(decomposed, "fr").text == label(composed, "fr").text
    assert source_bytes_sha256(decomposed.encode()) != source_bytes_sha256(composed.encode())
    with pytest.raises(TypeError):
        source_bytes_sha256(composed)  # type: ignore[arg-type]


def test_concept_identity_survives_translation_and_label_corrections() -> None:
    original = concept()
    edited = original.model_copy(
        update={"labels": [label("branche", "fr-CA"), label("branch", "en")]}
    )
    assert ConceptDefinition.model_validate(edited.model_dump()).concept_id == original.concept_id
    assert concept().concept_id != original.concept_id
    with pytest.raises(ValidationError):
        concept(concept_id="branch")


def test_preferred_label_limit_and_disjoint_roles_are_checked_per_language() -> None:
    concept(labels=[label("branch"), label("branche", "fr"), label("branche", "fr-CA")])
    with pytest.raises(ValidationError, match="multiple preferred"):
        concept(labels=[label("branch", "en-US"), label("branching", "EN-us")])
    with pytest.raises(ValidationError, match="conflicting concept label"):
        concept(labels=[label("branch"), label("branch", role="alt")])
    concept(labels=[label("branch"), label("fork", role="alt"), label("brach", role="hidden")])


def test_ambiguous_curated_branch_keeps_git_and_control_flow_candidates() -> None:
    git = concept(labels=[label("Git branch"), label("branch", role="alt")])
    flow = concept(labels=[label("control-flow branch"), label("branch", role="alt")])
    result = resolve_curated_label(
        "branch",
        "en",
        scope="project:alpha",
        namespace="software",
        scheme_id="programming",
        concepts=[git, flow],
    )
    assert result.status == "ambiguous" and not result.grants_authority
    assert {candidate.concept_id for candidate in result.candidates} == {
        git.concept_id,
        flow.concept_id,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "project:beta"},
        {"scope": "general"},
        {"namespace": "biology"},
        {"scheme_id": "botany"},
        {"curation_status": "proposed"},
    ],
)
def test_resolver_does_not_cross_scope_namespace_scheme_or_promote_proposals(change: dict) -> None:
    result = resolve_curated_label(
        "branch",
        "en",
        scope="project:alpha",
        namespace="software",
        scheme_id="programming",
        concepts=[concept(**change)],
    )
    assert result.status == "unmatched" and not result.candidates


def test_resolver_uses_declared_language_exactly_without_regional_fallback() -> None:
    french = concept(labels=[label("branche", "fr-CA")])
    assert (
        resolve_curated_label(
            "branche",
            "fr",
            scope="project:alpha",
            namespace="software",
            scheme_id="programming",
            concepts=[french],
        ).status
        == "unmatched"
    )
    assert (
        resolve_curated_label(
            "branche",
            "FR-ca",
            scope="project:alpha",
            namespace="software",
            scheme_id="programming",
            concepts=[french],
        ).status
        == "candidate"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": "project:beta"},
        {"namespace": "deployment"},
        {"scheme_id": "other"},
        {"kind": "preference"},
        {"subject": identity("different-deployment")},
        {"predicate": identity("suggests")},
        {"object": identity("tests.failed")},
        {"polarity": "negated"},
        {"modality": "possible"},
        {"modality": "permitted"},
        {"version": "v2"},
        {"applicability": {"environment": "production"}},
    ],
)
def test_symbolic_fingerprint_includes_all_meaning_boundaries(changes: dict) -> None:
    assert claim_fingerprint(claim()) != claim_fingerprint(claim(**changes))


def test_before_after_only_after_are_distinct_without_translation_inference() -> None:
    fingerprints = {
        claim_fingerprint(
            claim(
                effective_conditions=[
                    EffectiveCondition(relation=relation, argument=identity("approval"))
                ]
            )
        )
        for relation in ("before", "after", "only_after")
    }
    assert len(fingerprints) == 3
    # Merely saying "only after" in a pivot adds a restriction. This library
    # never extracts either phrase or claims that they are equivalent.


def test_units_versions_code_and_path_case_are_never_folded() -> None:
    kilogram = TypedLiteral(datatype="quantity", lexical_value="5", unit="kg")
    gram = TypedLiteral(datatype="quantity", lexical_value="5", unit="g")
    assert claim_fingerprint(claim(object=kilogram)) != claim_fingerprint(claim(object=gram))
    for datatype, before, after in [
        ("code", "saveRecord()", "saverecord()"),
        ("path", "src/Readme.md", "src/readme.md"),
        ("version", "v1.2.3", "v1.2.4"),
        ("symbol", "HTTPClient", "HttpClient"),
    ]:
        assert claim_fingerprint(
            claim(object=TypedLiteral(datatype=datatype, lexical_value=before))
        ) != claim_fingerprint(claim(object=TypedLiteral(datatype=datatype, lexical_value=after)))


def test_language_independent_concept_ids_give_same_claim_without_label_hashes() -> None:
    entity = concept()
    english = claim(object=IdentityTerm(namespace=entity.namespace, identity=entity.concept_id))
    french = claim(object=IdentityTerm(namespace=entity.namespace, identity=entity.concept_id))
    assert claim_fingerprint(english) == claim_fingerprint(french)


def test_applicability_key_order_is_normalized_but_arrays_and_types_are_not() -> None:
    assert claim_fingerprint(
        claim(applicability={"a": 1, "b": {"x": "value"}})
    ) == claim_fingerprint(claim(applicability={"b": {"x": "value"}, "a": 1}))
    assert normalized_json({"n": True}) != normalized_json({"n": 1})
    assert normalized_json({"n": 1.0}) != normalized_json({"n": 1})
    assert normalized_json({"order": ["a", "b"]}) != normalized_json({"order": ["b", "a"]})


@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf, b"bytes", (1, 2), {1: "bad key"}, {"s": "\ud800"}, {"a": "x" * 8_001}],
)
def test_invalid_or_over_budget_structured_conditions_are_rejected(value: Any) -> None:
    with pytest.raises(ValueError):
        normalized_json(value)


@pytest.mark.parametrize(
    "literal",
    [
        {"datatype": "integer", "lexical_value": "one"},
        {"datatype": "boolean", "lexical_value": "yes"},
        {"datatype": "date", "lexical_value": "2026-02-30"},
        {"datatype": "quantity", "lexical_value": "5"},
        {"datatype": "decimal", "lexical_value": "NaN"},
        {"datatype": "text", "lexical_value": "hello"},
    ],
)
def test_typed_literal_constraints_reject_implicit_interpretation(literal: dict) -> None:
    with pytest.raises(ValidationError):
        TypedLiteral(**literal)


def test_provenance_stays_unvalidated_and_cannot_promote_claims_or_cross_projects() -> None:
    evidence = EvidenceReference(
        scope="project:alpha",
        source_id="message_1",
        source_version="1",
        source_sha256=source_bytes_sha256(b"May deploy after approval."),
        relationship="supports",
        origin="assistant_claim",
    )
    proposal = ClaimProposal(
        proposal_id="proposal_1", claim=claim(modality="possible"), sources=[evidence]
    )
    assert proposal.validation_status == "unvalidated" and not proposal.grants_authority
    assert proposal.sources[0].validation_status == "unvalidated"
    with pytest.raises(ValidationError):
        ClaimProposal(**{**proposal.model_dump(), "validation_status": "verified"})
    with pytest.raises(ValidationError, match="source scope mismatch"):
        ClaimProposal(
            proposal_id="proposal_2", claim=claim(scope="project:beta"), sources=[evidence]
        )


def test_similarity_and_free_text_equivalence_are_not_accepted_inputs() -> None:
    with pytest.raises(ValidationError):
        SymbolicClaim(**{**claim().model_dump(), "vector_similarity": 0.99})
    with pytest.raises(ValidationError):
        ConceptDefinition(
            **{**concept().model_dump(), "automatically_equivalent_to": new_concept_id()}
        )
