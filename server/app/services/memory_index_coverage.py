"""Explicit read-only coverage inspection, separate from cheap outbox health."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.memory_vectors import MAX_MEMORY_DIMENSIONS, memory_vector
from app.services.memory_view_qualification import qualify_search_rows

VIEW_STATES = (
    "current",
    "missing_vector",
    "embedding_configuration_mismatch",
    "stale_binding",
    "invalid_vector",
    "configuration_missing",
)


class MemoryIndexCoverageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: str = Field(min_length=1, max_length=100)
    kind: str | None = Field(default=None, min_length=1, max_length=100)
    sensitivity: str = Field(default="normal", min_length=1, max_length=50)
    after_id: str | None = Field(default=None, min_length=1, max_length=200)
    limit: int = Field(default=50, ge=1, le=100)

    @field_validator("scope", "kind", "sensitivity", "after_id")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("selection cannot be blank")
        return value


class MemoryCoverageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["original", "canonical"]
    view_id: str = Field(min_length=1, max_length=200)
    status: Literal[
        "current",
        "missing_vector",
        "embedding_configuration_mismatch",
        "stale_binding",
        "invalid_vector",
        "configuration_missing",
    ]


class MemoryCoverageItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    memory_id: str = Field(min_length=1, max_length=200)
    status: Literal["qualified", "unqualified_source"]
    revision: int | None = Field(ge=1)
    views: list[MemoryCoverageView] = Field(max_length=2)


class MemoryCoverageCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    current: int = Field(ge=0, le=200)
    missing_vector: int = Field(ge=0, le=200)
    embedding_configuration_mismatch: int = Field(ge=0, le=200)
    stale_binding: int = Field(ge=0, le=200)
    invalid_vector: int = Field(ge=0, le=200)
    configuration_missing: int = Field(ge=0, le=200)


class MemoryIndexCoveragePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    consistency: Literal["page_snapshot"]
    counts_scope: Literal["page"]
    scope: str = Field(min_length=1, max_length=100)
    kind: str | None = Field(min_length=1, max_length=100)
    sensitivity: str = Field(min_length=1, max_length=50)
    provider_fingerprint: str | None = Field(pattern=r"^memory-v3:[a-f0-9]{64}$")
    provider_readiness: Literal["configured_not_probed", "not_configured"]
    dimensions: int | None = Field(ge=1, le=MAX_MEMORY_DIMENSIONS)
    dimension_check: Literal["provider_declared", "stored_vector_only", "not_configured"]
    page_status: Literal["empty", "unqualified", "configuration_missing", "incomplete", "covered"]
    scanned_memories: int = Field(ge=0, le=100)
    qualified_memories: int = Field(ge=0, le=100)
    unqualified_memories: int = Field(ge=0, le=100)
    expected_views: int = Field(ge=0, le=200)
    covered_views: int = Field(ge=0, le=200)
    views_by_status: MemoryCoverageCounts
    items: list[MemoryCoverageItem] = Field(max_length=100)
    has_more: bool
    next_after_id: str | None = Field(min_length=1, max_length=200)
    covers_entire_selection: bool


class MemoryIndexCoverageError(Exception):
    def __init__(self, code: str, status_code: int):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _view_status(row: aiosqlite.Row, provider: str | None, dimensions: int | None) -> str:
    if provider is None:
        return "configuration_missing"
    if row["vector_provider"] is None:
        return (
            "embedding_configuration_mismatch"
            if row["other_provider_present"]
            else "missing_vector"
        )
    if any(
        row["vector_" + field] != row[field]
        for field in (
            "source_id",
            "source_sha256",
            "view_sha256",
            "pipeline_signature",
            "item_revision",
        )
    ):
        return "stale_binding"
    if dimensions is not None and row["vector_dimensions"] != dimensions:
        return "embedding_configuration_mismatch"
    try:
        vector = memory_vector(json.loads(row["vector_json"]), dimensions)
    except (ValueError, TypeError, RecursionError):
        vector = None
    if vector is None or row["vector_dimensions"] != len(vector):
        return "invalid_vector"
    return "current"


async def inspect_index_coverage(
    db_path: Path,
    request: MemoryIndexCoverageRequest,
    *,
    provider: str | None,
    dimensions: int | None,
    assert_current: Callable[[], None],
) -> dict[str, Any]:
    """Inspect a single read snapshot with no model calls or implicit repair.

    Scope/kind/sensitivity precede LIMIT. Separate cursor pages do not share a
    transaction and must never be combined into a purported global snapshot.
    Historical projections, legacy cache and outbox completions prove no coverage.
    """
    assert_current()
    if dimensions is not None and (
        type(dimensions) is not int or not 1 <= dimensions <= MAX_MEMORY_DIMENSIONS
    ):
        raise MemoryIndexCoverageError("memory_index_configuration_invalid", 503)
    try:
        async with aiosqlite.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            rows = list(
                await (
                    await db.execute(
                        """SELECT * FROM memory_items WHERE scope=? AND sensitivity=?
                AND (? IS NULL OR kind=?) AND id>? ORDER BY id LIMIT ?""",
                        (
                            request.scope,
                            request.sensitivity,
                            request.kind,
                            request.kind,
                            request.after_id or "",
                            request.limit + 1,
                        ),
                    )
                ).fetchall()
            )
            page = rows[: request.limit]
            qualified = await qualify_search_rows(db, page)
            ids = [
                row["id"]
                for row in page
                if (item := qualified.get(row["id"])) is not None
                and item.view_token is not None
                and item.original is not None
            ]
            # Two qualified views per memory at most, one exact-provider row
            # per view. EXISTS never materializes historical or foreign vectors.
            views = list(
                await (
                    await db.execute(
                        """SELECT v.id,v.memory_id,v.role,v.source_id,v.source_sha256,
                v.text_sha256 AS view_sha256,v.pipeline_signature,h.item_revision,
                e.provider AS vector_provider,e.source_id AS vector_source_id,
                e.source_sha256 AS vector_source_sha256,e.view_sha256 AS vector_view_sha256,
                e.pipeline_signature AS vector_pipeline_signature,
                e.item_revision AS vector_item_revision,e.dimensions AS vector_dimensions,e.vector_json,
                EXISTS(SELECT 1 FROM memory_view_embeddings other
                    WHERE other.memory_id=v.memory_id AND other.revision=v.revision
                    AND other.view_id=v.id AND other.provider<>?) AS other_provider_present
                FROM memory_text_views v
                JOIN memory_text_heads h ON h.memory_id=v.memory_id AND h.revision=v.revision
                LEFT JOIN memory_view_embeddings e ON e.memory_id=v.memory_id
                    AND e.revision=v.revision AND e.view_id=v.id AND e.provider=?
                WHERE v.memory_id IN (SELECT value FROM json_each(?))
                ORDER BY v.memory_id,v.role,v.id""",
                        (provider, provider, json.dumps(ids)),
                    )
                ).fetchall()
            )
    except (aiosqlite.Error, RecursionError) as exc:
        raise MemoryIndexCoverageError("memory_index_unavailable", 503) from exc
    assert_current()
    by_memory: dict[str, list[dict[str, str]]] = {}
    counts = dict.fromkeys(VIEW_STATES, 0)
    for view in views:
        status = _view_status(view, provider, dimensions)
        counts[status] += 1
        by_memory.setdefault(view["memory_id"], []).append(
            {"role": view["role"], "view_id": view["id"], "status": status}
        )
    items = []
    unqualified = 0
    for row in page:
        item = qualified.get(row["id"])
        current_views = by_memory.get(row["id"], [])
        accepted = item is not None and item.view_token is not None and bool(current_views)
        if not accepted:
            unqualified += 1
        items.append(
            {
                "memory_id": row["id"],
                "status": "qualified" if accepted else "unqualified_source",
                "revision": item.view_token[0] if accepted and item and item.view_token else None,
                "views": current_views if accepted else [],
            }
        )
    expected = sum(counts.values())
    has_more = len(rows) > request.limit
    page_status = (
        "empty"
        if not page
        else "unqualified"
        if unqualified
        else "configuration_missing"
        if provider is None
        else "incomplete"
        if counts["current"] != expected
        else "covered"
    )
    return {
        "consistency": "page_snapshot",
        "counts_scope": "page",
        "scope": request.scope,
        "kind": request.kind,
        "sensitivity": request.sensitivity,
        "provider_fingerprint": provider,
        "provider_readiness": "configured_not_probed" if provider is not None else "not_configured",
        "dimensions": dimensions,
        "dimension_check": "provider_declared"
        if dimensions is not None
        else "stored_vector_only"
        if provider is not None
        else "not_configured",
        "page_status": page_status,
        "scanned_memories": len(page),
        "qualified_memories": len(page) - unqualified,
        "unqualified_memories": unqualified,
        "expected_views": expected,
        "covered_views": counts["current"],
        "views_by_status": counts,
        "items": items,
        "has_more": has_more,
        "next_after_id": page[-1]["id"] if has_more else None,
        "covers_entire_selection": request.after_id is None and not has_more,
    }
