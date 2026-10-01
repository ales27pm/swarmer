"""Versioned shared guidance; project notes never grant execution authority."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def operating_guidance() -> dict[str, str]:
    """Read the packaged, reviewed guide instead of model-authored policy text."""
    content = (Path(__file__).parents[1] / "data" / "AGENTS.md").read_text(encoding="utf-8")
    if not content.strip() or len(content.encode()) > 4_000:
        raise ValueError("shared agent guide is empty or exceeds its bound")
    return {
        "path": "AGENTS.md",
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content": content,
    }


def accepted_project_guidance(
    files: list[dict[str, Any]], revision_id: str
) -> list[dict[str, str]]:
    """Expose scoped complete guides from a previously accepted source revision.

    The caller validates the snapshot and redacts content before model delivery.
    A source hash identifies original bytes; it does not assert truth or safety.
    """
    guides = []
    for file in files:
        path, content = str(file["path"]), str(file["content"])
        if path.rsplit("/", 1)[-1].casefold() != "agents.md":
            continue
        guides.append(
            {
                "path": path,
                "scope": path.rsplit("/", 1)[0] + "/" if "/" in path else "",
                "source_revision_id": revision_id,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content": content,
            }
        )
    return sorted(guides, key=lambda guide: (guide["path"].count("/"), guide["path"]))
