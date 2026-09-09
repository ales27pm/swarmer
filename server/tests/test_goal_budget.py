from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.goal_limits import GoalBudget, GoalBudgetExceeded, GoalUsage


def test_goal_budget_accepts_usage_strictly_below_every_limit() -> None:
    started_at = datetime(2026, 9, 8, tzinfo=UTC)
    budget = GoalBudget(
        max_steps=20,
        max_replans=3,
        max_runtime_seconds=1_800,
        max_model_calls=30,
    )
    usage = GoalUsage(steps=19, replans=2, model_calls=29, started_at=started_at)

    budget.require_available(usage, now=started_at + timedelta(seconds=1_799))


@pytest.mark.parametrize(
    ("usage", "elapsed_seconds", "reason"),
    [
        (GoalUsage(20, 0, 0, datetime(2026, 9, 8, tzinfo=UTC)), 1, "steps"),
        (GoalUsage(0, 3, 0, datetime(2026, 9, 8, tzinfo=UTC)), 1, "replans"),
        (GoalUsage(0, 0, 30, datetime(2026, 9, 8, tzinfo=UTC)), 1, "model_calls"),
        (GoalUsage(0, 0, 0, datetime(2026, 9, 8, tzinfo=UTC)), 1_800, "runtime"),
    ],
)
def test_goal_budget_stops_deterministically_at_each_hard_limit(
    usage: GoalUsage,
    elapsed_seconds: int,
    reason: str,
) -> None:
    budget = GoalBudget(20, 3, 1_800, 30)

    with pytest.raises(GoalBudgetExceeded, match=reason):
        budget.require_available(
            usage,
            now=usage.started_at + timedelta(seconds=elapsed_seconds),
        )


def test_goal_budget_requires_timezone_aware_time() -> None:
    budget = GoalBudget(20, 3, 1_800, 30)
    usage = GoalUsage(0, 0, 0, datetime(2026, 9, 8))  # noqa: DTZ001 - invalid input fixture

    with pytest.raises(ValueError, match="timezone-aware"):
        budget.require_available(
            usage,
            now=datetime(2026, 9, 8),  # noqa: DTZ001 - invalid input fixture
        )
