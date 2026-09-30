"""Opt-in canonical English writes; source journals are separate from retrieval."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.services.memory_normalization import MemoryNormalizationResult, MemoryNormalizationSource


class MemoryNormalizer(Protocol):
    normalization_signature: str

    async def normalize(
        self,
        source: MemoryNormalizationSource,
        *,
        recheck_source: Callable[[MemoryNormalizationSource], Awaitable[bool]] | None = None,
    ) -> MemoryNormalizationResult: ...


import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
from pydantic import ValidationError

from app.models import MemoryCreate, MemoryUpdate
from app.services.audit_log import append_audit_event
from app.services.memory_normalization import (
    MAX_SOURCE_BYTES,
    POLICY_SHA256,
    POLICY_VERSION,
    MemoryNormalizationError,
    canonical_text_sha256,
    normalization_identity,
)

MEMORY_CANONICAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_source_journal (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    source_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_canonical_receipts (
    id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    intent_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    previous_source_id TEXT,
    memory_id TEXT NOT NULL,
    expected_revision TEXT,
    normalization_signature TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','failed','deleted')),
    lease_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    result_json TEXT,
    error_category TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_canonical_intent ON memory_canonical_receipts(intent_key,status);
CREATE INDEX IF NOT EXISTS idx_memory_canonical_item ON memory_canonical_receipts(memory_id,status);
"""


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _revision(item: dict[str, Any]) -> str:
    return _hash(item)


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(item.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _source_record(content: str, summary: str | None, attributes: dict[str, Any]) -> dict[str, Any]:
    text_hash = _hash({"content": content, "summary": summary})
    return {
        "id": "msrc_"
        + _hash([attributes["scope"], attributes["kind"], attributes["sensitivity"], text_hash]),
        "scope": attributes["scope"],
        "kind": attributes["kind"],
        "sensitivity": attributes["sensitivity"],
        "content": content,
        "summary": summary,
        "source_sha256": text_hash,
    }


async def _read_item(db: aiosqlite.Connection, memory_id: str) -> dict[str, Any] | None:
    db.row_factory = aiosqlite.Row
    row = await (await db.execute("SELECT * FROM memory_items WHERE id=?", (memory_id,))).fetchone()
    return dict(row) if row else None


async def _journal(db: aiosqlite.Connection, source: dict[str, Any], now: str) -> None:
    await db.execute(
        """INSERT OR IGNORE INTO memory_source_journal
        (id,scope,kind,sensitivity,content,summary,source_sha256,created_at) VALUES(?,?,?,?,?,?,?,?)""",
        tuple(
            source[key]
            for key in ("id", "scope", "kind", "sensitivity", "content", "summary", "source_sha256")
        )
        + (now,),
    )
    row = await (
        await db.execute("SELECT * FROM memory_source_journal WHERE id=?", (source["id"],))
    ).fetchone()
    if row is None or any(row[key] != value for key, value in source.items()):
        raise MemoryNormalizationError("source_conflict", "source_journal_changed")


async def forget_canonical_sources(db: aiosqlite.Connection, memory_id: str) -> None:
    """Called inside the user's deletion transaction; no plaintext tombstones."""
    rows = await (
        await db.execute(
            "SELECT source_id,previous_source_id FROM memory_canonical_receipts WHERE memory_id=?",
            (memory_id,),
        )
    ).fetchall()
    source_ids = {str(value) for row in rows for value in row if value}
    await db.execute(
        """UPDATE memory_canonical_receipts SET status='deleted',result_json=NULL,
        error_category=NULL,lease_token=?,updated_at=? WHERE memory_id=?""",
        (uuid4().hex, datetime.now(UTC).isoformat(), memory_id),
    )
    for source_id in source_ids:
        await db.execute(
            """DELETE FROM memory_source_journal WHERE id=? AND NOT EXISTS(
            SELECT 1 FROM memory_canonical_receipts WHERE status<>'deleted'
            AND (source_id=? OR previous_source_id=?))""",
            (source_id, source_id, source_id),
        )


class MemoryCanonicalStore:
    def __init__(
        self,
        db_path: Path,
        provider: MemoryNormalizer | None,
        *,
        current_provider: Callable[[], MemoryNormalizer | None],
        gate: asyncio.Lock,
        timeout_seconds: float,
    ) -> None:
        self.db_path, self.provider = db_path, provider
        self.current_provider, self.gate, self.timeout_seconds = (
            current_provider,
            gate,
            timeout_seconds,
        )
        self.signature = provider.normalization_signature if provider else ""

    def _provider_current(self) -> bool:
        return (
            self.provider is not None
            and self.current_provider() is self.provider
            and self.provider.normalization_signature == self.signature
        )

    async def write(
        self,
        request: MemoryCreate | MemoryUpdate,
        actor_id: str,
        *,
        memory_id: str | None = None,
    ) -> tuple[str, bool] | None:
        existing: dict[str, Any] | None = None
        async with aiosqlite.connect(self.db_path) as db:
            if memory_id is not None:
                existing = await _read_item(db, memory_id)
                if existing is None:
                    return None
        if isinstance(request, MemoryCreate):
            attributes = request.model_dump(exclude={"content", "summary"})
            content, summary = request.content, request.summary
        else:
            assert existing is not None
            attributes = {
                key: existing[key]
                for key in ("scope", "kind", "sensitivity", "confidence", "pinned")
            }
            attributes["pinned"] = (
                bool(existing["pinned"]) if request.pinned is None else request.pinned
            )
            content = request.content if request.content is not None else existing["content"]
            summary = (
                request.summary
                if "summary" in request.model_fields_set
                else None
                if content != existing["content"]
                else existing["summary"]
            )
        if self.provider is None:
            raise MemoryNormalizationError("unavailable", "provider_not_configured")
        for unit in (content, summary):
            if unit is not None and (
                not unit.strip() or len(unit.encode("utf-8")) > MAX_SOURCE_BYTES
            ):
                raise MemoryNormalizationError("invalid", "source_budget_exceeded")
        source = _source_record(content, summary, attributes)
        expected = _revision(existing) if existing else None
        intent_attributes = {
            key: value for key, value in attributes.items() if memory_id is None or key != "pinned"
        }
        intent_key = _hash([memory_id or "create", source["id"], intent_attributes, self.signature])
        request_key = _hash([intent_key, expected])
        # Reading an accepted receipt is allowed without consuming the model gate.
        replay = await self._replay(intent_key, source, attributes)
        if replay:
            return replay, False
        if self.gate.locked():
            raise MemoryNormalizationError("unavailable", "normalizer_busy")
        async with self.gate:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    return await self._write_locked(
                        content,
                        summary,
                        source,
                        attributes,
                        existing,
                        expected,
                        intent_key,
                        request_key,
                        actor_id,
                        memory_id,
                    )
            except TimeoutError as exc:
                raise MemoryNormalizationError("unavailable", "deadline_exceeded") from exc

    async def _replay(
        self,
        intent_key: str,
        source: dict[str, Any],
        attributes: dict[str, Any],
    ) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            rows = await (
                await db.execute(
                    "SELECT * FROM memory_canonical_receipts WHERE intent_key=? AND status='accepted' ORDER BY created_at DESC LIMIT 20",
                    (intent_key,),
                )
            ).fetchall()
            for row in rows:
                item = await _read_item(db, row["memory_id"])
                if item is None:
                    continue
                try:
                    result = json.loads(row["result_json"])
                    if not isinstance(result, dict) or set(result) != {"content", "summary"}:
                        raise ValueError
                except (TypeError, ValueError) as exc:
                    raise MemoryNormalizationError(
                        "source_conflict", "accepted_receipt_invalid"
                    ) from exc
                original = await (
                    await db.execute(
                        "SELECT * FROM memory_source_journal WHERE id=?", (row["source_id"],)
                    )
                ).fetchone()
                if original is None or any(original[key] != value for key, value in source.items()):
                    raise MemoryNormalizationError("source_conflict", "source_journal_changed")
                if (
                    item["content"] == result["content"]
                    and item["summary"] == result["summary"]
                    and _metadata(item).get("canonical_receipt_id") == row["id"]
                    and all(item[key] == value for key, value in attributes.items())
                    and row["source_id"] == source["id"]
                ):
                    if not self._provider_current():
                        raise MemoryNormalizationError("source_conflict", "normalizer_changed")
                    return str(item["id"])
        return None

    async def _write_locked(
        self,
        content: str,
        summary: str | None,
        source: dict[str, Any],
        attributes: dict[str, Any],
        existing: dict[str, Any] | None,
        expected: str | None,
        intent_key: str,
        request_key: str,
        actor_id: str,
        memory_id: str | None,
    ) -> tuple[str, bool]:
        now = datetime.now(UTC)
        target = memory_id or "mem_" + uuid4().hex
        receipt_id, lease = "mnorm_" + uuid4().hex, uuid4().hex
        previous = (
            _source_record(existing["content"], existing["summary"], existing)
            if existing and _metadata(existing).get("canonical_language") != "en"
            else None
        )
        previous_id = (
            previous["id"]
            if previous
            else _metadata(existing).get("source_id")
            if existing
            else None
        )
        # Validate typed sources before storing any source or reserving a model call.
        units: list[MemoryNormalizationSource] = []
        try:
            for name, text in (("content", content), ("summary", summary)):
                if text is not None:
                    units.append(
                        MemoryNormalizationSource(
                            scope=attributes["scope"],
                            kind=attributes["kind"],
                            applicability_sha256=_hash(
                                {key: attributes[key] for key in ("sensitivity", "confidence")}
                            ),
                            source_id=source["id"] + ":" + name,
                            source_version="1",
                            source_sha256=canonical_text_sha256(text),
                            text=text,
                            expected_memory_revision=expected,
                        )
                    )
        except ValidationError as exc:
            raise MemoryNormalizationError("invalid", "unsupported_memory_source") from exc
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if existing and _revision(await _read_item(db, target) or {}) != expected:
                raise MemoryNormalizationError("source_conflict", "memory_changed")
            row = await (
                await db.execute(
                    "SELECT * FROM memory_canonical_receipts WHERE request_key=?", (request_key,)
                )
            ).fetchone()
            if row:
                if row["status"] == "accepted":
                    raise MemoryNormalizationError("source_conflict", "accepted_memory_changed")
                if row["status"] == "pending" and row["expires_at"] > now.isoformat():
                    raise MemoryNormalizationError("source_conflict", "normalization_in_progress")
                receipt_id = str(row["id"])
            await _journal(db, source, now.isoformat())
            if previous:
                await _journal(db, previous, now.isoformat())
            await db.execute(
                """INSERT INTO memory_canonical_receipts
                (id,request_key,intent_key,source_id,previous_source_id,memory_id,expected_revision,
                normalization_signature,status,lease_token,expires_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?,?) ON CONFLICT(request_key) DO UPDATE SET
                status='pending',lease_token=excluded.lease_token,expires_at=excluded.expires_at,
                memory_id=excluded.memory_id,result_json=NULL,error_category=NULL,updated_at=excluded.updated_at""",
                (
                    receipt_id,
                    request_key,
                    intent_key,
                    source["id"],
                    previous_id,
                    target,
                    expected,
                    self.signature,
                    lease,
                    (now + timedelta(seconds=self.timeout_seconds + 5)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            if not self._provider_current():
                raise MemoryNormalizationError("source_conflict", "normalizer_changed")
            await db.commit()

        async def recheck(_: MemoryNormalizationSource) -> bool:
            if not self._provider_current():
                return False
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                return await self._current(db, receipt_id, lease, source, target, expected)

        try:
            results: list[MemoryNormalizationResult] = []
            assert self.provider is not None
            for unit in units:
                result = await self.provider.normalize(unit, recheck_source=recheck)
                self._validate_result(unit, result)
                if unit.source_id.endswith(":summary") and len(result.canonical_text) > 2_000:
                    raise MemoryNormalizationError("invalid", "canonical_summary_budget_exceeded")
                results.append(result)
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                if not await self._current(db, receipt_id, lease, source, target, expected):
                    raise MemoryNormalizationError("source_conflict", "memory_changed")
                accepted_at = datetime.now(UTC).isoformat()
                metadata = {
                    "canonical_language": "en",
                    "canonical_receipt_id": receipt_id,
                    "source_id": source["id"],
                    "source_sha256": source["source_sha256"],
                    "normalization_policy_version": POLICY_VERSION,
                    "normalization_signature": self.signature,
                    "content": results[0].model_dump(exclude={"canonical_text"}),
                    "summary": results[1].model_dump(exclude={"canonical_text"})
                    if len(results) > 1
                    else None,
                    "supersedes_revision": expected,
                }
                canonical = {
                    "content": results[0].canonical_text,
                    "summary": results[1].canonical_text if len(results) > 1 else None,
                }
                if existing:
                    await db.execute(
                        "UPDATE memory_items SET content=?,summary=?,pinned=?,metadata_json=?,updated_at=? WHERE id=?",
                        (
                            canonical["content"],
                            canonical["summary"],
                            int(attributes["pinned"]),
                            _json(metadata),
                            accepted_at,
                            target,
                        ),
                    )
                    await db.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (target,))
                else:
                    await db.execute(
                        """INSERT INTO memory_items
                        (id,scope,kind,content,summary,sensitivity,confidence,pinned,metadata_json,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            target,
                            attributes["scope"],
                            attributes["kind"],
                            canonical["content"],
                            canonical["summary"],
                            attributes["sensitivity"],
                            attributes["confidence"],
                            int(attributes["pinned"]),
                            _json(metadata),
                            accepted_at,
                            accepted_at,
                        ),
                    )
                await db.execute(
                    "UPDATE memory_canonical_receipts SET status='accepted',result_json=?,updated_at=? WHERE id=? AND lease_token=?",
                    (_json(canonical), accepted_at, receipt_id, lease),
                )
                await append_audit_event(
                    db,
                    "memory.updated" if existing else "memory.remembered",
                    {
                        "memory_id": target,
                        "scope": attributes["scope"],
                        "canonical_language": "en",
                        "normalization_receipt_id": receipt_id,
                    },
                    actor_type="device",
                    actor_id=actor_id,
                    created_at=accepted_at,
                )
                if not self._provider_current():
                    raise MemoryNormalizationError("source_conflict", "normalizer_changed")
                await db.commit()
            return target, True
        except BaseException as exc:
            category = exc.category if isinstance(exc, MemoryNormalizationError) else "unavailable"
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE memory_canonical_receipts SET status='failed',error_category=?,updated_at=? WHERE id=? AND lease_token=? AND status='pending'",
                    (category, datetime.now(UTC).isoformat(), receipt_id, lease),
                )
                await db.commit()
            if isinstance(exc, (MemoryNormalizationError, asyncio.CancelledError)):
                raise
            raise MemoryNormalizationError("unavailable", "normalizer_failed") from exc

    async def _current(
        self,
        db: aiosqlite.Connection,
        receipt_id: str,
        lease: str,
        source: dict[str, Any],
        target: str,
        expected: str | None,
    ) -> bool:
        row = await (
            await db.execute("SELECT * FROM memory_canonical_receipts WHERE id=?", (receipt_id,))
        ).fetchone()
        current_source = await (
            await db.execute("SELECT * FROM memory_source_journal WHERE id=?", (source["id"],))
        ).fetchone()
        item = await _read_item(db, target)
        return bool(
            row
            and row["status"] == "pending"
            and row["lease_token"] == lease
            and current_source
            and all(current_source[key] == value for key, value in source.items())
            and (
                (expected is None and item is None)
                or (item is not None and _revision(item) == expected)
            )
            and self._provider_current()
        )

    def _validate_result(
        self, source: MemoryNormalizationSource, result: MemoryNormalizationResult
    ) -> None:
        try:
            result = MemoryNormalizationResult.model_validate(result.model_dump())
        except (ValidationError, AttributeError, TypeError) as exc:
            raise MemoryNormalizationError("invalid", "invalid_normalization_receipt") from exc
        expected = {
            key: getattr(source, key)
            for key in (
                "scope",
                "kind",
                "applicability_sha256",
                "source_id",
                "source_version",
                "source_sha256",
                "expected_memory_revision",
            )
        }
        expected.update(
            normalization_signature=self.signature,
            normalization_policy_version=POLICY_VERSION,
            normalization_policy_sha256=POLICY_SHA256,
            deduplication_identity=normalization_identity(source, self.signature),
            source_revalidated=True,
        )
        if (
            any(getattr(result, key) != value for key, value in expected.items())
            or not result.canonical_text.strip()
            or len(result.canonical_text.encode("utf-8")) > 32_000
            or result.canonical_sha256 != canonical_text_sha256(result.canonical_text)
        ):
            raise MemoryNormalizationError("invalid", "normalization_receipt_mismatch")
