"""Paired-device API boundary for text-free read-only index diagnostics."""

from __future__ import annotations

import sqlite3

import pytest

from app.services.memory_index_coverage import MemoryIndexCoverageError
from tests.test_memory_index_coverage import NeverCalledProvider, cache_views, database_dump

PATH = "/memory/index-coverage"


def test_coverage_requires_pairing_and_never_caches_authentication_errors(client):
    response = client.get(PATH, params={"scope": "general"})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"scope": " "},
        {"scope": "x" * 101},
        {"scope": "general", "kind": "x" * 101},
        {"scope": "general", "sensitivity": " "},
        {"scope": "general", "after_id": "x" * 201},
        {"scope": "general", "limit": "0"},
        {"scope": "general", "limit": "101"},
    ],
)
def test_coverage_validates_bounded_explicit_selection_without_cache(
    client, paired_headers, params
):
    response = client.get(PATH, params=params, headers=paired_headers)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"


def test_coverage_does_not_expose_content_metadata_vectors_or_private_provider_endpoint(
    client, paired_headers
):
    state = client.app.state.state_service
    secret = "Private content marker 985124 not a coverage field"
    item = client.post(
        "/memory",
        json={"content": secret, "summary": "Private summary marker", "scope": "general"},
        headers=paired_headers,
    ).json()
    with sqlite3.connect(state.db_path) as db:
        db.execute(
            "UPDATE memory_items SET metadata_json=? WHERE id=?",
            ('{"private_note":"Private metadata marker"}', item["id"]),
        )
    state.embedding_service = NeverCalledProvider()
    cache_views(state, item["id"])
    # Authentication advances devices.last_seen_at on every paired request.
    # The diagnostic must leave all other durable data and schemas unchanged.
    before = [line for line in database_dump(state) if not line.startswith('INSERT INTO "devices"')]
    response = client.get(PATH, params={"scope": "general"}, headers=paired_headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    report = response.json()
    assert report["page_status"] == "covered" and report["covered_views"] == 1
    assert report["provider_readiness"] == "configured_not_probed"
    assert set(report["items"][0]) == {"memory_id", "status", "revision", "views"}
    assert set(report["items"][0]["views"][0]) == {"role", "view_id", "status"}
    for forbidden in (
        secret,
        "Private summary marker",
        "Private metadata marker",
        "source_id",
        "source_sha256",
        "canonical_receipt_id",
        "pipeline_signature",
        "vector_json",
        NeverCalledProvider.base_url,
    ):
        assert forbidden not in response.text
    after = [line for line in database_dump(state) if not line.startswith('INSERT INTO "devices"')]
    assert after == before


def test_api_filters_scope_and_default_sensitivity_before_reporting(client, paired_headers):
    created = {}
    for label, fields in (
        ("normal", {"scope": "general"}),
        ("private", {"scope": "general", "sensitivity": "private"}),
        ("outside", {"scope": "project:outside"}),
    ):
        response = client.post("/memory", json={"content": label, **fields}, headers=paired_headers)
        assert response.status_code == 201
        created[label] = response.json()["id"]
    default = client.get(PATH, params={"scope": "general"}, headers=paired_headers)
    assert default.status_code == 200
    assert [item["memory_id"] for item in default.json()["items"]] == [created["normal"]]
    private = client.get(
        PATH, params={"scope": "general", "sensitivity": "private"}, headers=paired_headers
    )
    assert private.status_code == 200
    assert [item["memory_id"] for item in private.json()["items"]] == [created["private"]]
    empty = client.get(PATH, params={"scope": "general", "kind": "absent"}, headers=paired_headers)
    assert empty.json()["page_status"] == "empty"


def test_deeply_nested_metadata_api_error_is_sanitized_and_uncached(client, paired_headers):
    state = client.app.state.state_service
    item = client.post(
        "/memory", json={"content": "Nested metadata fixture"}, headers=paired_headers
    ).json()
    with sqlite3.connect(state.db_path) as db:
        db.execute(
            "UPDATE memory_items SET metadata_json=? WHERE id=?",
            ("[" * 10_000 + "0" + "]" * 10_000, item["id"]),
        )
    response = client.get(PATH, params={"scope": "general"}, headers=paired_headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "memory_index_unavailable"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "code,status",
    [("memory_index_configuration_changed", 409), ("memory_index_unavailable", 503)],
)
def test_coverage_errors_keep_exact_nonprivate_contract(
    client, paired_headers, monkeypatch, code, status
):
    async def unavailable(request):
        raise MemoryIndexCoverageError(code, status)

    monkeypatch.setattr(client.app.state.state_service, "memory_index_coverage", unavailable)
    response = client.get(PATH, params={"scope": "general"}, headers=paired_headers)
    assert response.status_code == status
    assert response.json() == {"detail": code}
    assert response.headers["cache-control"] == "no-store"
