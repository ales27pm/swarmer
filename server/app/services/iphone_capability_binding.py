from __future__ import annotations

import hashlib
import json
from typing import Any


class CapabilityRequestBindingError(ValueError):
    pass


def canonical_capability_request_fingerprint(
    *,
    job_id: str,
    lease_generation: int,
    capability_name: str,
    arguments: dict[str, Any],
) -> str:
    """Identify one logical capability request without weakening its one-use action digest."""

    if not job_id or lease_generation < 0 or not capability_name or not isinstance(arguments, dict):
        raise CapabilityRequestBindingError("capability request binding is invalid")
    try:
        canonical = json.dumps(
            {
                "arguments": arguments,
                "capability_name": capability_name,
                "job_id": job_id,
                "lease_generation": lease_generation,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapabilityRequestBindingError(
            "capability request arguments are not canonical JSON"
        ) from exc
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
