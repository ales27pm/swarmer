"""Exercise proposal provenance and forgetting through the paired public API."""

from copy import deepcopy
from hashlib import sha256
from typing import Any

import pytest
from fastapi.testclient import TestClient


def identity(value: str) -> dict[str, str]:
    return {"type": "identity", "namespace": "software", "identity": value}


def claim(scope: str = "project:alpha") -> dict[str, Any]:
    return {
        "scope": scope,
        "namespace": "software",
        "scheme_id": "engineering",
        "kind": "constraint",
        "subject": identity("Cache"),
        "predicate": identity("invalidate"),
        "object": identity("cache.py"),
        "polarity": "affirmed",
        "modality": "required",
        "effective_conditions": [{"relation": "only_after", "argument": identity("commit")}],
    }


def source(
    client: TestClient,
    headers: dict[str, str],
    *,
    scope: str = "project:alpha",
    content: str = "Invalider Cache dans cache.py seulement après commit.",
) -> tuple[str, dict[str, Any]]:
    created = client.post("/memory", headers=headers, json={"scope": scope, "content": content})
    assert created.status_code == 201, created.text
    memory_id = created.json()["id"]
    result = client.get(
        f"/memory/{memory_id}/symbolic-source", params={"scope": scope}, headers=headers
    )
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert content not in result.text
    binding = next(item for item in result.json()["bindings"] if item["field"] == "content")
    assert binding["field_sha256"] == sha256(content.encode()).hexdigest()
    return memory_id, binding


def propose(client: TestClient, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    result = client.post("/memory/proposals", json=body, headers=headers)
    assert result.status_code == 201, result.text
    assert result.headers["cache-control"] == "no-store"
    record = result.json()
    assert record["validation_status"] == "unvalidated"
    assert record["grants_authority"] is False
    assert all(item["origin"] == "source_document" for item in record["sources"])
    return record


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/memory/concepts"),
        ("get", "/memory/concepts/urn:swarmer:concept:" + "0" * 32 + "?scope=general"),
        ("post", "/memory/proposals"),
        ("get", "/memory/missing/symbolic-source?scope=general"),
        ("get", "/memory/missing/proposals?scope=general"),
        ("post", "/memory/proposals/missing/relations"),
    ],
)
def test_symbolic_routes_require_pairing(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 401


def test_equivalent_observations_keep_both_proposals_and_original_bindings(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    french_id, french = source(client, paired_headers)
    english_id, english = source(
        client, paired_headers, content="Invalidate Cache in cache.py only after commit."
    )
    first = propose(client, paired_headers, {"claim": claim(), "sources": [french]})
    second = propose(client, paired_headers, {"claim": claim(), "sources": [english]})
    assert first["proposal_id"] != second["proposal_id"]
    assert first["claim_sha256"] == second["claim_sha256"]
    assert first["sources"][0]["binding"] == french
    assert second["sources"][0]["binding"] == english
    for mid, proposal in ((french_id, first), (english_id, second)):
        page = client.get(
            f"/memory/{mid}/proposals", params={"scope": "project:alpha"}, headers=paired_headers
        )
        assert page.status_code == 200
        assert page.json()["proposals"] == [proposal]
        assert page.headers["cache-control"] == "no-store"


def test_concept_labels_link_to_claim_without_client_controlled_promotion(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    mid, binding = source(client, paired_headers)
    body = {
        "scope": "project:alpha",
        "namespace": "software",
        "scheme_id": "engineering",
        "labels": [
            {"text": "cache", "language": {"tag": "fr-CA", "origin": "declared"}, "role": "pref"},
            {"text": "cache", "language": {"tag": "en", "origin": "declared"}, "role": "pref"},
        ],
    }
    concept = client.post("/memory/concepts", headers=paired_headers, json=body)
    assert concept.status_code == 201, concept.text
    assert concept.json()["curation_status"] == "proposed"
    assert concept.json()["grants_authority"] is False
    read = client.get(
        f"/memory/concepts/{concept.json()['concept_id']}",
        params={"scope": "project:alpha"},
        headers=paired_headers,
    )
    assert read.status_code == 200, read.text
    assert read.json() == concept.json()
    assert read.headers["cache-control"] == "no-store"
    assert (
        client.get(
            f"/memory/concepts/{concept.json()['concept_id']}",
            params={"scope": "project:other"},
            headers=paired_headers,
        ).status_code
        == 404
    )
    c = claim()
    c["subject"] = identity(concept.json()["concept_id"])
    proposal = propose(client, paired_headers, {"claim": c, "sources": [binding]})
    assert proposal["concept_ids"] == [concept.json()["concept_id"]]
    for forbidden in ({"curation_status": "curated"}, {"grants_authority": True}):
        assert (
            client.post(
                "/memory/concepts", json={**body, **forbidden}, headers=paired_headers
            ).status_code
            == 422
        )
    bad = deepcopy(c)
    bad["subject"] = identity("urn:swarmer:concept:" + "0" * 32)
    assert (
        client.post(
            "/memory/proposals", json={"claim": bad, "sources": [binding]}, headers=paired_headers
        ).status_code
        == 409
    )
    assert client.get(
        f"/memory/{mid}/proposals", params={"scope": "project:alpha"}, headers=paired_headers
    ).json()["proposals"] == [proposal]


@pytest.mark.parametrize(
    "field,value",
    [
        ("field_sha256", "0" * 64),
        ("document_sha256", "0" * 64),
        ("revision", 99),
        ("view_id", "unknown"),
    ],
)
def test_forged_or_stale_source_binding_is_rejected(
    client: TestClient, paired_headers: dict[str, str], field: str, value: Any
) -> None:
    _, binding = source(client, paired_headers)
    binding[field] = value
    result = client.post(
        "/memory/proposals", json={"claim": claim(), "sources": [binding]}, headers=paired_headers
    )
    assert result.status_code == 409
    assert result.headers["cache-control"] == "no-store"


def test_source_scope_sensitivity_and_origin_cannot_be_bypassed(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    mid, binding = source(client, paired_headers)
    for suffix in ("symbolic-source", "proposals"):
        response = client.get(
            f"/memory/{mid}/{suffix}", params={"scope": "project:other"}, headers=paired_headers
        )
        assert response.status_code == 404
    foreign = client.post(
        "/memory/proposals",
        json={"claim": claim("project:other"), "sources": [binding]},
        headers=paired_headers,
    )
    assert foreign.status_code == 404
    forged = {**binding, "origin": "user_statement"}
    assert (
        client.post(
            "/memory/proposals",
            json={"claim": claim(), "sources": [forged]},
            headers=paired_headers,
        ).status_code
        == 422
    )
    sensitive = client.post(
        "/memory",
        json={"content": "Private original", "scope": "project:alpha", "sensitivity": "secret"},
        headers=paired_headers,
    ).json()
    for suffix in ("symbolic-source", "proposals"):
        assert (
            client.get(
                f"/memory/{sensitive['id']}/{suffix}",
                params={"scope": "project:alpha"},
                headers=paired_headers,
            ).status_code
            == 404
        )
    for scope in ("global", "arbitrary", ""):
        assert (
            client.get(
                f"/memory/{mid}/symbolic-source", params={"scope": scope}, headers=paired_headers
            ).status_code
            == 422
        )


def test_edit_revokes_readability_and_forget_purges_derived_proposals(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    mid, binding = source(client, paired_headers)
    before = propose(client, paired_headers, {"claim": claim(), "sources": [binding]})
    assert (
        client.patch(
            f"/memory/{mid}", json={"content": "Correction: before commit."}, headers=paired_headers
        ).status_code
        == 200
    )
    page = client.get(
        f"/memory/{mid}/proposals", params={"scope": "project:alpha"}, headers=paired_headers
    )
    assert page.status_code == 200
    assert page.json()["proposals"] == []
    assert page.json()["unavailable"] == [
        {"proposal_id": before["proposal_id"], "reason": "stale_source"}
    ]
    assert (
        client.post(
            "/memory/proposals",
            json={"claim": claim(), "sources": [binding]},
            headers=paired_headers,
        ).status_code
        == 409
    )
    assert client.delete(f"/memory/{mid}", headers=paired_headers).status_code == 204
    assert (
        client.get(
            f"/memory/{mid}/proposals", params={"scope": "project:alpha"}, headers=paired_headers
        ).status_code
        == 404
    )
    fresh_id, fresh = source(client, paired_headers)
    proposal = propose(client, paired_headers, {"claim": claim(), "sources": [fresh]})
    result = client.post(
        f"/memory/proposals/{proposal['proposal_id']}/relations",
        json={"target_proposal_id": before["proposal_id"], "relationship": "related_to"},
        headers=paired_headers,
    )
    assert result.status_code == 404
    assert fresh_id != mid


def test_contradictions_and_supersession_are_explicit_and_remain_unvalidated(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    mid, binding = source(client, paired_headers)
    first = propose(client, paired_headers, {"claim": claim(), "sources": [binding]})
    opposite = claim()
    opposite["polarity"] = "negated"
    second = propose(client, paired_headers, {"claim": opposite, "sources": [binding]})
    assert first["claim_sha256"] != second["claim_sha256"]
    endpoint = f"/memory/proposals/{second['proposal_id']}/relations"
    for relationship in ("contradicts", "supersedes"):
        relation = client.post(
            endpoint,
            json={"target_proposal_id": first["proposal_id"], "relationship": relationship},
            headers=paired_headers,
        )
        assert relation.status_code == 201, relation.text
        assert relation.json()["grants_authority"] is False
    page = client.get(
        f"/memory/{mid}/proposals", params={"scope": "project:alpha"}, headers=paired_headers
    ).json()
    records = {item["proposal_id"]: item for item in page["proposals"]}
    assert records[first["proposal_id"]]["lifecycle"] == "superseded"
    assert records[second["proposal_id"]]["lifecycle"] == "active"
    assert all(item["validation_status"] == "unvalidated" for item in records.values())
    cycle = client.post(
        f"/memory/proposals/{first['proposal_id']}/relations",
        json={"target_proposal_id": second["proposal_id"], "relationship": "supersedes"},
        headers=paired_headers,
    )
    assert cycle.status_code == 409
