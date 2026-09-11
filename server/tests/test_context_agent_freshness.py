from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_context_builder import _seed_goal
from test_goal_worker_preflight import (
    OBJECTIVE,
    _assert_waiting,
    _create,
    _FinitePlanner,
    _plan,
    _start,
)

from app.main import create_app
from app.services.context_builder import ContextBuilder
from app.services.swarm_contracts import GoalStartRequest, PlannerSource
from app.settings import Settings

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_stale_agent_does_not_consume_card_limit_or_enter_persisted_context(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, _ = await _seed_goal(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE agents SET last_seen_at=?,skills_json=? WHERE id='agent_context'",
            ((NOW - timedelta(seconds=90)).isoformat(), json.dumps(["code.generate_python"])),
        )
        await db.execute(
            """INSERT INTO agents(
                id,name,version,endpoint,model_id,status,skills_json,last_seen_at,
                max_concurrency,runtime,supported_protocol_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "agent_zz_fresh",
                "Project worker",
                "1.2.3",
                "https://worker.invalid",
                "project-model",
                "online",
                json.dumps(["code.build_project"]),
                (NOW - timedelta(seconds=1)).isoformat(),
                1,
                "python",
                "mongars-worker-v0.9",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        await db.commit()
    builder = ContextBuilder(db_path, max_agent_cards=1, clock=lambda: NOW)
    await builder.initialize()

    context = await builder.build(goal_run_id=goal_id)

    agent_cards = [card for card in context.cards if card.kind == "agent_card"]
    assert [card.card_id for card in agent_cards] == ["agent:agent_zz_fresh:1.2.3"]
    assert "code.build_project" in agent_cards[0].summary
    assert "agent_context" not in context.provenance_ids
    async with aiosqlite.connect(db_path) as db:
        stored = await (
            await db.execute(
                "SELECT context_json,provenance_json FROM goal_contexts WHERE id=?",
                (context.id,),
            )
        ).fetchone()
        statuses = await (await db.execute("SELECT id,status FROM agents ORDER BY id")).fetchall()
    assert stored is not None
    assert "code.generate_python" not in stored[0]
    assert "agent_context" not in stored[1]
    assert dict(statuses) == {"agent_context": "online", "agent_zz_fresh": "online"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "last_seen_at", "included"),
    [
        ("online", (NOW - timedelta(seconds=89)).isoformat(), True),
        ("online", (NOW - timedelta(seconds=90)).isoformat(), False),
        ("online", (NOW - timedelta(seconds=91)).isoformat(), False),
        ("online", None, False),
        ("online", "not-a-timestamp", False),
        ("online", "2030-01-01T11:59:59", False),
        ("online", (NOW + timedelta(seconds=1)).isoformat(), False),
        (
            "online",
            (NOW - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=-4))).isoformat(),
            True,
        ),
        ("draining", (NOW - timedelta(seconds=1)).isoformat(), True),
        ("offline", (NOW - timedelta(seconds=1)).isoformat(), False),
    ],
)
async def test_context_agent_uses_canonical_freshness_without_status_mutation(
    tmp_path: Path,
    status: str,
    last_seen_at: str | None,
    included: bool,
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, _ = await _seed_goal(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE agents SET status=?,last_seen_at=?", (status, last_seen_at))
        await db.commit()
    builder = ContextBuilder(db_path, clock=lambda: NOW)
    await builder.initialize()

    context = await builder.build(goal_run_id=goal_id)

    assert any(card.kind == "agent_card" for card in context.cards) is included
    async with aiosqlite.connect(db_path) as db:
        assert await (await db.execute("SELECT status,last_seen_at FROM agents")).fetchone() == (
            status,
            last_seen_at,
        )


@pytest.mark.asyncio
async def test_context_respects_custom_offline_timeout(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, _ = await _seed_goal(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE agents SET last_seen_at=?", ((NOW - timedelta(seconds=15)).isoformat(),)
        )
        await db.commit()
    builder = ContextBuilder(db_path, offline_timeout_seconds=15, clock=lambda: NOW)
    await builder.initialize()

    assert not any(
        card.kind == "agent_card" for card in (await builder.build(goal_run_id=goal_id)).cards
    )


@pytest.mark.parametrize("timeout", [0, -1])
def test_context_rejects_nonpositive_offline_timeout(tmp_path: Path, timeout: int) -> None:
    with pytest.raises(ValueError, match="offline timeout must be positive"):
        ContextBuilder(tmp_path / "state.db", offline_timeout_seconds=timeout)


def test_factory_wires_configured_offline_timeout_into_context(tmp_path: Path) -> None:
    app = create_app(Settings(db_path=tmp_path / "state.db", agent_offline_timeout_seconds=37))
    assert app.state.context_builder.offline_timeout_seconds == 37
    assert app.state.agent_dispatcher.scheduler.offline_timeout_seconds == 37


def _stale_agent(test_app: FastAPI) -> None:
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            """INSERT INTO agents(id,name,version,endpoint,status,skills_json,last_seen_at,
                created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "agent_stale",
                "Stopped worker",
                "1.0.0",
                "https://worker.invalid",
                "online",
                '["workspace.list_dir"]',
                "2000-01-01T00:00:00+00:00",
                "2000-01-01T00:00:00+00:00",
                "2000-01-01T00:00:00+00:00",
            ),
        )


def test_stale_worker_start_and_recovery_preserve_planner_budget(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
) -> None:
    planner = _FinitePlanner()
    test_app.state.goal_manager.planner = planner
    _stale_agent(test_app)
    goal_id = _create(client, paired_headers)

    _assert_waiting(_start(client, paired_headers, goal_id))
    assert client.portal is not None
    for _ in range(2):
        assert client.portal.call(test_app.state.goal_manager.reconcile) == 0
        _assert_waiting(_start(client, paired_headers, goal_id))

    assert planner.calls == 0
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM goal_model_calls").fetchone() == (0,)
        assert db.execute("SELECT status FROM agents WHERE id='agent_stale'").fetchone() == (
            "online",
        )


def test_stale_worker_waiting_goal_does_not_hide_ready_goal_at_recovery_limit(
    client: TestClient,
    paired_headers: dict[str, str],
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = _FinitePlanner()
    manager = test_app.state.goal_manager
    manager.planner = planner
    waiting_id = _create(client, paired_headers)
    _assert_waiting(_start(client, paired_headers, waiting_id))
    ready_id = _create(client, paired_headers)
    assert client.portal is not None

    async def crash_before_dispatch(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated exit after plan commit")

    with monkeypatch.context() as crash:
        crash.setattr(manager, "_advance_ready", crash_before_dispatch)
        with pytest.raises(RuntimeError, match="simulated exit after plan commit"):
            client.portal.call(
                manager.start_goal,
                ready_id,
                GoalStartRequest(
                    plan_proposal=_plan(OBJECTIVE), planner_source=PlannerSource.MANUAL
                ),
            )
    _stale_agent(test_app)
    with sqlite3.connect(test_app.state.settings.db_path) as db:
        db.execute(
            "UPDATE goal_runs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (waiting_id,)
        )

    assert client.portal.call(partial(manager.reconcile, limit=1)) == 1

    _assert_waiting(client.get(f"/goals/{waiting_id}", headers=paired_headers).json())
    recovered = client.get(f"/goals/{ready_id}", headers=paired_headers).json()
    assert recovered["nodes"][0]["status"] == "dispatched"
    assert recovered["goal"]["model_call_count"] == 0
    assert planner.calls == 0
