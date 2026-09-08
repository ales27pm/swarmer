from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any


def lease_token_hash(token: str) -> str:
    return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


def lease_matches(
    row: Mapping[str, Any],
    *,
    agent_id: str,
    token: str,
    lease_id: str | None,
    lease_generation: int | None,
    now: str,
    require_unexpired: bool,
) -> bool:
    stored_hash = row.get("lease_token_hash")
    token_matches = isinstance(stored_hash, str) and hmac.compare_digest(
        stored_hash, lease_token_hash(token)
    )
    return bool(
        row.get("claimed_by") == agent_id
        and token_matches
        and (lease_id is None or row.get("lease_id") == lease_id)
        and (
            lease_generation is None
            or isinstance(row.get("lease_generation"), int)
            and row.get("lease_generation") == lease_generation
        )
        and (
            not require_unexpired
            or isinstance(row.get("lease_expires_at"), str)
            and str(row["lease_expires_at"]) > now
        )
    )
