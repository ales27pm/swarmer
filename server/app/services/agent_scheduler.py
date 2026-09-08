from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite


class SchedulerService:
    """Deterministic worker eligibility and ranking; models never participate."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

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
                       COALESCE(s.score,0.0) AS historical_score
                FROM agents a LEFT JOIN agent_scores s ON s.agent_id=a.id
                WHERE a.id=?
                """,
                (agent_id,),
            )
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["skills"] = json.loads(str(value.pop("skills_json")))
        return value

    @staticmethod
    def eligible(agent: dict[str, Any], required_skill: str | None = None) -> bool:
        skills = agent.get("skills")
        return bool(
            agent.get("status") == "online"
            and isinstance(agent.get("active_jobs"), int)
            and isinstance(agent.get("max_concurrency"), int)
            and int(agent["active_jobs"]) < int(agent["max_concurrency"])
            and (required_skill is None or isinstance(skills, list) and required_skill in skills)
        )

    async def rank_eligible_agents(self, required_skill: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = list(
                await (
                    await db.execute(
                        """
                        SELECT a.*,
                               (SELECT COUNT(*) FROM agent_jobs j
                                WHERE j.claimed_by=a.id
                                  AND j.status IN ('claimed','running')) AS active_jobs,
                               COALESCE(s.score,0.0) AS historical_score
                        FROM agents a LEFT JOIN agent_scores s ON s.agent_id=a.id
                        """
                    )
                ).fetchall()
            )
        candidates: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["skills"] = json.loads(str(value.pop("skills_json")))
            if self.eligible(value, required_skill):
                candidates.append(value)
        candidates.sort(
            key=lambda agent: (
                float(agent["active_jobs"]) / float(agent["max_concurrency"]),
                -float(agent["historical_score"]),
                str(agent.get("last_seen_at") or agent.get("created_at") or ""),
                str(agent["id"]),
            )
        )
        return [self._public_candidate(agent) for agent in candidates]

    @staticmethod
    def _public_candidate(agent: dict[str, Any]) -> dict[str, Any]:
        return {
            key: agent.get(key)
            for key in (
                "id",
                "name",
                "model_id",
                "status",
                "skills",
                "max_concurrency",
                "active_jobs",
                "historical_score",
                "last_seen_at",
            )
        }
