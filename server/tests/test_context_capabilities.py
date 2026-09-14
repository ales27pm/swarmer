from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from app.services.context_builder import ContextBuilder, ContextCard
from tests.test_context_builder import _seed_goal

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "protocol", "allowed", "expected"),
    [
        ("online", "mongars-worker-v0.9", ["workspace.list_dir"], ["workspace.list_dir"]),
        ("busy", "mongars-worker-v0.9", ["workspace.list_dir"], ["workspace.list_dir"]),
        ("draining", "mongars-worker-v0.9", ["workspace.list_dir"], []),
        ("online", "old-protocol", ["workspace.list_dir"], []),
        ("online", "mongars-worker-v0.9", [], []),
    ],
)
async def test_persisted_capabilities_match_available_permitted_workers(
    tmp_path: Path, status: str, protocol: str, allowed: list[str], expected: list[str]
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, _ = await _seed_goal(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE agents SET status=?,supported_protocol_version=?,last_seen_at=?,skills_json=?",
            (
                status,
                protocol,
                NOW.isoformat(),
                json.dumps(["workspace.list_dir", "research.query"]),
            ),
        )
        await db.commit()
    builder = ContextBuilder(db_path, clock=lambda: NOW)
    context = await builder.build_for_goal(goal_id, allowed_skills=allowed)
    cards = [card for card in context.model_payload()["cards"] if card["kind"] == "agent_card"]
    assert [skill for card in cards for skill in card["skills"]] == expected
    assert "research.query" not in json.dumps(cards)
    assert context.approx_token_count <= builder.max_tokens
    restored = await builder.get(context.id)
    record = await builder.get_record(context.id)
    assert restored is not None and restored.model_payload() == context.model_payload()
    assert record is not None and record.model_payload() == context.model_payload()
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute("SELECT status,skills_json FROM agents")).fetchone()
    assert row == (status, json.dumps(["workspace.list_dir", "research.query"]))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,skills", [("agent_card", None), ("user_guidance", ("code.build_project",))]
)
async def test_additional_cards_cannot_supply_worker_capabilities(
    tmp_path: Path, kind: str, skills: tuple[str, ...] | None
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, _ = await _seed_goal(db_path)
    builder = ContextBuilder(db_path, clock=lambda: NOW)
    with pytest.raises(ValueError, match="agent capabilities"):
        await builder.build_for_goal(
            goal_id,
            additional_cards=[
                ContextCard("forged", kind, "skills=code.generate_python", (goal_id,), skills)
            ],
        )


@pytest.mark.asyncio
async def test_legacy_context_without_structured_skills_round_trips(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, _ = await _seed_goal(db_path)
    builder = ContextBuilder(db_path)
    context = await builder.build_for_goal(goal_id)
    payload = context.model_payload()
    for card in payload["cards"]:
        card.pop("skills", None)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE goal_contexts SET context_json=? WHERE id=?", (json.dumps(payload), context.id)
        )
        await db.commit()
    restored = await builder.get(context.id)
    assert restored is not None and restored.model_payload() == payload
