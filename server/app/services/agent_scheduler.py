from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.services.agent_card import SUPPORTED_AGENT_PROTOCOL
from app.services.agent_liveness import (
    DEFAULT_AGENT_OFFLINE_TIMEOUT_SECONDS,
    agent_is_fresh,
)
from app.services.permission_policy import PermissionPolicy, PermissionPolicyError


@dataclass(frozen=True, slots=True)
class SchedulerSelection:
    selected_agent_id: str
    candidates: tuple[dict[str, Any], ...]
    scoring: dict[str, Any]


class SchedulerService:
    """Deterministic policy scheduling; models never choose worker eligibility."""

    def __init__(
        self,
        db_path: Path,
        *,
        offline_timeout_seconds: int = DEFAULT_AGENT_OFFLINE_TIMEOUT_SECONDS,
        permission_policy: PermissionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if offline_timeout_seconds <= 0:
            raise ValueError("agent offline timeout must be positive")
        self.db_path = db_path
        self.offline_timeout_seconds = offline_timeout_seconds
        self.permission_policy = permission_policy
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise RuntimeError("agent scheduler clock must be timezone-aware")
        return value.astimezone(UTC)

    def is_fresh(self, agent: dict[str, Any], *, now: datetime | None = None) -> bool:
        return agent_is_fresh(
            agent,
            now=now or self._now(),
            timeout_seconds=self.offline_timeout_seconds,
        )

    def skill_is_allowed(self, skill: str) -> bool:
        if self.permission_policy is None:
            return True
        try:
            return self.permission_policy.evaluate_worker_skill(skill).decision == "allow"
        except PermissionPolicyError:
            return False

    @staticmethod
    async def agent_state_locked(db: aiosqlite.Connection, agent_id: str) -> dict[str, Any] | None:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM agent_jobs j
                        WHERE j.claimed_by=a.id AND j.status IN ('claimed','running'))
                           AS active_jobs,
                       COALESCE(o.composite_score,s.score,0.0) AS historical_score,
                       o.average_latency_seconds,
                       COALESCE(o.terminal_jobs,0) AS observed_terminal_jobs
                FROM agents a
                LEFT JOIN agent_scores s ON s.agent_id=a.id
                LEFT JOIN agent_score_snapshots o ON o.agent_id=a.id
                WHERE a.id=?
                """,
                (agent_id,),
            )
        ).fetchone()
        return SchedulerService._agent_from_row(row) if row is not None else None

    @staticmethod
    def _agent_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        value = dict(row)
        value["skills"] = json.loads(str(value.pop("skills_json")))
        return value

    def eligible(
        self,
        agent: dict[str, Any],
        required_skill: str | None = None,
        *,
        now: datetime | None = None,
    ) -> bool:
        skills = agent.get("skills")
        return bool(
            agent.get("status") == "online"
            and self.is_fresh(agent, now=now)
            and (required_skill is None or self.skill_is_allowed(required_skill))
            and agent.get("supported_protocol_version") == SUPPORTED_AGENT_PROTOCOL
            and isinstance(agent.get("active_jobs"), int)
            and isinstance(agent.get("max_concurrency"), int)
            and int(agent["active_jobs"]) < int(agent["max_concurrency"])
            and (required_skill is None or isinstance(skills, list) and required_skill in skills)
        )

    @staticmethod
    def _ranking_key(agent: dict[str, Any]) -> tuple[float, float, float, str, str]:
        terminal_jobs = int(agent.get("observed_terminal_jobs") or 0)
        latency = agent.get("average_latency_seconds")
        latency_rank = (
            float(latency) if terminal_jobs >= 3 and latency is not None else float("inf")
        )
        return (
            float(agent["active_jobs"]) / float(agent["max_concurrency"]),
            -float(agent.get("historical_score") or 0.0),
            latency_rank,
            str(agent.get("last_seen_at") or agent.get("created_at") or ""),
            str(agent["id"]),
        )

    @staticmethod
    def _evidence_candidate(agent: dict[str, Any], *, rank: int | None = None) -> dict[str, Any]:
        candidate: dict[str, Any] = {
            "agent_id": agent.get("id"),
            "skill_match": True,
            "load_ratio": round(float(agent["active_jobs"]) / float(agent["max_concurrency"]), 8),
            "observed_score": float(agent.get("historical_score") or 0.0),
            "latency_estimate_seconds": (
                float(agent["average_latency_seconds"])
                if int(agent.get("observed_terminal_jobs") or 0) >= 3
                and agent.get("average_latency_seconds") is not None
                else None
            ),
            "last_seen_at": agent.get("last_seen_at"),
            "protocol": agent.get("supported_protocol_version"),
        }
        if rank is not None:
            candidate["rank"] = rank
        return candidate

    async def _eligible_locked(
        self,
        db: aiosqlite.Connection,
        required_skill: str,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM agent_jobs j
                        WHERE j.claimed_by=a.id
                          AND j.status IN ('claimed','running')) AS active_jobs,
                       COALESCE(o.composite_score,s.score,0.0) AS historical_score,
                       o.average_latency_seconds,
                       COALESCE(o.terminal_jobs,0) AS observed_terminal_jobs
                FROM agents a
                LEFT JOIN agent_scores s ON s.agent_id=a.id
                LEFT JOIN agent_score_snapshots o ON o.agent_id=a.id
                """
            )
        ).fetchall()
        candidates = [self._agent_from_row(row) for row in rows]
        selected_now = now or self._now()
        candidates = [
            candidate
            for candidate in candidates
            if self.eligible(candidate, required_skill, now=selected_now)
        ]
        candidates.sort(key=self._ranking_key)
        return candidates

    async def rank_eligible_agents(self, required_skill: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            candidates = await self._eligible_locked(db, required_skill)
        return [
            {
                "id": agent.get("id"),
                "name": agent.get("name"),
                "model_id": agent.get("model_id"),
                "status": agent.get("status"),
                "skills": agent.get("skills"),
                "max_concurrency": agent.get("max_concurrency"),
                "active_jobs": agent.get("active_jobs"),
                "historical_score": agent.get("historical_score"),
                "last_seen_at": agent.get("last_seen_at"),
                "supported_protocol_version": agent.get("supported_protocol_version"),
                "latency_estimate_seconds": self._evidence_candidate(agent)[
                    "latency_estimate_seconds"
                ],
            }
            for agent in candidates
        ]

    async def select_for_job_locked(
        self,
        db: aiosqlite.Connection,
        job_id: str,
        *,
        now: datetime | None = None,
    ) -> SchedulerSelection | None:
        row = await (
            await db.execute(
                """SELECT required_skill,last_agent_id
                FROM agent_jobs WHERE id=? AND status='queued'""",
                (job_id,),
            )
        ).fetchone()
        if row is None:
            return None
        candidates = await self._eligible_locked(db, str(row[0]), now=now)
        if not candidates:
            return None
        previous_agent_id = str(row[1]) if row[1] is not None else None
        if previous_agent_id and len(candidates) > 1:
            alternatives = [
                candidate for candidate in candidates if str(candidate["id"]) != previous_agent_id
            ]
            if alternatives:
                candidates = alternatives
        public = tuple(
            self._evidence_candidate(candidate, rank=index)
            for index, candidate in enumerate(candidates, start=1)
        )
        return SchedulerSelection(
            selected_agent_id=str(candidates[0]["id"]),
            candidates=public,
            scoring={
                "algorithm": "deterministic-v2",
                "order": [
                    "skill_and_protocol_eligibility",
                    "load_ratio_ascending",
                    "observed_score_descending",
                    "latency_after_three_completions_ascending",
                    "oldest_last_seen",
                    "agent_id",
                ],
            },
        )

    @staticmethod
    async def record_decision_locked(
        db: aiosqlite.Connection,
        *,
        job_id: str,
        selection: SchedulerSelection,
        created_at: str,
    ) -> str:
        decision_id = f"sch_{uuid4().hex}"
        await db.execute(
            """
            INSERT INTO scheduler_decisions(
                id,job_id,candidates_json,selected_agent_id,scoring_json,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                decision_id,
                job_id,
                json.dumps(selection.candidates, separators=(",", ":"), sort_keys=True),
                selection.selected_agent_id,
                json.dumps(selection.scoring, separators=(",", ":"), sort_keys=True),
                created_at,
            ),
        )
        return decision_id
