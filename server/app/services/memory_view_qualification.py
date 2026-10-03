"""Shared read-only qualification of current memory text and source provenance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite

from app.services.memory_search_presentation import _qualified
from app.services.memory_text_fingerprints import _hash, text_view_sha256

_PAYLOAD_FIELDS = (
    "role",
    "language",
    "pipeline_signature",
    "content",
    "summary",
    "source_id",
    "source_sha256",
    "scope",
    "kind",
    "sensitivity",
)


@dataclass(frozen=True)
class SearchMemory:
    row: aiosqlite.Row
    original: tuple[str, str | None] | None
    # Rechecked after presentation I/O; no private provenance is added to API rows.
    view_token: tuple[Any, ...] | None


async def _rows(db: aiosqlite.Connection, sql: str, ids: list[str]) -> list[aiosqlite.Row]:
    async with db.execute(sql, (json.dumps(ids),)) as cursor:
        cursor.row_factory = aiosqlite.Row
        return list(await cursor.fetchall())


async def qualify_search_rows(
    db: aiosqlite.Connection,
    rows: list[aiosqlite.Row],
) -> dict[str, SearchMemory]:
    """Qualify only candidate pages, never hash every unrelated stored memory.

    Absent heads and views retain pre-migration reads. A present head requires its
    complete current view set. Invalid canonical receipts disable native matching;
    existing canonical-text hits still reach the established final qualification
    error instead of silently disappearing.
    """
    if not rows:
        return {}
    ids = [row["id"] for row in rows]
    heads = {
        row["memory_id"]: row
        for row in await _rows(
            db,
            "SELECT * FROM memory_text_heads WHERE memory_id IN (SELECT value FROM json_each(?))",
            ids,
        )
    }
    views: dict[str, list[aiosqlite.Row]] = {}
    for view in await _rows(
        db,
        """SELECT v.* FROM memory_text_views v
        JOIN memory_text_heads h ON h.memory_id=v.memory_id AND h.revision=v.revision
        WHERE v.memory_id IN (SELECT value FROM json_each(?))""",
        ids,
    ):
        views.setdefault(view["memory_id"], []).append(view)
    orphans = {
        row["memory_id"]
        for row in await _rows(
            db,
            """SELECT DISTINCT v.memory_id
        FROM memory_text_views v WHERE v.memory_id IN (SELECT value FROM json_each(?))
        AND NOT EXISTS (SELECT 1 FROM memory_text_heads h WHERE h.memory_id=v.memory_id)""",
            ids,
        )
    }
    decoded = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
        except (ValueError, TypeError):
            metadata = None
        decoded[row["id"]] = {**dict(row), "metadata": metadata}
    receipt_ids: list[str] = []
    for item in decoded.values():
        metadata = item["metadata"]
        receipt_id = metadata.get("canonical_receipt_id") if isinstance(metadata, dict) else None
        if isinstance(receipt_id, str):
            receipt_ids.append(receipt_id)
    source_ids = [head["source_id"] for head in heads.values() if head["source_id"]]
    receipts = (
        {
            row["id"]: row
            for row in await _rows(
                db,
                "SELECT * FROM memory_canonical_receipts WHERE id IN (SELECT value FROM json_each(?))",
                receipt_ids,
            )
        }
        if receipt_ids
        else {}
    )
    sources = (
        {
            row["id"]: row
            for row in await _rows(
                db,
                "SELECT * FROM memory_source_journal WHERE id IN (SELECT value FROM json_each(?))",
                source_ids,
            )
        }
        if source_ids
        else {}
    )
    result = {}
    for row in rows:
        memory_id = row["id"]
        head = heads.get(memory_id)
        if head is None:
            if memory_id not in orphans:
                result[memory_id] = SearchMemory(row, None, None)
            continue
        if head["deleted"] or head["item_revision"] != row["updated_at"]:
            continue
        current = sorted(views.get(memory_id, []), key=lambda view: view["role"] != "original")
        expected_roles = ["original", "canonical"] if head["source_id"] else ["original"]
        if [view["role"] for view in current] != expected_roles:
            continue
        payloads = [{key: view[key] for key in _PAYLOAD_FIELDS} for view in current]
        if any(
            view["id"] != "mtv_" + _hash([memory_id, head["revision"], payload])
            or view["text_sha256"] != text_view_sha256(view["content"], view["summary"])
            or any(view[key] != row[key] for key in ("scope", "kind", "sensitivity"))
            or any(view[key] != head[key] for key in ("source_id", "source_sha256"))
            for view, payload in zip(current, payloads, strict=True)
        ):
            continue
        original, selected = current[0], current[-1]
        if (
            head["view_set_sha256"] != _hash(payloads)
            or head["index_view_id"] != selected["id"]
            or selected["content"] != row["content"]
            or selected["summary"] != row["summary"]
            or original["pipeline_signature"] != "original-v1"
            or not 1 <= len(original["language"]) <= 64
            or original["source_sha256"] != original["text_sha256"]
            or (
                head["source_id"]
                and (selected["language"] != "en" or not selected["pipeline_signature"])
            )
        ):
            continue
        native: tuple[str, str | None] | None = (original["content"], original["summary"])
        if head["source_id"]:
            item = decoded[memory_id]
            metadata = item["metadata"] if isinstance(item["metadata"], dict) else {}
            source = sources.get(head["source_id"])
            receipt_id = metadata.get("canonical_receipt_id")
            receipt = receipts.get(receipt_id) if isinstance(receipt_id, str) else None
            if not _qualified(item, receipt, source):
                native = None
            else:
                assert source is not None
                languages = {
                    metadata[field]["source_language"]
                    for field in ("content", "summary")
                    if source[field] is not None
                }
                language = next(iter(languages)) if len(languages) == 1 else "und"
                if (
                    selected["pipeline_signature"] != metadata.get("normalization_signature")
                    or any(
                        original[key] != source[key]
                        for key in ("content", "summary", "source_sha256")
                    )
                    or original["language"] != language
                ):
                    native = None
        result[memory_id] = SearchMemory(
            row,
            native,
            (
                head["revision"],
                head["item_revision"],
                head["view_set_sha256"],
                head["index_view_id"],
            ),
        )
    return result
