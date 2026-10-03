from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest

from app.services.memory_normalization import (
    POLICY_SHA256,
    POLICY_VERSION,
    MemoryNormalizationResult,
    canonical_text_sha256,
)
from app.services.memory_text_views import (
    claim_projection_batch,
    complete_current_projection_locked,
    delete_text_views_locked,
    fail_projection_claim,
    finish_projection_locked,
    initialize_text_view_schema_locked,
    mark_projection_batch_dispatched,
    mark_projection_batch_response_received,
    mark_projection_dispatched,
    mark_projection_response_received,
    projection_status,
    read_projection_source,
    record_text_views_locked,
    refresh_text_view_head_locked,
    reserve_projection_batch,
    retry_known_projection_failure,
    text_view_sha256,
)

NOW = "2026-10-03T10:00:00+00:00"
LATER = "2026-10-03T10:00:10+00:00"
EXPIRED = "2026-10-03T10:02:00+00:00"


async def database(tmp_path: Path) -> Path:
    path = tmp_path / "views.db"
    async with aiosqlite.connect(path) as db:
        await db.executescript("""
            CREATE TABLE memory_items(id TEXT PRIMARY KEY, scope TEXT, kind TEXT,
              sensitivity TEXT, content TEXT, summary TEXT, metadata_json TEXT, updated_at TEXT,
              confidence REAL NOT NULL DEFAULT 1.0);
            CREATE TABLE memory_embeddings(memory_id TEXT, provider TEXT, dimensions INTEGER,
              vector_json TEXT, updated_at TEXT, PRIMARY KEY(memory_id,provider));
            CREATE TABLE memory_source_journal(id TEXT PRIMARY KEY, scope TEXT, kind TEXT,
              sensitivity TEXT, content TEXT, summary TEXT, source_sha256 TEXT, created_at TEXT);
            CREATE TABLE memory_canonical_receipts(id TEXT PRIMARY KEY,memory_id TEXT,source_id TEXT,
              status TEXT,normalization_signature TEXT,expected_revision TEXT,result_json TEXT);
        """)
        await db.execute("BEGIN IMMEDIATE")
        await initialize_text_view_schema_locked(db)
        await db.commit()
    return path


async def put(
    path: Path,
    content: str = "Texte original",
    *,
    revision: str = NOW,
    provider: str | None = "embed-v1",
    canonical: str | None = None,
    memory_id: str = "m1",
) -> int:
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        source_sha = text_view_sha256(content, None)
        source_id = (
            ("msrc_" + digest(["project:p", "fact", "normal", source_sha])) if canonical else None
        )
        metadata = (
            {
                "source_id": source_id,
                "source_sha256": source_sha,
                "normalization_signature": "normalizer-v1",
                "canonical_language": "en",
                "canonical_receipt_id": "receipt-1",
                "normalization_policy_version": POLICY_VERSION,
                "supersedes_revision": None,
            }
            if canonical
            else {}
        )
        if canonical:
            unit = MemoryNormalizationResult(
                canonical_text=canonical,
                canonical_sha256=canonical_text_sha256(canonical),
                scope="project:p",
                kind="fact",
                applicability_sha256=digest({"sensitivity": "normal", "confidence": 1.0}),
                source_id=source_id + ":content",
                source_version="v1",
                source_sha256=canonical_text_sha256(content),
                source_language="fr",
                language_origin="model_reviewed",
                expected_memory_revision=None,
                normalization_policy_version=POLICY_VERSION,
                normalization_policy_sha256=POLICY_SHA256,
                normalization_signature="normalizer-v1",
                translator_model="fixture",
                translator_revision=None,
                reviewer_model="fixture",
                reviewer_revision=None,
                source_revalidated=True,
                literal_count=0,
                deduplication_identity="fixture",
            )
            metadata["content"] = unit.model_dump(exclude={"canonical_text"})
            metadata["summary"] = None
            await db.execute(
                "INSERT OR REPLACE INTO memory_canonical_receipts VALUES(?,?,?,?,?,?,?)",
                (
                    "receipt-1",
                    memory_id,
                    source_id,
                    "accepted",
                    "normalizer-v1",
                    None,
                    json.dumps({"content": canonical, "summary": None}),
                ),
            )
            await db.execute(
                "INSERT OR REPLACE INTO memory_source_journal VALUES(?,?,?,?,?,?,?,?)",
                (source_id, "project:p", "fact", "normal", content, None, source_sha, NOW),
            )
        await db.execute(
            "INSERT OR REPLACE INTO memory_items(id,scope,kind,sensitivity,content,summary,metadata_json,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                memory_id,
                "project:p",
                "fact",
                "normal",
                canonical or content,
                None,
                json.dumps(metadata),
                revision,
            ),
        )
        version = await record_text_views_locked(
            db,
            memory_id=memory_id,
            item_revision=revision,
            original_content=content,
            original_summary=None,
            original_language="fr-CA",
            source_id=source_id,
            source_sha256=source_sha,
            canonical_content=canonical,
            normalization_signature="normalizer-v1" if canonical else None,
            embedding_provider=provider,
            now=revision,
        )
        await db.commit()
    return version


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def rows(path: Path, table: str) -> list[dict[str, object]]:
    assert table in {
        "memory_text_views",
        "memory_text_heads",
        "memory_index_outbox",
        "memory_embeddings",
    }
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        return [dict(row) for row in await (await db.execute(f"SELECT * FROM {table}")).fetchall()]


@pytest.mark.asyncio
async def test_views_and_private_intent_are_atomic_and_original_is_exact(tmp_path: Path) -> None:
    path = await database(tmp_path)
    assert await put(path, "Garder `A.swift` : 30 ms.\n", canonical="Keep `A.swift`: 30 ms.\n") == 1
    views = await rows(path, "memory_text_views")
    assert {v["role"] for v in views} == {"original", "canonical"}
    assert (
        next(v for v in views if v["role"] == "original")["content"]
        == "Garder `A.swift` : 30 ms.\n"
    )
    outbox = await rows(path, "memory_index_outbox")
    assert len(outbox) == 1
    assert "A.swift" not in json.dumps(outbox)
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await delete_text_views_locked(db, memory_id="m1", now=LATER)
        await db.rollback()
    assert await rows(path, "memory_text_views") == views
    assert await rows(path, "memory_index_outbox") == outbox


@pytest.mark.asyncio
async def test_idempotence_text_revision_and_provider_identity(tmp_path: Path) -> None:
    path = await database(tmp_path)
    assert await put(path) == 1
    assert await put(path) == 1
    assert len(await rows(path, "memory_text_views")) == 1
    assert len(await rows(path, "memory_index_outbox")) == 1
    assert await put(path, revision=LATER, provider="embed-v2") == 1
    assert len(await rows(path, "memory_index_outbox")) == 2
    assert await put(path, "Autre texte", revision=EXPIRED) == 2
    assert len(await rows(path, "memory_text_views")) == 2


@pytest.mark.asyncio
async def test_stale_worker_cannot_restore_updated_or_deleted_memory(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path)
    (old,) = await claim_projection_batch(path, "worker-a", now=NOW)
    await put(path, "Nouvelle version", revision=LATER)
    assert await read_projection_source(path, old, now=LATER) is None
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert not await finish_projection_locked(db, old, vector=[1.0, 0.0], now=LATER)
        await db.commit()
    (current,) = await claim_projection_batch(path, "worker-b", now=LATER)
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("DELETE FROM memory_items WHERE id='m1'")
        await delete_text_views_locked(db, memory_id="m1", now=LATER)
        assert not await finish_projection_locked(db, current, vector=[1.0, 0.0], now=LATER)
        await db.commit()
    assert not await rows(path, "memory_text_views")
    assert not await rows(path, "memory_embeddings")
    (tombstone,) = await rows(path, "memory_text_heads")
    assert tombstone["deleted"] == 1 and tombstone["revision"] == 3
    assert tombstone["source_sha256"] is None
    assert all("Nouvelle" not in json.dumps(row) for row in await rows(path, "memory_index_outbox"))


@pytest.mark.asyncio
async def test_pin_only_keeps_projection_and_uses_current_item_revision(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path)
    (claim,) = await claim_projection_batch(path, "worker", now=NOW)
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE memory_items SET updated_at=? WHERE id='m1'", (LATER,))
        await refresh_text_view_head_locked(db, "m1", LATER, LATER)
        assert await finish_projection_locked(db, claim, vector=[1.0, 0.0], now=LATER)
        await db.commit()
    (vector,) = await rows(path, "memory_embeddings")
    assert vector["updated_at"] == LATER
    assert (
        len(await rows(path, "memory_text_views"))
        == len(await rows(path, "memory_index_outbox"))
        == 1
    )
    assert await claim_projection_batch(path, "worker", now=LATER) == []


@pytest.mark.asyncio
async def test_expired_lease_and_owner_generation_fence_all_acknowledgements(
    tmp_path: Path,
) -> None:
    path = await database(tmp_path)
    await put(path)
    (old,) = await claim_projection_batch(path, "a", now=NOW)
    assert await claim_projection_batch(path, "b", now=LATER) == []
    (new,) = await claim_projection_batch(path, "b", now=EXPIRED)
    assert new.generation == old.generation + 1
    assert not await fail_projection_claim(
        path, old, error_category="provider_unavailable", now=EXPIRED
    )
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert not await finish_projection_locked(db, old, vector=[1.0], now=EXPIRED)
        assert await finish_projection_locked(db, new, vector=[1.0], now=EXPIRED)
        await db.commit()


@pytest.mark.asyncio
async def test_missing_provider_intent_survives_and_other_provider_is_not_claimed(
    tmp_path: Path,
) -> None:
    path = await database(tmp_path)
    await put(path, provider=None)
    assert (await projection_status(path))["configuration_missing"] == 1
    assert (await projection_status(path, provider="embed-v1"))["configuration_missing"] == 0
    assert (await projection_status(path, provider="embed-v1"))["unbound_provider"] == 1
    (claim,) = await claim_projection_batch(path, "a", provider="embed-v1", now=NOW)
    assert claim.provider == "embed-v1"
    assert await fail_projection_claim(path, claim, error_category="provider_unavailable", now=NOW)
    assert await claim_projection_batch(path, "b", provider="embed-v2", now=EXPIRED) == []
    assert (await projection_status(path, provider="embed-v2"))["provider_mismatch"] == 1
    assert await claim_projection_batch(path, "b", memory_id="other", now=EXPIRED) == []
    assert len(await claim_projection_batch(path, "b", provider="embed-v1", now=EXPIRED)) == 1


@pytest.mark.asyncio
async def test_strict_vector_and_source_checks_leave_intent_unacknowledged(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path, canonical="Original text")
    (claim,) = await claim_projection_batch(path, "a", now=NOW)
    source = await read_projection_source(path, claim, now=NOW)
    assert source is not None and source.content == "Original text" and source.scope == "project:p"
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError):
            await finish_projection_locked(db, claim, vector=[float("nan")], now=NOW)
        await db.execute("UPDATE memory_source_journal SET content='tampered'")
        assert not await finish_projection_locked(db, claim, vector=[1.0], now=NOW)
        await db.commit()
    assert not await rows(path, "memory_embeddings")
    assert (await rows(path, "memory_index_outbox"))[0]["status"] == "claimed"


@pytest.mark.asyncio
async def test_helpers_require_outer_transaction_and_known_error_categories(tmp_path: Path) -> None:
    path = await database(tmp_path)
    async with aiosqlite.connect(path) as db:
        with pytest.raises(RuntimeError):
            await delete_text_views_locked(db, memory_id="m1", now=NOW)
    await put(path)
    (claim,) = await claim_projection_batch(path, "a", now=NOW)
    with pytest.raises(ValueError):
        await fail_projection_claim(path, claim, error_category="sensitive raw failure", now=NOW)


@pytest.mark.asyncio
async def test_ack_and_embedding_commit_or_rollback_together(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path)
    (claim,) = await claim_projection_batch(path, "a", now=NOW)
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await finish_projection_locked(db, claim, vector=[1.0, 0.0], now=NOW)
        await db.rollback()
    assert not await rows(path, "memory_embeddings")
    assert (await rows(path, "memory_index_outbox"))[0]["status"] == "claimed"


@pytest.mark.asyncio
async def test_two_claimers_cannot_both_acquire_the_same_current_view(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path)
    left, right = await asyncio.gather(
        claim_projection_batch(path, "left", now=NOW),
        claim_projection_batch(path, "right", now=NOW),
    )
    assert sorted((len(left), len(right))) == [0, 1]


@pytest.mark.asyncio
async def test_retry_has_not_before_and_does_not_duplicate_intent(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path)
    (first,) = await claim_projection_batch(path, "worker", now=NOW)
    assert await fail_projection_claim(path, first, error_category="provider_unavailable", now=NOW)
    assert await claim_projection_batch(path, "worker", now=LATER) == []
    (second,) = await claim_projection_batch(path, "worker", now=EXPIRED)
    assert second.event_id == first.event_id and second.generation == 2
    assert len(await rows(path, "memory_index_outbox")) == 1


@pytest.mark.asyncio
async def test_old_delete_cannot_erase_a_recreated_memory(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("DELETE FROM memory_items WHERE id='m1'")
        await delete_text_views_locked(db, memory_id="m1", now=NOW)
        await delete_text_views_locked(db, memory_id="m1", now=NOW)
        await db.commit()
    (deletion,) = await claim_projection_batch(path, "worker", now=NOW)
    assert deletion.operation == "delete" and deletion.revision == 2
    assert await put(path, "Recréée", revision=LATER) == 3
    (fresh,) = await claim_projection_batch(path, "worker", now=LATER)
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await finish_projection_locked(db, fresh, vector=[0.0, 1.0], now=LATER)
        assert not await finish_projection_locked(db, deletion, vector=None, now=LATER)
        await db.commit()
    assert len(await rows(path, "memory_embeddings")) == 1


@pytest.mark.asyncio
async def test_backfill_cannot_steal_active_claim_but_fences_expired_claim(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path)
    (claim,) = await claim_projection_batch(path, "worker", now=NOW)
    (head,) = await rows(path, "memory_text_heads")
    for timestamp, permitted in ((LATER, False), (EXPIRED, True)):
        async with aiosqlite.connect(path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT INTO memory_embeddings VALUES(?,?,?,?,?)",
                ("m1", "embed-v1", 1, "[1.0]", NOW),
            )
            accepted = await complete_current_projection_locked(
                db,
                memory_id="m1",
                item_revision=NOW,
                provider="embed-v1",
                revision=int(head["revision"]),
                view_id=str(head["index_view_id"]),
                source_sha256=str(head["source_sha256"]),
                now=timestamp,
            )
            assert accepted is permitted
            if accepted:
                assert not await finish_projection_locked(db, claim, vector=[0.0], now=timestamp)
                await db.commit()
            else:
                await db.rollback()
    assert (await rows(path, "memory_embeddings"))[0]["vector_json"] == "[1.0]"
    assert (await rows(path, "memory_index_outbox"))[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_migration_seed_is_additive_and_transactional(tmp_path: Path) -> None:
    path = await database(tmp_path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO memory_items(id,scope,kind,sensitivity,content,summary,metadata_json,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("m1", "project:p", "fact", "normal", "Source", None, "{}", NOW),
        )
        await db.execute(
            "INSERT INTO memory_embeddings VALUES(?,?,?,?,?)", ("m1", "embed-v1", 1, "[1.0]", NOW)
        )
        await db.commit()
        await db.execute("BEGIN IMMEDIATE")
        args = {
            "memory_id": "m1",
            "item_revision": NOW,
            "original_content": "Source",
            "original_summary": None,
            "original_language": "und",
            "source_id": None,
            "source_sha256": text_view_sha256("Source", None),
            "migration_seed": True,
            "now": NOW,
        }
        assert await record_text_views_locked(db, **args) == 1
        with pytest.raises(ValueError, match="without a text head"):
            await record_text_views_locked(db, **args)
        await db.rollback()
    assert not await rows(path, "memory_text_heads")
    assert not await rows(path, "memory_index_outbox")
    assert (await rows(path, "memory_embeddings"))[0]["vector_json"] == "[1.0]"
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await record_text_views_locked(db, **args)
        await db.commit()
    assert (await rows(path, "memory_embeddings"))[0]["vector_json"] == "[1.0]"
    assert (await rows(path, "memory_index_outbox"))[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_unconfigured_intent_does_not_duplicate_explicit_provider_job(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path, provider=None)
    await put(path, provider="embed-v1")
    claims = await claim_projection_batch(path, "worker", provider="embed-v1", limit=1, now=NOW)
    assert len(claims) == 1 and claims[0].provider == "embed-v1"
    assert (await projection_status(path, provider="embed-v1"))["obsolete"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("vector", [[0.0, 0.0], [1.0] * 8193, [True], [], [float("inf")]])
async def test_sink_matches_shared_vector_contract(tmp_path: Path, vector: list[float]) -> None:
    path = await database(tmp_path)
    await put(path)
    (claim,) = await claim_projection_batch(path, "worker", now=NOW)
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="vector is invalid"):
            await finish_projection_locked(db, claim, vector=vector, now=NOW)
        await db.rollback()
    assert not await rows(path, "memory_embeddings")
    assert (await rows(path, "memory_index_outbox"))[0]["status"] == "claimed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["receipt", "unit", "scope", "normalization_signature"])
async def test_canonical_provenance_is_requalified_before_projection_commit(
    tmp_path: Path, mutation: str
) -> None:
    path = await database(tmp_path)
    await put(path, canonical="Original text")
    (claim,) = await claim_projection_batch(path, "worker", now=NOW)
    assert await read_projection_source(path, claim, now=NOW) is not None
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if mutation == "receipt":
            await db.execute("UPDATE memory_canonical_receipts SET status='failed'")
        elif mutation == "scope":
            await db.execute("UPDATE memory_items SET scope='other'")
        else:
            (raw,) = await (await db.execute("SELECT metadata_json FROM memory_items")).fetchone()
            metadata = json.loads(raw)
            if mutation == "unit":
                metadata["content"]["source_revalidated"] = False
            else:
                metadata["normalization_signature"] = "another-pipeline"
            await db.execute("UPDATE memory_items SET metadata_json=?", (json.dumps(metadata),))
        assert not await finish_projection_locked(db, claim, vector=[1.0, 0.0], now=LATER)
        await db.commit()
    assert not await rows(path, "memory_embeddings")


@pytest.mark.asyncio
async def test_status_and_explicit_retry_distinguish_configuration_failure_and_unknown_lease(
    tmp_path: Path,
) -> None:
    path = await database(tmp_path)
    await put(path)
    (claim,) = await claim_projection_batch(path, "worker", now=NOW)
    assert not await retry_known_projection_failure(
        path, memory_id="m1", provider="embed-v1", now=LATER
    )

    assert not await retry_known_projection_failure(
        path, memory_id="m1", provider="embed-v1", now=EXPIRED
    )
    assert await fail_projection_claim(path, claim, error_category="invalid_vector", now=LATER)
    status = await projection_status(path, provider="embed-v1", now=LATER)
    assert status["invalid_vector"] == 1 and status["configuration_missing"] == 0
    assert (await projection_status(path, now=LATER))["configuration_missing"] == 1
    assert not await retry_known_projection_failure(
        path, memory_id="m1", provider="embed-v2", now=LATER
    )
    assert await retry_known_projection_failure(
        path, memory_id="m1", provider="embed-v1", now=LATER
    )
    (retried,) = await claim_projection_batch(path, "worker", now=LATER)
    assert await fail_projection_claim(path, retried, error_category="cancelled", now=LATER)
    assert not await retry_known_projection_failure(
        path, memory_id="m1", provider="embed-v1", now=LATER
    )


async def snapshot(path: Path, memory_id: str = "m1") -> dict[str, object]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                """SELECT m.*,h.revision AS projection_revision,
            h.index_view_id AS projection_view_id,h.source_sha256 AS projection_source_sha256
            FROM memory_items m JOIN memory_text_heads h ON h.memory_id=m.id WHERE m.id=?""",
                (memory_id,),
            )
        ).fetchone()
        assert row is not None
        return dict(row)


@pytest.mark.asyncio
async def test_dispatched_unknown_survives_lease_expiry_restart_and_all_retry_paths(
    tmp_path: Path,
) -> None:
    path = await database(tmp_path)
    await put(path)
    (claim,) = await claim_projection_batch(path, "crashed-process", now=NOW)
    original = await snapshot(path)
    assert await mark_projection_dispatched(path, claim, now=NOW)
    # Every following operation opens a fresh connection, as after a process crash.
    assert await claim_projection_batch(path, "restarted", now=EXPIRED) == []
    assert not await retry_known_projection_failure(
        path, memory_id="m1", provider="embed-v1", now=EXPIRED
    )
    assert not await fail_projection_claim(path, claim, error_category="cancelled", now=LATER)
    assert not await fail_projection_claim(
        path, claim, error_category="provider_unavailable", now=EXPIRED
    )
    assert (
        await reserve_projection_batch(
            path, "backfill", snapshots=[original], provider="embed-v1", now=EXPIRED
        )
        == []
    )
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert not await finish_projection_locked(db, claim, vector=[1.0], now=LATER)
        await db.execute(
            "INSERT INTO memory_embeddings VALUES(?,?,?,?,?)", ("m1", "embed-v1", 1, "[1.0]", NOW)
        )
        assert not await complete_current_projection_locked(
            db,
            memory_id="m1",
            item_revision=NOW,
            provider="embed-v1",
            revision=int(original["projection_revision"]),
            view_id=str(original["projection_view_id"]),
            source_sha256=str(original["projection_source_sha256"]),
            now=EXPIRED,
        )
        await db.rollback()
    assert (await projection_status(path, now=EXPIRED))["request_outcome_unknown"] == 1
    (row,) = await rows(path, "memory_index_outbox")
    assert row["generation"] == claim.generation and row["owner"] == "crashed-process"


@pytest.mark.asyncio
async def test_only_matching_owner_can_record_real_late_response_then_release_uncertainty(
    tmp_path: Path,
) -> None:
    path = await database(tmp_path)
    await put(path)
    (claim,) = await claim_projection_batch(path, "worker", now=NOW)
    assert await mark_projection_dispatched(path, claim, now=NOW)
    assert not await mark_projection_response_received(
        path, replace(claim, owner="impostor"), now=EXPIRED
    )
    assert not await mark_projection_response_received(
        path, replace(claim, generation=2), now=EXPIRED
    )
    assert await claim_projection_batch(path, "other", now=EXPIRED) == []
    assert await mark_projection_response_received(path, claim, now=EXPIRED)
    assert await mark_projection_response_received(path, claim, now=EXPIRED)  # idempotent receipt
    (next_claim,) = await claim_projection_batch(path, "other", now=EXPIRED)
    assert next_claim.generation == 2
    assert not await mark_projection_response_received(path, claim, now=EXPIRED)
    assert await mark_projection_dispatched(path, next_claim, now=EXPIRED)


@pytest.mark.asyncio
async def test_update_and_delete_keep_unknown_request_fence_without_retaining_text(
    tmp_path: Path,
) -> None:
    path = await database(tmp_path)
    await put(path)
    (claim,) = await claim_projection_batch(path, "worker", now=NOW)
    assert await mark_projection_dispatched(path, claim, now=NOW)
    await put(path, "Replacement", revision=LATER)
    assert await claim_projection_batch(path, "other", now=EXPIRED) == []
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("DELETE FROM memory_items WHERE id='m1'")
        await delete_text_views_locked(db, memory_id="m1", now=LATER)
        await db.commit()
    (delete_claim,) = await claim_projection_batch(path, "deleter", now=EXPIRED)
    assert delete_claim.operation == "delete"
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await finish_projection_locked(db, delete_claim, vector=None, now=EXPIRED)
        await db.commit()
    assert not await rows(path, "memory_text_views")
    assert not await rows(path, "memory_embeddings")
    assert (await projection_status(path, now=EXPIRED))["request_outcome_unknown"] == 1
    assert await mark_projection_response_received(path, claim, now=EXPIRED)
    assert (await projection_status(path, now=EXPIRED))["request_outcome_unknown"] == 0
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert not await finish_projection_locked(db, claim, vector=[1.0], now=EXPIRED)


@pytest.mark.asyncio
async def test_dispatch_race_between_different_memories_has_one_winner(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path)
    await put(path, "Other memory", memory_id="m2")
    first, second = await claim_projection_batch(path, "worker", now=NOW)
    results = await asyncio.gather(
        mark_projection_dispatched(path, first, now=NOW),
        mark_projection_dispatched(path, second, now=NOW),
    )
    assert sorted(results) == [False, True]
    assert (await projection_status(path, now=NOW))["request_outcome_unknown"] == 1
    assert await claim_projection_batch(path, "other", now=EXPIRED) == []


@pytest.mark.asyncio
async def test_batch_reservation_and_dispatch_are_atomic_and_preserve_response_order(
    tmp_path: Path,
) -> None:
    path = await database(tmp_path)
    await put(path)
    await put(path, "Other memory", memory_id="m2")
    snapshots = [await snapshot(path, "m2"), await snapshot(path, "m1")]
    corrupted = [snapshots[0], {**snapshots[1], "projection_revision": 99}]
    assert (
        await reserve_projection_batch(
            path, "backfill", snapshots=corrupted, provider="embed-v1", now=NOW
        )
        == []
    )
    assert all(
        row["status"] == "pending" and row["claim_count"] == 0
        for row in await rows(path, "memory_index_outbox")
    )
    claims = await reserve_projection_batch(
        path, "backfill", snapshots=snapshots, provider="embed-v1", now=NOW
    )
    assert [claim.memory_id for claim in claims] == ["m2", "m1"]
    assert not await mark_projection_batch_dispatched(
        path, [claims[0], replace(claims[1], generation=99)], now=NOW
    )
    assert all(
        row["request_state"] == "not_sent" for row in await rows(path, "memory_index_outbox")
    )
    assert await mark_projection_batch_dispatched(path, claims, now=NOW)
    assert await claim_projection_batch(path, "other", now=EXPIRED) == []
    assert (
        await reserve_projection_batch(
            path, "other", snapshots=snapshots, provider="embed-v1", now=EXPIRED
        )
        == []
    )
    assert await mark_projection_batch_response_received(path, claims, now=LATER)
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        for claim, vector in zip(claims, [[1.0, 0.0], [0.0, 1.0]], strict=True):
            assert await finish_projection_locked(db, claim, vector=vector, now=LATER)
        await db.commit()
    stored = {
        row["memory_id"]: json.loads(str(row["vector_json"]))
        for row in await rows(path, "memory_embeddings")
    }
    assert stored == {"m2": [1.0, 0.0], "m1": [0.0, 1.0]}


@pytest.mark.asyncio
async def test_batch_limits_and_duplicate_identities_reject_before_mutation(tmp_path: Path) -> None:
    path = await database(tmp_path)
    await put(path)
    original = await snapshot(path)
    for malformed in ([], [original, original], [original] * 101):
        with pytest.raises(ValueError, match="explicit projection page"):
            await reserve_projection_batch(
                path, "worker", snapshots=malformed, provider="embed-v1", now=NOW
            )
    (claim,) = await claim_projection_batch(path, "worker", now=NOW)
    for malformed_claims in ([], [claim, claim], [claim] * 101):
        with pytest.raises(ValueError, match="request batch"):
            await mark_projection_batch_dispatched(path, malformed_claims, now=NOW)
        with pytest.raises(ValueError, match="request batch"):
            await mark_projection_batch_response_received(path, malformed_claims, now=NOW)
    (current,) = await rows(path, "memory_index_outbox")
    assert current["request_state"] == "not_sent" and current["generation"] == 1
    assert not await mark_projection_response_received(path, claim, now=NOW)


@pytest.mark.asyncio
async def test_received_invalid_result_can_retry_but_never_before_received_marker(
    tmp_path: Path,
) -> None:
    path = await database(tmp_path)
    await put(path)
    (claim,) = await claim_projection_batch(path, "worker", now=NOW)
    assert await mark_projection_dispatched(path, claim, now=NOW)
    assert not await fail_projection_claim(path, claim, error_category="invalid_vector", now=LATER)
    assert await mark_projection_response_received(path, claim, now=LATER)
    assert await fail_projection_claim(path, claim, error_category="invalid_vector", now=LATER)
    assert await retry_known_projection_failure(
        path, memory_id="m1", provider="embed-v1", now=LATER
    )
    (retried,) = await claim_projection_batch(path, "worker", now=LATER)
    assert retried.generation == 2
    assert not await mark_projection_response_received(path, claim, now=LATER)
    assert await mark_projection_dispatched(path, retried, now=LATER)


@pytest.mark.asyncio
async def test_unknown_dispatch_lookup_uses_its_bounded_partial_index(tmp_path: Path) -> None:
    path = await database(tmp_path)
    async with aiosqlite.connect(path) as db:
        plan = await (
            await db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM memory_index_outbox WHERE request_state='in_flight' LIMIT 1"
            )
        ).fetchall()
    assert any("idx_memory_index_inflight" in str(row) for row in plan)
