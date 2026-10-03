from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services.memory_normalization import canonical_text_sha256


def bound_memory_presentation(item: dict[str, Any]) -> bool:
    """Validate a display view without giving its text authority over the memory.

    Original French is bound to the already qualified source journal metadata.
    Callers must still revalidate the canonical record/metadata against SQL after
    awaited retrieval work; this pure check cannot establish current provenance.
    """
    presentation = item.get("presentation")
    metadata = item.get("metadata")
    if not isinstance(presentation, dict) or not isinstance(metadata, dict):
        return False
    try:
        if (
            metadata.get("canonical_language") != "en"
            or presentation.get("language") != "fr"
            or presentation.get("temporary") is not True
            or presentation.get("grants_authority") is not False
            or presentation.get("source_revision") != item["updated_at"]
            or presentation.get("canonical_sha256") != canonical_text_sha256(item["content"])
            or presentation.get("summary_sha256")
            != (canonical_text_sha256(item["summary"]) if item["summary"] is not None else None)
        ):
            return False
        for field in ("content", "summary"):
            # Null and omitted are distinct contracts. A present summary must not
            # hide an empty content, or invent a summary absent from the source.
            if field not in presentation:
                return False
            value = presentation[field]
            if field == "summary" and item[field] is None:
                if value is not None:
                    return False
            elif not isinstance(value, str) or not value.strip():
                return False
        if "mode" not in presentation:
            return bool(presentation.get("validation_status") == "model_reviewed")
        if presentation["mode"] != "original":
            return False
        if presentation.get("validation_status") != "source_preserved":
            return False
        for field in ("source_id", "source_sha256", "canonical_receipt_id"):
            if (
                not isinstance(metadata.get(field), str)
                or not metadata[field]
                or presentation.get(field) != metadata[field]
            ):
                return False
        source_hash = hashlib.sha256(
            json.dumps(
                {"content": presentation["content"], "summary": presentation["summary"]},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if presentation["source_sha256"] != source_hash:
            return False
        for field in ("content", "summary"):
            unit = metadata.get(field)
            value = presentation[field]
            if value is None:
                if field not in metadata or unit is not None:
                    return False
                continue
            if (
                not isinstance(unit, dict)
                or unit.get("source_language") != "fr"
                or unit.get("source_id") != f"{metadata['source_id']}:{field}"
                or unit.get("source_sha256") != canonical_text_sha256(value)
                or unit.get("canonical_sha256") != canonical_text_sha256(item[field])
                or unit.get("source_revalidated") is not True
                or unit.get("grants_authority") is not False
            ):
                return False
        return True
    except (KeyError, TypeError, ValueError, UnicodeError):
        return False
