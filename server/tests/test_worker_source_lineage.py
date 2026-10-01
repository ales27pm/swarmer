"""Replans retain only named ancestry or still-current user page requests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.worker_context import read_worker_context
from tests.test_research_collect_handoff import receipt, started, submit
from tests.test_worker_context import database_rows
from tests.test_writing_failure_diagnostics import diagnostics
from tests.test_writing_retry_feedback import insert_repair

URLS = ["https://example.org/a", "https://example.org/b"]
OBJECTIVE = f"Lis ces deux pages : {URLS[0]} et {URLS[1]} . Rédige une note de 150 à 200 mots. Cite les deux URL."


async def collected(tmp_path: Path, *, explicit: bool = True) -> tuple[Any, ...]:
    manager, goal, agents, research = await started(tmp_path)
    if explicit:
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute("UPDATE goal_runs SET objective=? WHERE id=?", (OBJECTIVE, goal["id"]))
            await db.execute(
                "UPDATE goal_messages SET content=? WHERE id=?",
                (OBJECTIVE, "gmsg_initial_" + goal["id"]),
            )
            await db.commit()
    await submit(manager, agents[0], research, receipt())
    writer = await manager.agent_dispatcher.claim(agents[1])
    assert writer
    node = next(
        n for n in await manager.graph.list_nodes(goal["id"]) if n["worker_job_id"] == writer["id"]
    )
    return manager, goal, agents, research, writer, node


async def consumer(
    manager: Any, goal: Any, parent: Any, revision: int, skill: str = "writing.draft"
) -> Any:
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = dict(
            await (
                await db.execute("SELECT * FROM plan_nodes WHERE id=?", (parent["id"],))
            ).fetchone()
        )
        row.update(
            id="node_new_consumer",
            status="planned",
            task_id=None,
            worker_job_id=None,
            depends_on_json="[]",
            conversation_revision=revision,
            required_skill=skill,
            planner_metadata_json=json.dumps({"source": "replan" if revision else "evaluator"}),
        )
        await db.execute(
            "UPDATE goal_runs SET conversation_revision=? WHERE id=?", (revision, goal["id"])
        )
        await db.execute(
            "INSERT INTO plan_nodes ("
            + ",".join(row)
            + ") VALUES ("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
        await db.commit()
    return next(n for n in await manager.graph.list_nodes(goal["id"]) if n["id"] == row["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [0, 1])
@pytest.mark.parametrize("skill", ["writing.draft", "code.build_project"])
async def test_requested_pages_survive_omitted_edges_and_new_revision(
    tmp_path: Path, revision: int, skill: str
) -> None:
    manager, goal, _agents, research, writer, previous = await collected(tmp_path)
    node = await consumer(manager, goal, previous, revision, skill)
    before = await database_rows(manager.db_path)
    context, sources = await read_worker_context(manager.db_path, goal["id"], node["id"])
    assert context == []  # No unrelated summaries are inferred as dependencies.
    assert sources == writer["payload"]["research_sources"]
    assert [s["evidence"]["requested_url"] for s in sources] == URLS
    assert all(
        s["worker_job_id"] == research["id"] and s["content_trust"] == "untrusted" for s in sources
    )
    if skill == "writing.draft":
        payload = await manager._worker_payload(await manager.graph.get_goal(goal["id"]), node)
        assert payload["research_sources"] == sources
        assert payload["requirements"]["min_citations"] == 2
        assert "previous_attempt_feedback" not in payload
    assert await database_rows(manager.db_path) == before


@pytest.mark.asyncio
async def test_synthesis_dependency_follows_only_its_named_research_ancestors(
    tmp_path: Path,
) -> None:
    manager, goal, _agents, _research, writer, original = await collected(tmp_path, explicit=False)
    bridge = await consumer(manager, goal, original, 0)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET node_type='synthesis',required_skill=NULL,status='completed',depends_on_json=? WHERE id=?",
            (json.dumps(original["depends_on"]), bridge["id"]),
        )
        await db.execute(
            "UPDATE plan_nodes SET depends_on_json=? WHERE id=?",
            (json.dumps([bridge["id"]]), original["id"]),
        )
        await db.commit()
    context, sources = await read_worker_context(manager.db_path, goal["id"], original["id"])
    assert context == []
    assert sources == writer["payload"]["research_sources"]
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET depends_on_json=? WHERE id=?",
            (json.dumps([original["id"]]), bridge["id"]),
        )
        await db.commit()
    with pytest.raises(ValueError, match="cycle"):
        await read_worker_context(manager.db_path, goal["id"], original["id"])


@pytest.mark.asyncio
async def test_explicit_measured_retry_inherits_parent_sources_without_failed_text(
    tmp_path: Path,
) -> None:
    manager, goal, agents, _research, writer, previous = await collected(tmp_path)
    measurement = diagnostics(
        word_count=225,
        citation_count=2,
        min_citations=2,
        required_source_domains=[],
        cited_source_domains=["example.org"],
        failures=["max_words"],
    )
    result, _ = await manager.agent_dispatcher.submit_result(
        agents[1],
        writer["id"],
        writer["claim_token"],
        status="failed",
        result=measurement,
        error="writing_requirements_unmet",
        lease_id=writer["lease_id"],
        lease_generation=writer["lease_generation"],
    )
    await manager.on_job_result(result)
    repair = await insert_repair(manager, goal, previous["id"])
    payload = await manager._worker_payload(await manager.graph.get_goal(goal["id"]), repair)
    assert payload["research_sources"] == writer["payload"]["research_sources"]
    assert payload["previous_attempt_feedback"]["diagnostics"]["word_count"] == 225
    assert not payload.get("dependency_context")  # Failed output never becomes evidence.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["no_request", "wrong_goal", "wrong_task", "failed_job", "forged_receipt", "new_read_request"],
)
async def test_unrelated_or_stale_page_receipts_do_not_enter_replan(
    tmp_path: Path, change: str
) -> None:
    manager, goal, _agents, research, _writer, previous = await collected(
        tmp_path, explicit=change != "no_request"
    )
    node = await consumer(manager, goal, previous, 1)
    async with aiosqlite.connect(manager.db_path) as db:
        if change == "wrong_goal":
            await db.execute(
                "UPDATE plan_nodes SET goal_run_id='goal_other' WHERE worker_job_id=?",
                (research["id"],),
            )
        elif change == "wrong_task":
            await db.execute(
                "UPDATE tasks SET source='goal:other' WHERE id=?", (research["task_id"],)
            )
        elif change == "failed_job":
            await db.execute("UPDATE agent_jobs SET status='failed' WHERE id=?", (research["id"],))
        elif change == "forged_receipt":
            invalid = receipt()
            invalid["pages"][0]["content_sha256"] = "0" * 64
            await db.execute(
                "UPDATE agent_jobs SET result_json=? WHERE id=?",
                (json.dumps(invalid), research["id"]),
            )
        elif change == "new_read_request":
            await db.execute(
                "UPDATE goal_messages SET created_at='2099-01-01T00:00:00+00:00' WHERE goal_run_id=?",
                (goal["id"],),
            )
        await db.commit()
    assert await read_worker_context(manager.db_path, goal["id"], node["id"]) == ([], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("role,count", [("user", 1), ("assistant", 2)])
async def test_current_user_exclusions_apply_without_promoting_assistant_text(
    tmp_path: Path, role: str, count: int
) -> None:
    manager, goal, _agents, _research, _writer, previous = await collected(tmp_path)
    node = await consumer(manager, goal, previous, 1)
    async with aiosqlite.connect(manager.db_path) as db:
        link = await (
            await db.execute(
                "SELECT conversation_id FROM goal_conversation_links WHERE goal_run_id=?",
                (goal["id"],),
            )
        ).fetchone()
        await db.execute(
            "INSERT INTO goal_messages(id,conversation_id,goal_run_id,role,content,created_at) VALUES ('gmsg_exclude',?,?,?,?,?)",
            (link[0], goal["id"], role, "Ne lis plus " + URLS[0], "2099-01-01T00:00:00+00:00"),
        )
        await db.commit()
    _, sources = await read_worker_context(manager.db_path, goal["id"], node["id"])
    assert len(sources) == count
    if role == "user":
        assert sources[0]["url"] == URLS[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["mismatched_message_goal", "other_project_shared_conversation"])
async def test_foreign_conversation_message_cannot_change_page_requirements(
    tmp_path: Path, scope: str
) -> None:
    from app.services.swarm_contracts import GoalCreateRequest

    manager, goal, _agents, _research, _writer, previous = await collected(tmp_path)
    node = await consumer(manager, goal, previous, 1)
    other = await manager.create_goal(
        GoalCreateRequest(objective="Unrelated request"), actor_id="phone"
    )
    async with aiosqlite.connect(manager.db_path) as db:
        conversation = await (
            await db.execute(
                "SELECT conversation_id FROM goal_conversation_links WHERE goal_run_id=?",
                (goal["id"],),
            )
        ).fetchone()
        await db.execute(
            "UPDATE goal_messages SET conversation_id=?,content=?,created_at='2099-01-01T00:00:00+00:00' WHERE goal_run_id=?",
            (conversation[0], "Ne lis plus " + URLS[0], other["id"]),
        )
        if scope == "other_project_shared_conversation":
            await db.execute(
                "UPDATE goal_conversation_links SET conversation_id=? WHERE goal_run_id=?",
                (conversation[0], other["id"]),
            )
        await db.commit()
    _, sources = await read_worker_context(manager.db_path, goal["id"], node["id"])
    assert [s["url"] for s in sources] == URLS


@pytest.mark.asyncio
async def test_explicit_request_without_authoritative_message_blocks_instead_of_dropping_sources(
    tmp_path: Path,
) -> None:
    manager, goal, _agents, _research, _writer, previous = await collected(tmp_path)
    node = await consumer(manager, goal, previous, 1)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("DELETE FROM goal_messages WHERE goal_run_id=?", (goal["id"],))
        await db.commit()
    with pytest.raises(ValueError, match="authoritative dated source"):
        await read_worker_context(manager.db_path, goal["id"], node["id"])


@pytest.mark.asyncio
async def test_shared_ancestor_is_also_preserved_as_direct_context(tmp_path: Path) -> None:
    manager, goal, _agents, research, _writer, original = await collected(tmp_path, explicit=False)
    bridge = await consumer(manager, goal, original, 0)
    source_id = original["depends_on"][0]
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET node_type='synthesis',required_skill=NULL,status='completed',depends_on_json=? WHERE id=?",
            (json.dumps([source_id]), bridge["id"]),
        )
        await db.execute(
            "UPDATE plan_nodes SET depends_on_json=? WHERE id=?",
            (json.dumps([bridge["id"], source_id]), original["id"]),
        )
        await db.commit()
    context, _ = await read_worker_context(manager.db_path, goal["id"], original["id"])
    assert len(context) == 1 and context[0]["worker_job_id"] == research["id"]


@pytest.mark.asyncio
async def test_required_pages_precede_unrelated_search_hits_at_source_limit(tmp_path: Path) -> None:
    manager, goal, _agents, research, _writer, original = await collected(tmp_path)
    node = await consumer(manager, goal, original, 1)
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        job = dict(
            await (
                await db.execute("SELECT * FROM agent_jobs WHERE id=?", (research["id"],))
            ).fetchone()
        )
        task = dict(
            await (await db.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],))).fetchone()
        )
        ancestor = dict(
            await (
                await db.execute(
                    "SELECT * FROM plan_nodes WHERE worker_job_id=?", (research["id"],)
                )
            ).fetchone()
        )
        result = {
            "content_trust": "untrusted",
            "results": [
                {
                    "title": "Search hit",
                    "url": f"https://example.net/{i}",
                    "snippet": "No page read.",
                }
                for i in range(6)
            ],
        }
        for table, row in (
            ("tasks", {**task, "id": "task_search"}),
            (
                "agent_jobs",
                {
                    **job,
                    "id": "job_search",
                    "task_id": "task_search",
                    "required_skill": "research.query",
                    "result_json": json.dumps(result),
                },
            ),
            (
                "plan_nodes",
                {
                    **ancestor,
                    "id": "node_search",
                    "task_id": "task_search",
                    "worker_job_id": "job_search",
                    "required_skill": "research.query",
                    "conversation_revision": 1,
                },
            ),
        ):
            await db.execute(
                "INSERT INTO "
                + table
                + " ("
                + ",".join(row)
                + ") VALUES ("
                + ",".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
        await db.execute(
            "UPDATE plan_nodes SET depends_on_json='[\"node_search\"]' WHERE id=?", (node["id"],)
        )
        await db.commit()
    _, sources = await read_worker_context(manager.db_path, goal["id"], node["id"])
    assert len(sources) == 6
    assert [s["url"] for s in sources[:2]] == URLS
    assert all(s["worker_job_id"] == research["id"] and s.get("evidence") for s in sources[:2])
