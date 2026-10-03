"""Independent public regressions for symbolic bounds and current-source authority."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.memory_concepts import SymbolicClaim

SCOPE = "project:symbolic-review"
SYMBOLIC_TABLES = (
    "memory_symbolic_concepts",
    "memory_symbolic_labels",
    "memory_symbolic_proposals",
    "memory_symbolic_sources",
    "memory_symbolic_relations",
    "memory_symbolic_links",
)


def _claim() -> dict[str, Any]:
    def identity(value: str) -> dict[str, str]:
        return {"type": "identity", "namespace": "software", "identity": value}

    return {
        "scope": SCOPE,
        "namespace": "software",
        "scheme_id": "review",
        "kind": "fact",
        "subject": identity("cache"),
        "predicate": identity("explains"),
        "object": identity("delay"),
        "polarity": "affirmed",
        "modality": "possible",
        "applicability": {"padding": ""},
    }


def _source(
    client: TestClient, headers: dict[str, str], content: str
) -> tuple[str, dict[str, Any]]:
    created = client.post("/memory", headers=headers, json={"content": content, "scope": SCOPE})
    assert created.status_code == 201, created.text
    memory_id = created.json()["id"]
    described = client.get(
        f"/memory/{memory_id}/symbolic-source", headers=headers, params={"scope": SCOPE}
    )
    assert described.status_code == 200, described.text
    return memory_id, described.json()["bindings"][0]


def _page(client: TestClient, headers: dict[str, str], memory_id: str) -> dict[str, Any]:
    response = client.get(
        f"/memory/{memory_id}/proposals", headers=headers, params={"scope": SCOPE}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot(client: TestClient) -> dict[str, list[tuple[Any, ...]]]:
    with sqlite3.connect(client.app.state.state_service.db_path) as db:
        return {
            table: db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid").fetchall()
            for table in SYMBOLIC_TABLES
        }


def _fingerprint_bytes(claim: dict[str, Any]) -> int:
    # Include the fingerprint's policy envelope and all model defaults. The
    # bound is 8,000 UTF-8 bytes, not the request's larger 64 KiB cap.
    snapshot = SymbolicClaim.model_validate(claim).model_dump()
    return len(
        json.dumps(
            {"policy": "symbolic-claim-v1", "claim": snapshot},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )


@pytest.mark.parametrize("character", ["x", "é"], ids=["ascii", "utf8"])
def test_claim_fingerprint_byte_boundary_is_422_before_any_symbolic_write(
    client: TestClient, paired_headers: dict[str, str], character: str
) -> None:
    memory_id, binding = _source(client, paired_headers, "Independent source for size boundary.")
    claim = _claim()
    remaining = 8_000 - _fingerprint_bytes(claim)
    width = len(character.encode("utf-8"))
    claim["applicability"]["padding"] = character * (remaining // width) + "x" * (remaining % width)
    assert _fingerprint_bytes(claim) == 8_000
    oversized = {**claim, "applicability": {"padding": claim["applicability"]["padding"] + "x"}}
    assert _fingerprint_bytes(oversized) == 8_001
    before = _snapshot(client)
    rejected = client.post(
        "/memory/proposals", headers=paired_headers, json={"claim": oversized, "sources": [binding]}
    )
    assert rejected.status_code == 422, rejected.text
    assert _snapshot(client) == before
    assert _page(client, paired_headers, memory_id)["proposals"] == []

    accepted = client.post(
        "/memory/proposals", headers=paired_headers, json={"claim": claim, "sources": [binding]}
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["validation_status"] == "unvalidated"
    assert accepted.json()["grants_authority"] is False
    assert len(_page(client, paired_headers, memory_id)["proposals"]) == 1


@pytest.mark.parametrize("change", ["content", "sensitivity", "scope"])
def test_unqualified_replacement_cannot_supersede_visible_proposal(
    client: TestClient, paired_headers: dict[str, str], change: str
) -> None:
    retained_id, retained_binding = _source(
        client, paired_headers, "Original, still valid evidence."
    )
    replacement_id, replacement_binding = _source(client, paired_headers, "Replacement evidence.")
    proposals = []
    for binding in (retained_binding, replacement_binding):
        response = client.post(
            "/memory/proposals",
            headers=paired_headers,
            json={"claim": _claim(), "sources": [binding]},
        )
        assert response.status_code == 201, response.text
        proposals.append(response.json())
    retained, replacement = proposals
    related = client.post(
        f"/memory/proposals/{replacement['proposal_id']}/relations",
        headers=paired_headers,
        json={"target_proposal_id": retained["proposal_id"], "relationship": "supersedes"},
    )
    assert related.status_code == 201, related.text
    before = _page(client, paired_headers, retained_id)
    assert before["proposals"][0]["lifecycle"] == "superseded"
    relation_rows = _snapshot(client)["memory_symbolic_relations"]

    if change == "content":
        edited = client.patch(
            f"/memory/{replacement_id}",
            headers=paired_headers,
            json={"content": "Different evidence."},
        )
        assert edited.status_code == 200, edited.text
    else:
        # There is no public sensitivity/scope update operation. Simulate an
        # authoritative access change in the isolated test database only.
        value = "secret" if change == "sensitivity" else "project:other"
        with sqlite3.connect(client.app.state.state_service.db_path) as db:
            db.execute(f"UPDATE memory_items SET {change}=? WHERE id=?", (value, replacement_id))

    after = _page(client, paired_headers, retained_id)
    assert [item["proposal_id"] for item in after["proposals"]] == [retained["proposal_id"]]
    assert after["proposals"][0]["lifecycle"] == "active"
    assert after["proposals"][0]["validation_status"] == "unvalidated"
    assert after["proposals"][0]["grants_authority"] is False
    assert after["relations"] == []
    assert _snapshot(client)["memory_symbolic_relations"] == relation_rows
    if change != "content":
        hidden = client.get(
            f"/memory/{replacement_id}/proposals", headers=paired_headers, params={"scope": SCOPE}
        )
        assert hidden.status_code == 404
