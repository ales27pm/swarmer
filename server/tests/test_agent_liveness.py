from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.agent_liveness import agent_counts_as_active, agent_is_fresh


def test_agent_freshness_fails_closed_at_and_after_timeout() -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    assert agent_is_fresh(
        {"last_seen_at": (now - timedelta(seconds=89)).isoformat()},
        now=now,
        timeout_seconds=90,
    )
    assert not agent_is_fresh(
        {"last_seen_at": (now - timedelta(seconds=90)).isoformat()},
        now=now,
        timeout_seconds=90,
    )
    assert not agent_is_fresh(
        {"last_seen_at": (now - timedelta(seconds=91)).isoformat()},
        now=now,
        timeout_seconds=90,
    )


@pytest.mark.parametrize(
    "last_seen_at",
    [None, "not-a-timestamp", "2026-09-08T12:00:00", "2026-09-08T12:00:01+00:00"],
)
def test_agent_freshness_rejects_missing_invalid_naive_and_future_values(
    last_seen_at: str | None,
) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    assert not agent_is_fresh(
        {"last_seen_at": last_seen_at},
        now=now,
        timeout_seconds=90,
    )


def test_agent_freshness_accepts_equivalent_offset_aware_timestamp() -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    eastern = timezone(timedelta(hours=-4))

    assert agent_is_fresh(
        {"last_seen_at": datetime(2026, 9, 8, 7, 59, 30, tzinfo=eastern).isoformat()},
        now=now,
        timeout_seconds=90,
    )


def test_only_fresh_active_status_counts_as_active() -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    fresh = (now - timedelta(seconds=1)).isoformat()

    assert agent_counts_as_active(
        {"status": "draining", "last_seen_at": fresh}, now=now, timeout_seconds=90
    )
    assert not agent_counts_as_active(
        {"status": "offline", "last_seen_at": fresh}, now=now, timeout_seconds=90
    )
