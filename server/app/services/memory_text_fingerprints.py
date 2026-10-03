"""Stable JSON and text fingerprints shared by memory projections and retrieval."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def text_view_sha256(content: str, summary: str | None) -> str:
    return _hash({"content": content, "summary": summary})
