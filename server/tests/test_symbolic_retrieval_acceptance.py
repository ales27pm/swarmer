"""Public symbolic discovery with real SQLite, paired HTTP, and no model providers."""

import socket
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.settings import Settings

SCOPE = "project:symbolic-acceptance"
CATALOG = {"namespace": "software", "scheme_id": "engineering"}
LABELS = (
    ("horloge silencieuse", "fr-CA"),
    ("silent clock", "en"),
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch):
    attempts: list[bool] = []

    def forbidden(*args: Any, **kwargs: Any) -> None:
        attempts.append(True)
        raise AssertionError("network forbidden in symbolic acceptance tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield
    assert not attempts


@pytest.fixture
def test_app(tmp_path: Path) -> FastAPI:
    app = create_app(
        Settings(
            _env_file=None,
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
            permissions_path=Path(__file__).resolve().parents[2] / "configs/permissions.yaml",
            pairing_bootstrap_token=SecretStr("test-operator-token-with-sufficient-entropy"),
            pairing_code_ttl_seconds=600,
            pairing_max_attempts=3,
            embedding_base_url=None,
            embedding_model=None,
            project_embedding_base_url=None,
            project_embedding_model=None,
            memory_canonical_language="legacy",
            memory_normalization_base_url=None,
            memory_translator_model=None,
            memory_reviewer_model=None,
        )
    )
    service = app.state.state_service
    assert service.embedding_service is None
    assert service.memory_normalizer is None
    assert service.memory_presenter is None
    return app


def _source(
    client: TestClient,
    headers: dict[str, str],
    content: str,
    *,
    scope: str = SCOPE,
    kind: str = "fact",
    pinned: bool = False,
) -> tuple[str, dict[str, Any]]:
    response = client.post(
        "/memory",
        headers=headers,
        json={"scope": scope, "kind": kind, "content": content, "pinned": pinned},
    )
    assert response.status_code == 201, response.text
    memory_id = response.json()["id"]
    described = client.get(
        f"/memory/{memory_id}/symbolic-source", headers=headers, params={"scope": scope}
    )
    assert described.status_code == 200, described.text
    binding = next(item for item in described.json()["bindings"] if item["field"] == "content")
    assert binding["memory_id"] == memory_id
    return memory_id, binding


def _concept(
    client: TestClient,
    headers: dict[str, str],
    *,
    scope: str = SCOPE,
    catalog: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/memory/concepts",
        headers=headers,
        json={
            "scope": scope,
            **(CATALOG if catalog is None else catalog),
            "labels": [
                {"text": text, "language": {"tag": tag, "origin": "declared"}, "role": "pref"}
                for text, tag in LABELS
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["curation_status"] == "proposed"
    return response.json()


def _identity(value: str, namespace: str = "software") -> dict[str, str]:
    return {"type": "identity", "namespace": namespace, "identity": value}


def _claim(concept: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": concept["scope"],
        "namespace": concept["namespace"],
        "scheme_id": concept["scheme_id"],
        "kind": "constraint",
        "subject": _identity(concept["concept_id"], concept["namespace"]),
        "predicate": _identity("preserve", concept["namespace"]),
        "object": _identity("Archive.py", concept["namespace"]),
        "polarity": "affirmed",
        "modality": "required",
        "effective_conditions": [{"relation": "only_after", "argument": _identity("commit")}],
    }


def _proposal(
    client: TestClient,
    headers: dict[str, str],
    claim: dict[str, Any],
    *bindings: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        "/memory/proposals", headers=headers, json={"claim": claim, "sources": list(bindings)}
    )
    assert response.status_code == 201, response.text
    assert response.json()["validation_status"] == "unvalidated"
    assert response.json()["grants_authority"] is False
    return response.json()


def _search(
    client: TestClient,
    headers: dict[str, str],
    query: str = "silent clock",
    *,
    catalogs: list[dict[str, str]] | None = None,
    **options: Any,
) -> list[dict[str, Any]]:
    response = client.post(
        "/memory/search",
        headers=headers,
        json={
            "query": query,
            "scope": SCOPE,
            "symbolic": {"catalogs": [CATALOG] if catalogs is None else catalogs},
            **options,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _evidence(row: dict[str, Any]) -> list[dict[str, Any]]:
    assert row["search_kind"] == "symbolic"
    assert row["symbolic_status"] == "available"
    assert row["ranking_algorithm"] == "symbolic-v1"
    evidence = row["symbolic_evidence"]
    assert evidence, "symbolic discovery must expose attributable unvalidated evidence"
    for item in evidence:
        assert item["schema_version"] == "symbolic-evidence-v1"
        assert item["validation_status"] == "unvalidated"
        assert item["grants_authority"] is False
        assert item["proposal"]["validation_status"] == "unvalidated"
        assert item["proposal"]["grants_authority"] is False
        assert len(item["read_token"]) == 64
    return evidence


@pytest.mark.parametrize("query,language", LABELS, ids=["french", "english"])
def test_public_label_only_discovery_requires_opt_in_and_returns_exact_evidence(
    client: TestClient, paired_headers: dict[str, str], query: str, language: str
) -> None:
    content = "Dossier Z91 : préserver Archive.py après validation."
    assert all(term.casefold() not in content.casefold() for term in query.split())
    memory_id, binding = _source(client, paired_headers, content)
    concept = _concept(client, paired_headers)
    proposal = _proposal(client, paired_headers, _claim(concept), binding)

    legacy = client.post(
        "/memory/search", headers=paired_headers, json={"query": query, "scope": SCOPE}
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json() == [], "catalog discovery must remain explicitly opt-in"
    rows = _search(client, paired_headers, query)
    assert [row["id"] for row in rows] == [memory_id]
    assert rows[0]["content"] == content
    evidence = _evidence(rows[0])
    assert len(evidence) == 1
    assert evidence[0]["catalog"] == CATALOG
    assert evidence[0]["proposal"] == proposal
    assert evidence[0]["concepts"] == [concept]
    assert proposal["sources"][0]["binding"] == binding
    assert any(
        match["channel"] == "concept_label"
        and match["concept_id"] == concept["concept_id"]
        and match["value"] == query
        and match["language"] == language
        for match in evidence[0]["matches"]
    )


def test_same_claim_preserves_observations_then_revokes_edited_and_forgotten_sources(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    concept = _concept(client, paired_headers)
    claim = _claim(concept)
    first_id, first_binding = _source(client, paired_headers, "Document Z91 : conserver les dates.")
    second_id, second_binding = _source(
        client, paired_headers, "Document Y72 : conserver les noms."
    )
    first = _proposal(client, paired_headers, claim, first_binding)
    second = _proposal(client, paired_headers, claim, second_binding)
    assert first["proposal_id"] != second["proposal_id"]
    assert first["claim_sha256"] == second["claim_sha256"]
    foreign_scope = "project:beta"
    foreign_id, foreign_binding = _source(
        client, paired_headers, "Document W63 : conserver les noms.", scope=foreign_scope
    )
    foreign = _proposal(
        client,
        paired_headers,
        _claim(_concept(client, paired_headers, scope=foreign_scope)),
        foreign_binding,
    )

    rows = _search(client, paired_headers)
    assert {row["id"] for row in rows} == {first_id, second_id}
    assert len(rows) == 2
    assert {item["proposal"]["proposal_id"] for row in rows for item in _evidence(row)} == {
        first["proposal_id"],
        second["proposal_id"],
    }
    assert foreign_id not in str(rows) and foreign["proposal_id"] not in str(rows)

    edited = client.patch(
        f"/memory/{first_id}", headers=paired_headers, json={"content": "Correction : garder zéro."}
    )
    assert edited.status_code == 200, edited.text
    remaining = _search(client, paired_headers)
    assert [row["id"] for row in remaining] == [second_id]
    assert _evidence(remaining[0])[0]["proposal"] == second
    assert client.delete(f"/memory/{second_id}", headers=paired_headers).status_code == 204
    assert _search(client, paired_headers) == []
    assert (
        client.get(
            f"/memory/{second_id}/proposals", headers=paired_headers, params={"scope": SCOPE}
        ).status_code
        == 404
    )
    stale = client.get(
        f"/memory/{first_id}/proposals", headers=paired_headers, params={"scope": SCOPE}
    ).json()
    assert stale["proposals"] == []
    assert stale["unavailable"] == [{"proposal_id": first["proposal_id"], "reason": "stale_source"}]


@pytest.mark.parametrize("excluded_by", ["scope", "namespace", "scheme", "kind"])
def test_authorized_catalog_scope_and_source_kind_are_filtered_before_limit(
    client: TestClient, paired_headers: dict[str, str], excluded_by: str
) -> None:
    good_concept = _concept(client, paired_headers)
    bad_scope = "project:beta" if excluded_by == "scope" else SCOPE
    bad_catalog = {
        "namespace": "other" if excluded_by == "namespace" else CATALOG["namespace"],
        "scheme_id": "other" if excluded_by == "scheme" else CATALOG["scheme_id"],
    }
    bad_concept = _concept(client, paired_headers, scope=bad_scope, catalog=bad_catalog)
    bad_ids = []
    valid_id = None
    # Invalid high-priority observations bracket the valid one in creation order.
    for position in range(3):
        bad = position != 1
        memory_id, binding = _source(
            client,
            paired_headers,
            f"Dossier numéro {position} : préserver les identifiants.",
            scope=bad_scope if bad else SCOPE,
            kind="lesson" if bad and excluded_by == "kind" else "fact",
            pinned=bad,
        )
        _proposal(client, paired_headers, _claim(bad_concept if bad else good_concept), binding)
        if bad:
            bad_ids.append(memory_id)
        else:
            valid_id = memory_id
    rows = _search(client, paired_headers, kind="fact", limit=1)
    assert [row["id"] for row in rows] == [valid_id]
    assert all(memory_id not in str(rows) for memory_id in bad_ids)
    assert _evidence(rows[0])[0]["catalog"] == CATALOG


def test_hidden_companion_source_disqualifies_whole_proposal_before_limit(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI
) -> None:
    concept = _concept(client, paired_headers)
    claim = _claim(concept)
    hidden_ids: list[str] = []
    excluded_ids: list[str] = []
    excluded_proposals: list[str] = []
    valid_id = None
    for position in range(3):
        memory_id, binding = _source(
            client,
            paired_headers,
            f"Note publique {position} : documenter les étapes.",
            pinned=True,
        )
        if position == 1:
            valid_id = memory_id
            _proposal(client, paired_headers, claim, binding)
        else:
            companion_id, companion = _source(
                client, paired_headers, f"Pièce jointe confidentielle numéro {position}."
            )
            proposal = _proposal(client, paired_headers, claim, binding, companion)
            excluded_ids.append(memory_id)
            hidden_ids.append(companion_id)
            excluded_proposals.append(proposal["proposal_id"])
    # Sensitivity has no public update route. Change only the disposable source
    # rows to represent access revoked after a valid public proposal was created.
    with sqlite3.connect(test_app.state.state_service.db_path) as db:
        db.executemany(
            "UPDATE memory_items SET sensitivity='secret' WHERE id=?",
            [(mid,) for mid in hidden_ids],
        )
    rows = _search(client, paired_headers, limit=1)
    assert [row["id"] for row in rows] == [valid_id]
    assert all(value not in str(rows) for value in hidden_ids + excluded_ids + excluded_proposals)
    assert "confidentielle" not in str(rows)
    assert len(_evidence(rows[0])) == 1


def test_conditions_negation_units_versions_and_contradictions_survive_public_search_whole(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    memory_id, binding = _source(
        client, paired_headers, "Spécification Z91 : conserver la structure."
    )
    concept = _concept(client, paired_headers)
    claim = _claim(concept)
    condition = "Conserver exactement cette condition longue. " * 16 + "FIN-CONDITION"
    claim.update(
        polarity="negated",
        modality="forbidden",
        object={
            "type": "literal",
            "datatype": "quantity",
            "lexical_value": "1.2500e+03",
            "unit": "MiB",
        },
        version="v01.2+RC",
        applicability={"enabled": False, "attempts": 0, "fraction": 1.25, "versions": ["01", "1"]},
        effective_conditions=[
            {
                "relation": "only_after",
                "argument": {
                    "type": "literal",
                    "datatype": "path",
                    "lexical_value": "Cache/State.py",
                },
            },
            {
                "relation": "unless",
                "argument": {"type": "literal", "datatype": "boolean", "lexical_value": "false"},
            },
            {
                "relation": "if",
                "argument": {
                    "type": "literal",
                    "datatype": "text",
                    "lexical_value": condition,
                    "language": {"tag": "fr-CA", "origin": "declared"},
                },
            },
        ],
    )
    negative = _proposal(client, paired_headers, claim, binding)
    assert negative["claim"]["applicability"] == claim["applicability"]
    assert type(negative["claim"]["applicability"]["enabled"]) is bool
    assert type(negative["claim"]["applicability"]["attempts"]) is int
    assert type(negative["claim"]["applicability"]["fraction"]) is float
    opposite = deepcopy(claim)
    opposite["polarity"] = "affirmed"
    affirmative = _proposal(client, paired_headers, opposite, binding)
    relation = client.post(
        f"/memory/proposals/{negative['proposal_id']}/relations",
        headers=paired_headers,
        json={"target_proposal_id": affirmative["proposal_id"], "relationship": "contradicts"},
    )
    assert relation.status_code == 201, relation.text
    rows = _search(client, paired_headers, limit=1)
    assert [row["id"] for row in rows] == [memory_id], (
        "one row per memory, distinct claims retained"
    )
    evidence = _evidence(rows[0])
    assert {item["proposal"]["proposal_id"] for item in evidence} == {
        negative["proposal_id"],
        affirmative["proposal_id"],
    }
    by_id = {item["proposal"]["proposal_id"]: item for item in evidence}
    assert by_id[negative["proposal_id"]]["proposal"] == negative
    assert by_id[affirmative["proposal_id"]]["proposal"] == affirmative
    assert negative["claim_sha256"] != affirmative["claim_sha256"]
    assert (
        by_id[negative["proposal_id"]]["proposal"]["claim"]["effective_conditions"][2]["argument"][
            "lexical_value"
        ]
        == condition
    )
    assert relation.json() in by_id[negative["proposal_id"]]["relations"]


@pytest.mark.parametrize(
    "selection",
    [
        {"catalogs": []},
        {"catalogs": [CATALOG, CATALOG]},
        {"catalogs": [{**CATALOG, "scope": SCOPE}]},
        {"catalogs": [{"namespace": "software", "scheme_id": "*"}]},
        {"catalogs": [{"namespace": "software", "scheme_id": str(n)} for n in range(9)]},
    ],
)
def test_invalid_catalog_selection_is_public_422(
    client: TestClient, paired_headers: dict[str, str], selection: dict[str, Any]
) -> None:
    response = client.post(
        "/memory/search",
        headers=paired_headers,
        json={"query": "silent clock", "scope": SCOPE, "symbolic": selection},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("scope", [None, "global", "arbitrary"])
def test_symbolic_search_requires_explicit_valid_public_scope(
    client: TestClient, paired_headers: dict[str, str], scope: str | None
) -> None:
    response = client.post(
        "/memory/search",
        headers=paired_headers,
        json={"query": "silent clock", "scope": scope, "symbolic": {"catalogs": [CATALOG]}},
    )
    assert response.status_code == 422, response.text


def test_public_match_budget_reports_explicit_error_without_truncating_valid_matches(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    memory_id, binding = _source(client, paired_headers, "Document Z91 : garder les dates.")
    labels = [
        {
            "text": f"label{index:02}",
            "language": {"tag": "en", "origin": "declared"},
            "role": "pref" if index == 0 else "alt",
        }
        for index in range(64)
    ]
    created = client.post(
        "/memory/concepts",
        headers=paired_headers,
        json={"scope": SCOPE, **CATALOG, "labels": labels},
    )
    assert created.status_code == 201, created.text
    proposal = _proposal(client, paired_headers, _claim(created.json()), binding)
    query = " ".join(label["text"] for label in labels)
    rows = _search(client, paired_headers, query, limit=1)
    assert [row["id"] for row in rows] == [memory_id]
    [evidence] = _evidence(rows[0])
    assert evidence["proposal"] == proposal
    assert len(evidence["matches"]) == 64
    assert {match["value"] for match in evidence["matches"]} == {label["text"] for label in labels}
    overflow = client.post(
        "/memory/search",
        headers=paired_headers,
        json={
            "query": query + " Archive.py",
            "scope": SCOPE,
            "symbolic": {"catalogs": [CATALOG]},
            "limit": 1,
        },
    )
    assert overflow.status_code == 409, overflow.text
    assert overflow.json() == {"detail": {"code": "symbolic_result_too_large"}}


@pytest.mark.parametrize("hide_related_sources", [False, True])
def test_incoming_relation_budget_counts_only_current_visible_evidence(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    hide_related_sources: bool,
) -> None:
    target_id, target_binding = _source(client, paired_headers, "Document cible Z91.")
    concept = _concept(client, paired_headers)
    target = _proposal(client, paired_headers, _claim(concept), target_binding)
    related_memory_id, related_binding = _source(
        client, paired_headers, "Document lié Y72 : notes sans label du catalogue."
    )
    related_ids: list[str] = []
    relation_ids: list[str] = []
    # The public store bounds outgoing relations. Each distinct observation has
    # only one outgoing edge, so 101 incoming edges are valid stored records.
    for index in range(101):
        claim = _claim(concept)
        claim["subject"] = _identity(f"RelatedObservation{index:03}")
        related = _proposal(client, paired_headers, claim, related_binding)
        related_ids.append(related["proposal_id"])
        response = client.post(
            f"/memory/proposals/{related['proposal_id']}/relations",
            headers=paired_headers,
            json={"target_proposal_id": target["proposal_id"], "relationship": "related_to"},
        )
        assert response.status_code == 201, response.text
        relation_ids.append(response.json()["id"])

    if hide_related_sources:
        # Public routes do not change sensitivity. Revoke access only in this
        # disposable database after all relations were created through HTTP.
        with sqlite3.connect(test_app.state.state_service.db_path) as db:
            db.execute(
                "UPDATE memory_items SET sensitivity='secret' WHERE id=?", (related_memory_id,)
            )
        rows = _search(client, paired_headers, limit=1)
        assert [row["id"] for row in rows] == [target_id]
        [evidence] = _evidence(rows[0])
        assert evidence["proposal"] == target
        assert evidence["relations"] == []
        assert all(
            value not in str(rows) for value in [related_memory_id, *related_ids, *relation_ids]
        )
    else:
        overflow = client.post(
            "/memory/search",
            headers=paired_headers,
            json={
                "query": "silent clock",
                "scope": SCOPE,
                "symbolic": {"catalogs": [CATALOG]},
                "limit": 1,
            },
        )
        assert overflow.status_code == 409, overflow.text
        assert overflow.json() == {"detail": {"code": "symbolic_result_too_large"}}
