from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class GoalBudgetExceeded(RuntimeError):
    """A hard autonomy budget has been consumed."""


class GoalLoopDetected(RuntimeError):
    """The planner or evaluator repeated work without observable progress."""


@dataclass(frozen=True, slots=True)
class GoalUsage:
    steps: int
    replans: int
    model_calls: int
    started_at: datetime


@dataclass(frozen=True, slots=True)
class GoalBudget:
    max_steps: int
    max_replans: int
    max_runtime_seconds: int
    max_model_calls: int

    def __post_init__(self) -> None:
        if (
            min(
                self.max_steps,
                self.max_runtime_seconds,
                self.max_model_calls,
            )
            <= 0
            or self.max_replans < 0
        ):
            raise ValueError("goal budgets must be positive, except replans may be zero")

    def require_available(self, usage: GoalUsage, *, now: datetime) -> None:
        if usage.started_at.tzinfo is None or now.tzinfo is None:
            raise ValueError("goal budget timestamps must be timezone-aware")
        if min(usage.steps, usage.replans, usage.model_calls) < 0:
            raise ValueError("goal usage cannot be negative")
        if usage.steps >= self.max_steps:
            raise GoalBudgetExceeded("goal steps budget exhausted")
        if usage.replans >= self.max_replans:
            raise GoalBudgetExceeded("goal replans budget exhausted")
        if usage.model_calls >= self.max_model_calls:
            raise GoalBudgetExceeded("goal model_calls budget exhausted")
        elapsed = (now.astimezone(UTC) - usage.started_at.astimezone(UTC)).total_seconds()
        if elapsed < 0:
            raise ValueError("goal budget clock moved before goal start")
        if elapsed >= self.max_runtime_seconds:
            raise GoalBudgetExceeded("goal runtime budget exhausted")


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GoalLoopGuard:
    previous_plan_fingerprint: str | None = None
    previous_evaluation_fingerprint: str | None = None
    previous_state_fingerprint: str | None = None

    @staticmethod
    def plan_fingerprint(plan: dict[str, Any]) -> str:
        """Fingerprint executable structure, excluding explanatory prose."""

        normalized = dict(plan)
        normalized.pop("rationale_summary", None)
        normalized_nodes: list[dict[str, Any]] = []
        nodes = normalized.get("nodes", [])
        if isinstance(nodes, list):
            for raw in nodes:
                if not isinstance(raw, dict):
                    normalized_nodes.append({"invalid": type(raw).__name__})
                    continue
                node = dict(raw)
                node.pop("preferred_agent_constraints", None)
                normalized_nodes.append(node)
        normalized["nodes"] = normalized_nodes
        return _canonical_digest(normalized)

    @staticmethod
    def evaluation_fingerprint(decision: dict[str, Any]) -> str:
        normalized = dict(decision)
        normalized.pop("reason_summary", None)
        return _canonical_digest(normalized)

    @staticmethod
    def state_fingerprint(state: dict[str, Any]) -> str:
        return _canonical_digest(state)

    def require_new_plan(self, fingerprint: str) -> None:
        if self.previous_plan_fingerprint and fingerprint == self.previous_plan_fingerprint:
            raise GoalLoopDetected("planner proposed an equivalent plan")

    def require_progress(
        self,
        *,
        evaluation_fingerprint: str,
        state_fingerprint: str,
    ) -> None:
        if (
            self.previous_evaluation_fingerprint
            and self.previous_state_fingerprint
            and evaluation_fingerprint == self.previous_evaluation_fingerprint
            and state_fingerprint == self.previous_state_fingerprint
        ):
            raise GoalLoopDetected("evaluator repeated a decision without state change")


def active_runtime_seconds(goal: Mapping[str, Any], *, now: datetime | None = None) -> float:
    """Elapsed execution time, excluding durably recorded human waits."""
    record = dict(goal)
    if not record.get("started_at"):
        return 0.0
    started = datetime.fromisoformat(str(record["started_at"]))
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    end = now or datetime.now(UTC)
    if record.get("paused_at"):
        paused = datetime.fromisoformat(str(record["paused_at"]))
        if paused.tzinfo is None:
            paused = paused.replace(tzinfo=UTC)
        end = min(end, paused)
    return max(0.0, (end - started).total_seconds() - float(record.get("paused_seconds") or 0))


def runtime_remaining_seconds(goal: Mapping[str, Any], *, now: datetime | None = None) -> float:
    return max(0.0, float(goal["max_runtime_seconds"]) - active_runtime_seconds(goal, now=now))


def runtime_expired(goal: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    return runtime_remaining_seconds(goal, now=now) <= 0


# Used in the same writer transaction that makes a goal runnable again.
RESUME_RUNTIME_SQL = """paused_seconds=paused_seconds + CASE WHEN paused_at IS NULL THEN 0
    ELSE MAX(0,(julianday(?) - julianday(paused_at))*86400.0) END, paused_at=NULL"""
