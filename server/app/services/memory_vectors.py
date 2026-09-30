"""Validation and space identity for rebuildable memory embeddings."""

from __future__ import annotations

import hashlib
import json
import math

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
    if any(type(item) not in {int, float} for item in value):
        return None
    try:
        vector = [float(item) for item in value]
    except (ValueError, OverflowError):
        return None
    if not all(math.isfinite(item) for item in vector) or not any(vector):
        return None
    return vector


def memory_cosine(left: list[float], right: list[float]) -> float:
    def unit(values: list[float]) -> list[float]:
        scale = max(abs(value) for value in values)
        scaled = [value / scale for value in values]
        norm = math.hypot(*scaled)
        return [value / norm for value in scaled]

    return max(
        -1.0, min(1.0, math.fsum(a * b for a, b in zip(unit(left), unit(right), strict=True)))
    )
