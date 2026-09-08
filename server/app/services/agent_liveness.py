from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

ACTIVE_AGENT_STATUSES = frozenset({"online", "busy", "draining"})
DEFAULT_AGENT_OFFLINE_TIMEOUT_SECONDS = 90


def agent_is_fresh(
    agent: Mapping[str, Any],
    *,
    now: datetime,
    timeout_seconds: int,
) -> bool:
    """Return effective liveness without mutating the agent's declared status."""

    if now.tzinfo is None:
        raise ValueError("agent liveness clock must be timezone-aware")
    if timeout_seconds <= 0:
        raise ValueError("agent offline timeout must be positive")
    raw_last_seen = agent.get("last_seen_at")
    if not isinstance(raw_last_seen, str):
        return False
    try:
        last_seen = datetime.fromisoformat(raw_last_seen)
    except ValueError:
        return False
    if last_seen.tzinfo is None:
        return False
    age_seconds = (now.astimezone(UTC) - last_seen.astimezone(UTC)).total_seconds()
    return 0 <= age_seconds < timeout_seconds


def agent_counts_as_active(
    agent: Mapping[str, Any],
    *,
    now: datetime,
    timeout_seconds: int,
) -> bool:
    return str(agent.get("status")) in ACTIVE_AGENT_STATUSES and agent_is_fresh(
        agent,
        now=now,
        timeout_seconds=timeout_seconds,
    )
