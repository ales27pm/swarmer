"""Validation and space identity for rebuildable memory embeddings."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.embedding_service import EmbeddingService

MAX_MEMORY_DIMENSIONS = 8_192


def embedding_identity(
    provider: EmbeddingService | None,
    revision: str | None = None,
    *,
    query_prefix: str = "",
    document_prefix: str = "",
    input_format: str = "memory-content-summary-v1",
) -> str:
    # No type/repr/object address: wrappers and process restarts retain the same
    # identity when they expose the same provider contract. Never expose URLs or
    # credentials in cache keys, reports, or audit events.
    identity = {
        "format": 3,
        "provider": getattr(provider, "provider_name", None),
        "origin": str(getattr(provider, "base_url", "")).rstrip("/"),
        "model": getattr(provider, "model", None),
        "revision": revision,
        "provider_revision": getattr(provider, "model_revision", None),
        "dimensions": getattr(provider, "dimensions", None),
        # Provider metadata describes its actual adapter contract, when known.
        # Unknown normalization/revision stays unknown: a mutable remote alias
        # cannot be pinned or verified by hashing its name.
        "normalization": getattr(provider, "normalization", None),
        "chunker_version": getattr(provider, "chunker_version", None),
        "preprocessing_version": getattr(provider, "preprocessing_version", None),
        "provider_query_prefix": getattr(provider, "query_prefix", ""),
        "provider_document_prefix": getattr(provider, "document_prefix", ""),
        # Reader-level transformations are recorded only when applied. This
        # function never modifies input or assumes an E5-style prefix.
        "query_prefix": query_prefix,
        "document_prefix": document_prefix,
        "input_format": input_format,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "memory-v3:" + digest


def memory_vector(value: object, dimensions: int | None = None) -> list[float] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MEMORY_DIMENSIONS:
        return None
    if dimensions is not None and len(value) != dimensions:
        return None
    if not set(map(type, value)) <= {int, float}:
        return None
    try:
        vector = list(map(float, value))
    except (ValueError, OverflowError):
        return None
    if not all(map(math.isfinite, vector)) or not any(vector):
        return None
    return vector


def memory_unit(values: list[float]) -> list[float]:
    """Normalize a validated vector without overflowing or losing subnormal norms."""
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm < sys.float_info.min:
        # A representable norm permits one normalization pass. Overflow and
        # subnormal rounding require the scaled path used by the original math.
        scale = max(map(abs, values))
        values = [value / scale for value in values]
        norm = math.hypot(*values)
    return [value / norm for value in values]


def memory_prepared_cosine(unit: list[float], vector: list[float]) -> float:
    """Score a validated vector against an already normalized query vector."""
    return max(-1.0, min(1.0, math.sumprod(unit, memory_unit(vector))))


def memory_cosine(left: list[float], right: list[float]) -> float:
    return memory_prepared_cosine(memory_unit(left), right)
