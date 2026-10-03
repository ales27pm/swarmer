"""Read-only qualification and temporary display of canonical memory search hits.

The final read is a snapshot, not a lease beyond return. Canonical text and its
provenance remain unchanged; translation never becomes another memory record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import aiosqlite
from pydantic import ValidationError

from app.services.memory_normalization import (
    MemoryNormalizationError,
    MemoryNormalizationResult,
    canonical_text_sha256,
)
from app.services.memory_presentation import (
    MAX_PRESENTATION_OUTPUT_BYTES,
    MAX_PRESENTATION_SOURCE_BYTES,
    PRESENTATION_POLICY_SHA256,
    PRESENTATION_POLICY_VERSION,
    MemoryPresentationBatch,
    MemoryPresentationResult,
    MemoryPresentationSource,
    PresentationRecheck,
)
from app.services.model_request_execution import ModelExecutionControlError, ModelRequestExecutor


class MemoryPresenter(Protocol):
    @property
    def presentation_signature(self) -> str: ...

    async def present(
        self,
        batch: MemoryPresentationBatch,
        *,
        recheck_sources: PresentationRecheck | None = None,
        model_executor: ModelRequestExecutor | None = None,
    ) -> MemoryPresentationResult: ...


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _qualified(
    item: dict[str, Any], receipt: aiosqlite.Row | None, source: aiosqlite.Row | None
) -> bool:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict) or not receipt or not source:
        return False
    try:
        original_hash = _digest({"content": source["content"], "summary": source["summary"]})
        if (
            metadata.get("canonical_language") != "en"
            or receipt["status"] != "accepted"
            or receipt["memory_id"] != item["id"]
            or receipt["id"] != metadata.get("canonical_receipt_id")
            or receipt["source_id"] != source["id"]
            or source["id"] != metadata.get("source_id")
            or receipt["normalization_signature"] != metadata.get("normalization_signature")
            or receipt["expected_revision"] != metadata.get("supersedes_revision")
            or source["source_sha256"] != metadata.get("source_sha256")
            or source["source_sha256"] != original_hash
            or source["id"]
            != "msrc_"
            + _digest([source["scope"], source["kind"], source["sensitivity"], original_hash])
            or any(source[key] != item[key] for key in ("scope", "kind", "sensitivity"))
            or json.loads(receipt["result_json"])
            != {"content": item["content"], "summary": item["summary"]}
        ):
            return False
        for field in ("content", "summary"):
            if item[field] is None:
                if metadata.get(field) is not None or source[field] is not None:
                    return False
                continue
            unit = MemoryNormalizationResult.model_validate(
                {**metadata[field], "canonical_text": item[field]}
            )
            if (
                unit.scope != item["scope"]
                or unit.applicability_sha256
                != _digest({key: item[key] for key in ("sensitivity", "confidence")})
                or unit.expected_memory_revision != receipt["expected_revision"]
                or unit.kind != item["kind"]
                or unit.source_id != source["id"] + ":" + field
                or unit.source_sha256 != canonical_text_sha256(source[field])
                or unit.canonical_sha256 != canonical_text_sha256(item[field])
                or unit.normalization_signature != receipt["normalization_signature"]
                or unit.normalization_policy_version != metadata.get("normalization_policy_version")
                or not unit.source_revalidated
            ):
                return False
    except (ValidationError, ValueError, TypeError, KeyError, UnicodeError):
        return False
    return True


async def _recheck(
    db_path: Path, items: list[dict[str, Any]], *, initial: bool
) -> dict[str, dict[str, Any]]:
    sources = {}
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        for snapshot in items:
            row = await (
                await db.execute("SELECT * FROM memory_items WHERE id=?", (snapshot["id"],))
            ).fetchone()
            if row is None:
                raise MemoryNormalizationError("source_conflict", "memory_search_source_changed")
            item = dict(row)
            raw_metadata = item.pop("metadata_json")
            try:
                item["metadata"] = json.loads(raw_metadata) if raw_metadata else None
            except (ValueError, TypeError) as exc:
                raise MemoryNormalizationError(
                    "source_conflict", "memory_metadata_changed"
                ) from exc
            item["pinned"] = bool(item["pinned"])
            if any(snapshot.get(key) != value for key, value in item.items()):
                raise MemoryNormalizationError("source_conflict", "memory_search_source_changed")
            metadata = item["metadata"] if isinstance(item["metadata"], dict) else {}
            receipt = await (
                await db.execute(
                    "SELECT * FROM memory_canonical_receipts WHERE id=?",
                    (metadata.get("canonical_receipt_id"),),
                )
            ).fetchone()
            source = await (
                await db.execute(
                    "SELECT * FROM memory_source_journal WHERE id=?", (metadata.get("source_id"),)
                )
            ).fetchone()
            if not _qualified(item, receipt, source):
                raise MemoryNormalizationError(
                    "unavailable" if initial else "source_conflict",
                    "canonical_memory_unqualified" if initial else "canonical_memory_changed",
                )
            assert source is not None
            sources[item["id"]] = dict(source)
    return sources


def _presentation_batch(items: list[dict[str, Any]]) -> MemoryPresentationBatch:
    batch = MemoryPresentationBatch(
        items=[
            MemoryPresentationSource(
                memory_id=item["id"],
                scope=item["scope"],
                source_revision=item["updated_at"],
                canonical_sha256=canonical_text_sha256(item["content"]),
                content=item["content"],
                summary=item["summary"],
                summary_sha256=canonical_text_sha256(item["summary"])
                if item["summary"] is not None
                else None,
            )
            for item in items
        ]
    )
    if len({item.memory_id for item in batch.items}) != len(batch.items):
        raise MemoryNormalizationError("invalid", "duplicate_presentation_memory")
    if (
        sum(
            len(value.encode("utf-8"))
            for item in batch.items
            for value in (item.content, item.summary)
            if value is not None
        )
        > MAX_PRESENTATION_SOURCE_BYTES
    ):
        raise MemoryNormalizationError("invalid", "presentation_source_budget_exceeded")
    return batch


async def finalize_memory_search(
    db_path: Path,
    items: list[dict[str, Any]],
    *,
    french: bool,
    get_presenter: Callable[[], MemoryPresenter | None],
    gate: asyncio.Lock,
    timeout_seconds: float,
    assert_current: Callable[[], None],
    model_executor: ModelRequestExecutor | None = None,
) -> list[dict[str, Any]]:
    """Qualify the authorized selected snapshots and optionally display them in FR."""
    assert_current()
    if not items:
        return []
    sources = await _recheck(db_path, items, initial=True)
    assert_current()
    if not french:
        return items
    try:
        # Validate the entire selection before splitting it, so mixed batches
        # retain the same item and input byte limits as translated batches.
        _presentation_batch(items)
        original = {}
        translated = []
        for item in items:
            source = sources[item["id"]]
            metadata = item["metadata"]
            if not all(
                metadata[field]["source_language"] == "fr"
                for field in ("content", "summary")
                if source[field] is not None
            ):
                translated.append(item)
                continue
            # The language declaration is trusted persisted normalization
            # metadata; _recheck binds each exact original to its accepted
            # receipt, scope, revision and current English canonical text.
            original[item["id"]] = {
                **item,
                "presentation": {
                    "mode": "original",
                    "language": "fr",
                    "content": source["content"],
                    "summary": source["summary"],
                    "canonical_sha256": canonical_text_sha256(item["content"]),
                    "summary_sha256": canonical_text_sha256(item["summary"])
                    if item["summary"] is not None
                    else None,
                    "source_revision": item["updated_at"],
                    "validation_status": "source_preserved",
                    "temporary": True,
                    "grants_authority": False,
                    "source_id": source["id"],
                    "source_sha256": source["source_sha256"],
                    "canonical_receipt_id": metadata["canonical_receipt_id"],
                },
            }
        if translated:
            rendered = await _translate_memory_search(
                db_path,
                translated,
                all_items=items,
                get_presenter=get_presenter,
                gate=gate,
                timeout_seconds=timeout_seconds,
                assert_current=assert_current,
                model_executor=model_executor,
            )
            original.update((item["id"], item) for item in rendered)
        output = [original[item["id"]] for item in items]
        if (
            sum(
                len(value.encode("utf-8"))
                for item in output
                for value in (item["presentation"]["content"], item["presentation"]["summary"])
                if value is not None
            )
            > MAX_PRESENTATION_OUTPUT_BYTES
        ):
            raise MemoryNormalizationError("invalid", "presentation_output_budget_exceeded")
        # Translation already finishes by rechecking the whole mixed selection
        # and its presenter identity. Do not insert an unchecked await after it.
        if not translated:
            await _recheck(db_path, items, initial=False)
        assert_current()
        return output
    except (ValidationError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise MemoryNormalizationError("invalid", "invalid_presentation_receipt") from exc


async def _translate_memory_search(
    db_path: Path,
    items: list[dict[str, Any]],
    *,
    all_items: list[dict[str, Any]],
    get_presenter: Callable[[], MemoryPresenter | None],
    gate: asyncio.Lock,
    timeout_seconds: float,
    assert_current: Callable[[], None],
    model_executor: ModelRequestExecutor | None,
) -> list[dict[str, Any]]:
    presenter = get_presenter()
    if presenter is None:
        raise MemoryNormalizationError("unavailable", "presenter_not_configured")
    signature = presenter.presentation_signature

    def assert_providers() -> None:
        assert_current()
        if get_presenter() is not presenter or presenter.presentation_signature != signature:
            raise MemoryNormalizationError("source_conflict", "memory_presenter_changed")

    try:
        batch = _presentation_batch(items)
        batch_hash = _digest(batch.model_dump())

        async def recheck(candidate: MemoryPresentationBatch) -> bool:
            assert_providers()
            if _digest(candidate.model_dump()) != batch_hash:
                return False
            await _recheck(db_path, all_items, initial=False)
            assert_providers()
            return True

        if gate.locked():
            raise MemoryNormalizationError("unavailable", "normalizer_busy")
        async with gate:
            async with asyncio.timeout(timeout_seconds):
                result = (
                    await presenter.present(batch, recheck_sources=recheck)
                    if model_executor is None
                    else await presenter.present(
                        batch, recheck_sources=recheck, model_executor=model_executor
                    )
                )
                result = MemoryPresentationResult.model_validate(result.model_dump())
                assert_providers()
                if (
                    result.presentation_signature != signature
                    or result.presentation_policy_version != PRESENTATION_POLICY_VERSION
                    or result.presentation_policy_sha256 != PRESENTATION_POLICY_SHA256
                    or result.source_batch_sha256 != batch_hash
                    or not result.sources_revalidated
                    or len(result.items) != len(batch.items)
                    or len({item.memory_id for item in result.items}) != len(result.items)
                ):
                    raise MemoryNormalizationError("invalid", "presentation_receipt_mismatch")
                presented = {item.memory_id: item for item in result.items}
                output = []
                total_bytes = 0
                for snapshot, source in zip(items, batch.items, strict=True):
                    shown = presented.get(source.memory_id)
                    if (
                        shown is None
                        or any(
                            getattr(shown, key) != getattr(source, key)
                            for key in (
                                "scope",
                                "source_revision",
                                "canonical_sha256",
                                "summary_sha256",
                            )
                        )
                        or not shown.display_text.strip()
                        or (shown.display_summary is None) != (source.summary is None)
                    ):
                        raise MemoryNormalizationError("invalid", "presentation_item_mismatch")
                    total_bytes += len(shown.display_text.encode("utf-8")) + len(
                        (shown.display_summary or "").encode("utf-8")
                    )
                    if total_bytes > MAX_PRESENTATION_OUTPUT_BYTES:
                        raise MemoryNormalizationError(
                            "invalid", "presentation_output_budget_exceeded"
                        )
                    output.append(
                        {
                            **snapshot,
                            "presentation": {
                                "language": "fr",
                                "content": shown.display_text,
                                "summary": shown.display_summary,
                                "canonical_sha256": source.canonical_sha256,
                                "summary_sha256": source.summary_sha256,
                                "source_revision": source.source_revision,
                                "validation_status": "model_reviewed",
                                "temporary": True,
                                "grants_authority": False,
                            },
                        }
                    )
                await _recheck(db_path, all_items, initial=False)
                assert_providers()
                return output
    except (ModelExecutionControlError, MemoryNormalizationError):
        raise
    except TimeoutError as exc:
        raise MemoryNormalizationError("unavailable", "presentation_deadline_exceeded") from exc
    except (ValidationError, ValueError, TypeError, AttributeError, UnicodeError) as exc:
        raise MemoryNormalizationError("invalid", "invalid_presentation_receipt") from exc
    except Exception as exc:
        raise MemoryNormalizationError("unavailable", "presenter_unavailable") from exc
