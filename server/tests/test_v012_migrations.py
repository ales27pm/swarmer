from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.services.state_service import SCHEMA_VERSION, StateService

V011_SCHEMA_VERSION = 19
V012_TABLES = frozenset(
    {
        "goal_runs",
        "plan_nodes",
        "plan_edges",
        "goal_evaluations",
        "goal_results",
        "goal_feedback",
        "goal_model_calls",
        "goal_contexts",
        "episodes",
        "episode_steps",
        "episode_embeddings",
    }
)


async def _table_names(db: aiosqlite.Connection) -> set[str]:
    rows = await (await db.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    return {str(row[0]) for row in rows}


async def _make_v011_fixture(path: Path) -> None:
    """Create the exact v0.11 shape by removing v0.12's additive tables."""

    state = StateService(path)
    await state.initialize()
    created_at = "2026-09-08T00:00:00+00:00"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """INSERT INTO tasks(
                id,title,input,mode,source,conversation_id,status,priority,
                created_at,updated_at,completed_at,error_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tsk_v011_preserved",
                "Preserve the v0.11 task",
                "Preserve the v0.11 task",
                "normal",
                "migration-test",
                None,
                "completed",
                2,
                created_at,
                created_at,
                created_at,
                None,
            ),
        )
        await db.execute(
            """INSERT INTO memory_items(
                id,scope,kind,content,summary,sensitivity,confidence,pinned,
                metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "mem_v011_preserved",
                "project",
                "fact",
                "Ubuntu remains authoritative.",
                "Authority invariant",
                "normal",
                1.0,
                1,
                '{"release":"0.11"}',
                created_at,
                created_at,
            ),
        )
        await db.execute("PRAGMA foreign_keys=OFF")
        for table in (
            "episode_embeddings",
            "episode_steps",
            "episodes",
            "goal_model_calls",
            "goal_contexts",
            "goal_feedback",
            "goal_results",
            "goal_evaluations",
            "plan_edges",
            "plan_nodes",
            "goal_runs",
        ):
            await db.execute(f"DROP TABLE {table}")
        await db.execute(f"PRAGMA user_version={V011_SCHEMA_VERSION}")
        await db.commit()


@pytest.mark.asyncio
async def test_v011_upgrade_to_v012_is_additive_restart_safe_and_preserves_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v011-state.db"
    await _make_v011_fixture(path)

    state = StateService(path)
    await state.initialize()
    await state.initialize()

    async with aiosqlite.connect(path) as db:
        version = await (await db.execute("PRAGMA user_version")).fetchone()
        task = await (
            await db.execute(
                """SELECT title,status,priority,completed_at
                FROM tasks WHERE id='tsk_v011_preserved'"""
            )
        ).fetchone()
        memory = await (
            await db.execute(
                """SELECT content,pinned,metadata_json
                FROM memory_items WHERE id='mem_v011_preserved'"""
            )
        ).fetchone()
        foreign_key_errors = await (await db.execute("PRAGMA foreign_key_check")).fetchall()
        tables = await _table_names(db)
        new_table_counts = {
            table: int((await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0])
            for table in V012_TABLES
        }
        indexes = {
            str(row[0])
            for row in await (
                await db.execute(
                    """SELECT name FROM sqlite_master
                    WHERE type='index' AND name IN (
                        'idx_goal_runs_status','idx_plan_nodes_goal_status',
                        'idx_plan_edges_goal_to','idx_goal_evaluations_goal',
                        'idx_goal_feedback_goal','idx_goal_model_calls_goal',
                        'idx_goal_contexts_goal','idx_episodes_outcome_created'
                    )"""
                )
            ).fetchall()
        }

    assert SCHEMA_VERSION >= V011_SCHEMA_VERSION + 1
    assert version == (SCHEMA_VERSION,)
    assert task == (
        "Preserve the v0.11 task",
        "completed",
        2,
        "2026-09-08T00:00:00+00:00",
    )
    assert memory == (
        "Ubuntu remains authoritative.",
        1,
        '{"release":"0.11"}',
    )
    assert V012_TABLES.issubset(tables)
    assert set(indexes) == {
        "idx_goal_runs_status",
        "idx_plan_nodes_goal_status",
        "idx_plan_edges_goal_to",
        "idx_goal_evaluations_goal",
        "idx_goal_feedback_goal",
        "idx_goal_model_calls_goal",
        "idx_goal_contexts_goal",
        "idx_episodes_outcome_created",
    }
    assert set(new_table_counts.values()) == {0}
    assert foreign_key_errors == []
