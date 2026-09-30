from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.models import MemoryCreate, TaskCreate, TaskRecord
from app.services.context_builder import ContextBuilder
from app.services.episode_memory import EpisodeMemoryService, EpisodeSearchResult
from app.services.state_service import StateService
from app.services.strategy_retrieval import StrategyHint, StrategyRetrieval

NOW = "2026-09-29T01:00:00+00:00"


async def _goal(state: StateService, name: str, project: str | None) -> str:
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="SQLite storage"), source="test")
    )
    goal_id = f"goal_{name}"
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            """INSERT INTO goal_runs(id,root_task_id,objective,status,autonomy_profile,
            planner_source,max_steps,max_parallelism,max_replans,max_runtime_seconds,
            max_model_calls,completion_criteria_json,current_phase,created_at,updated_at)
            VALUES(?,?,'SQLite storage','running','assisted','ubuntu_local',8,2,1,600,5,
            '[]','executing',?,?)""",
            (goal_id, task.id, NOW, NOW),
        )
        if project:
            await db.execute(
                "INSERT OR IGNORE INTO coding_projects VALUES(?,?,?)", (project, NOW, NOW)
            )
            await db.execute("INSERT INTO goal_project_links VALUES(?,?)", (goal_id, project))
        await db.commit()
    return goal_id


async def _episode(state: StateService, name: str, project: str | None) -> str:
    goal_id = await _goal(state, name, project)
    async with aiosqlite.connect(state.db_path) as db:
        row = await (
            await db.execute("SELECT root_task_id FROM goal_runs WHERE id=?", (goal_id,))
        ).fetchone()
    assert row is not None
    episode = await EpisodeMemoryService(state.db_path).record_episode(
        goal_run_id=goal_id,
        root_task_id=str(row[0]),
        objective_summary=f"SQLite storage {name}",
        plan_summary="Compare storage and verify persistence",
        outcome="completed",
    )
    return episode.id


async def _fixture(tmp_path: Path) -> tuple[StateService, str, dict[str, str]]:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    goal = await _goal(state, "current", "project_a")
    records: dict[str, str] = {}
    for label, scope, sensitivity in (
        ("general", "general", "normal"),
        ("global", "global", "normal"),
        ("own", "project:project_a", "normal"),
        ("foreign", "project:project_b", "normal"),
        ("prefix", "project:project_a_extra", "normal"),
        ("legacy", "goal", "normal"),
        ("raw_id", "project_a", "normal"),
        ("private", "project:project_a", "secret"),
    ):
        item = await state.create_memory(
            MemoryCreate(content=f"SQLite storage {label}", scope=scope, sensitivity=sensitivity),
            actor_id="test",
        )
        records[label] = item["id"]
    records["own_episode"] = await _episode(state, "own_history", "project_a")
    records["foreign_episode"] = await _episode(state, "foreign_history", "project_b")
    records["unlinked_episode"] = await _episode(state, "legacy_history", None)
    return state, goal, records


@pytest.mark.asyncio
async def test_goal_context_only_contains_shared_and_exact_project_memories(tmp_path: Path) -> None:
    state, goal, ids = await _fixture(tmp_path)
    context = await ContextBuilder(state.db_path, max_tokens=8192, max_memory_items=20).build(
        goal_run_id=goal
    )
    relevant = set(context.provenance_ids).intersection(ids.values())
    assert relevant == {ids[key] for key in ("general", "global", "own", "own_episode")}
    persisted = await ContextBuilder(state.db_path).get_record(context.id)
    assert (
        persisted is not None
        and set(persisted.provenance_ids).intersection(ids.values()) == relevant
    )


@pytest.mark.asyncio
async def test_unscoped_strategy_only_returns_explicitly_shared_memories(tmp_path: Path) -> None:
    state, _, ids = await _fixture(tmp_path)
    hints = await StrategyRetrieval(
        state.db_path, EpisodeMemoryService(state.db_path), max_memory_hints=20
    ).retrieve("SQLite storage")
    assert set(hints.provenance_ids) == {ids["general"], ids["global"]}


class RecordingProvider:
    provider_name = "test-scope"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_unscoped_episode_search_returns_nothing_without_embedding(tmp_path: Path) -> None:
    state, _, _ = await _fixture(tmp_path)
    provider = RecordingProvider()
    result = await EpisodeMemoryService(state.db_path, provider).search("SQLite storage")
    assert result == []
    assert provider.calls == []


@pytest.mark.asyncio
async def test_strategy_scope_matches_exact_project_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    state, goal, ids = await _fixture(tmp_path)
    hints = await StrategyRetrieval(
        state.db_path, EpisodeMemoryService(state.db_path), max_memory_hints=20
    ).retrieve("SQLite storage", goal_run_id=goal)
    assert set(hints.provenance_ids) == {
        ids[key] for key in ("general", "global", "own", "own_episode")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["unlinked", "missing", "prefix"])
async def test_unresolved_goal_scope_never_searches_episodes_or_embeds(
    tmp_path: Path, scope: str
) -> None:
    state, goal, ids = await _fixture(tmp_path)
    target = (
        await _goal(state, "unlinked", None)
        if scope == "unlinked"
        else f"{goal}_extra"
        if scope == "prefix"
        else "goal_missing"
    )
    provider = RecordingProvider()
    episodes = EpisodeMemoryService(state.db_path, provider)
    assert await episodes.search("SQLite storage", goal_run_id=target) == []
    hints = await StrategyRetrieval(state.db_path, episodes, max_memory_hints=20).retrieve(
        "SQLite storage", goal_run_id=target
    )
    assert set(hints.provenance_ids) == {ids["general"], ids["global"]}
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "goal_id", ["", " ", "goal_current' OR 1=1 --", "goal_%", "goal_current\n"]
)
async def test_goal_scope_rejects_ambiguous_or_sql_identifiers(
    tmp_path: Path, goal_id: str
) -> None:
    state, _, _ = await _fixture(tmp_path)
    provider = RecordingProvider()
    episodes = EpisodeMemoryService(state.db_path, provider)
    with pytest.raises(ValueError):
        await episodes.search("SQLite storage", goal_run_id=goal_id)
    with pytest.raises(ValueError):
        await StrategyRetrieval(state.db_path, episodes).retrieve("SQLite", goal_run_id=goal_id)
    with pytest.raises(ValueError):
        await ContextBuilder(state.db_path).build(goal_run_id=goal_id)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_other_scope_memories_cannot_fill_context_or_strategy_candidate_windows(
    tmp_path: Path,
) -> None:
    state, goal, ids = await _fixture(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.executemany(
            """INSERT INTO memory_items(id,scope,kind,content,sensitivity,pinned,created_at,updated_at)
            VALUES(?,'project:project_b','fact','SQLite storage foreign','normal',1,?,?)""",
            [(f"mem_flood_{i}", NOW, NOW) for i in range(201)],
        )
        await db.commit()
    context = await ContextBuilder(state.db_path, max_tokens=8192, max_memory_items=3).build(
        goal_run_id=goal
    )
    hints = await StrategyRetrieval(
        state.db_path, EpisodeMemoryService(state.db_path), max_memory_hints=3
    ).retrieve("SQLite storage", goal_run_id=goal)
    wanted = {ids[key] for key in ("general", "global", "own")}
    assert {p for p in context.provenance_ids if p.startswith("mem_")} == wanted
    assert {hint.source_id for hint in hints.memory} == wanted


@pytest.mark.asyncio
@pytest.mark.parametrize("same_project", [False, True])
async def test_episode_project_and_outcome_filters_precede_candidate_limit(
    tmp_path: Path, same_project: bool
) -> None:
    state, goal, ids = await _fixture(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executemany(
            """INSERT INTO tasks(id,title,input,mode,source,status,created_at,updated_at)
            VALUES(?,'SQLite','SQLite','normal','test','completed',?,?)""",
            [(f"task_flood_{i}", NOW, NOW) for i in range(501)],
        )
        await db.executemany(
            """INSERT INTO goal_runs(id,root_task_id,objective,status,autonomy_profile,
            planner_source,max_steps,max_parallelism,max_replans,max_runtime_seconds,
            max_model_calls,completion_criteria_json,current_phase,created_at,updated_at)
            SELECT ?,?,objective,status,autonomy_profile,planner_source,
            max_steps,max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
            completion_criteria_json,current_phase,created_at,updated_at FROM goal_runs WHERE id=?""",
            [(f"goal_flood_{i}", f"task_flood_{i}", goal) for i in range(501)],
        )
        await db.executemany(
            "INSERT INTO goal_project_links VALUES(?,?)",
            [(f"goal_flood_{i}", "project_a" if same_project else "project_b") for i in range(501)],
        )
        await db.executemany(
            """INSERT INTO episodes(id,goal_run_id,root_task_id,objective_summary,plan_summary,
            outcome,duration_ms,worker_types_json,failure_tags_json,created_at,updated_at)
            SELECT ?,?,?,objective_summary,plan_summary,?,duration_ms,
            worker_types_json,failure_tags_json,'2099-01-01T00:00:00+00:00',updated_at
            FROM episodes WHERE id=?""",
            [
                (
                    f"ep_flood_{i}",
                    f"goal_flood_{i}",
                    f"task_flood_{i}",
                    "failed" if same_project else "completed",
                    ids["foreign_episode"],
                )
                for i in range(501)
            ],
        )
        await db.commit()
    results = await EpisodeMemoryService(state.db_path).search(
        "SQLite storage", goal_run_id=goal, outcomes=("completed",), limit=1
    )
    assert [item.episode.id for item in results] == [ids["own_episode"]]
    if not same_project:
        context = await ContextBuilder(state.db_path, max_tokens=8192, max_episode_items=1).build(
            goal_run_id=goal
        )
        assert {p for p in context.provenance_ids if p.startswith("ep_")} == {ids["own_episode"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
async def test_episode_scope_changed_during_embedding_never_returns_other_project(
    tmp_path: Path, replace: bool
) -> None:
    state, goal, _ = await _fixture(tmp_path)

    class RelinkingProvider(RecordingProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            async with aiosqlite.connect(state.db_path) as db:
                await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (goal,))
                if replace:
                    await db.execute(
                        "INSERT INTO goal_project_links VALUES(?,'project_b')", (goal,)
                    )
                await db.commit()
            return await super().embed(texts)

    results = await EpisodeMemoryService(state.db_path, RelinkingProvider()).search(
        "SQLite storage", goal_run_id=goal
    )
    assert results == []


@pytest.mark.asyncio
async def test_retrieval_does_not_delete_excluded_original_memories(tmp_path: Path) -> None:
    state, goal, ids = await _fixture(tmp_path)
    await ContextBuilder(state.db_path).build(goal_run_id=goal)
    await StrategyRetrieval(state.db_path, EpisodeMemoryService(state.db_path)).retrieve(
        "SQLite storage", goal_run_id=goal
    )
    async with aiosqlite.connect(state.db_path) as db:
        memories = await (await db.execute("SELECT id FROM memory_items")).fetchall()
        episodes = await (await db.execute("SELECT id FROM episodes")).fetchall()
    assert {row[0] for row in [*memories, *episodes]} == set(ids.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("after_search", [1, 2])
@pytest.mark.parametrize("change", ["replace", "unlink", "new_link"])
async def test_strategy_discards_context_when_project_changes_between_searches(
    tmp_path: Path, after_search: int, change: str
) -> None:
    state, goal, _ = await _fixture(tmp_path)
    if change == "new_link":
        async with aiosqlite.connect(state.db_path) as db:
            await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (goal,))
            await db.commit()

    class ChangingEpisodes(EpisodeMemoryService):
        calls = 0

        async def search(self, query: str, **kwargs: Any) -> list[EpisodeSearchResult]:
            result = await super().search(query, **kwargs)
            self.calls += 1
            if self.calls == after_search:
                async with aiosqlite.connect(state.db_path) as db:
                    await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (goal,))
                    if change != "unlink":
                        await db.execute(
                            "INSERT INTO goal_project_links VALUES(?,'project_b')", (goal,)
                        )
                    await db.commit()
            return result

    hints = await StrategyRetrieval(
        state.db_path, ChangingEpisodes(state.db_path), max_memory_hints=20
    ).retrieve("SQLite storage", goal_run_id=goal)
    assert hints.provenance_ids == ()
    assert hints.successful == hints.failures == hints.memory == ()


@pytest.mark.asyncio
async def test_strategy_rechecks_episode_source_scope_before_return(tmp_path: Path) -> None:
    state, goal, ids = await _fixture(tmp_path)

    class MovingSource(EpisodeMemoryService):
        async def search(self, query: str, **kwargs: Any) -> list[EpisodeSearchResult]:
            result = await super().search(query, **kwargs)
            async with aiosqlite.connect(state.db_path) as db:
                await db.execute(
                    """UPDATE goal_project_links SET project_id='project_b'
                    WHERE goal_run_id='goal_own_history'"""
                )
                await db.commit()
            return result

    hints = await StrategyRetrieval(
        state.db_path, MovingSource(state.db_path), max_memory_hints=20
    ).retrieve("SQLite storage", goal_run_id=goal)
    assert set(hints.provenance_ids) == {ids[key] for key in ("general", "global", "own")}
    assert hints.successful == hints.failures == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["scope", "sensitivity", "delete", "content"])
async def test_strategy_rechecks_memory_provenance_after_selection(
    tmp_path: Path, change: str
) -> None:
    state, goal, ids = await _fixture(tmp_path)

    class ChangingMemory(StrategyRetrieval):
        async def _memory_hints(
            self, query: str, *, goal_run_id: str | None
        ) -> tuple[StrategyHint, ...]:
            selected = await super()._memory_hints(query, goal_run_id=goal_run_id)
            statements = {
                "scope": "UPDATE memory_items SET scope='project:project_b' WHERE id=?",
                "sensitivity": "UPDATE memory_items SET sensitivity='secret' WHERE id=?",
                "delete": "DELETE FROM memory_items WHERE id=?",
                "content": "UPDATE memory_items SET content='New requirement' WHERE id=?",
            }
            async with aiosqlite.connect(state.db_path) as db:
                await db.execute(statements[change], (ids["own"],))
                await db.commit()
            return selected

    hints = await ChangingMemory(
        state.db_path, EpisodeMemoryService(state.db_path), max_memory_hints=20
    ).retrieve("SQLite storage", goal_run_id=goal)
    assert set(hints.provenance_ids) == {ids[key] for key in ("general", "global", "own_episode")}
