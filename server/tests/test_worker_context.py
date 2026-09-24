"""Only authoritative dependency receipts cross the bounded worker handoff."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import aiosqlite
import pytest

from app.services.project_contracts import ProjectPayload
from app.services.swarm_contracts import GoalCreateRequest
from app.services.worker_context import read_worker_context
from app.services.writing_contracts import WritingPayload
from tests.test_goal_research_sources import OBJECTIVE, researched


def encoded_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


async def database_rows(path: Path) -> list[str]:
    async with aiosqlite.connect(path) as db:
        return [line async for line in db.iterdump()]


async def replace_dependency_result(
    path: Path, job_id: str, result: dict[str, Any], *, skill: str = "research.query"
) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE agent_jobs SET required_skill=?,result_json=? WHERE id=?",
            (skill, json.dumps(result, ensure_ascii=False), job_id),
        )
        await db.execute(
            "UPDATE plan_nodes SET required_skill=? WHERE worker_job_id=?", (skill, job_id)
        )
        await db.commit()


async def duplicate_dependencies(path: Path, job_id: str, consumer_id: str, count: int) -> None:
    """Clone a real completed benign fixture, keeping every task/job/node link valid."""
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        job = dict(
            await (await db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,))).fetchone()
        )
        task = dict(
            await (await db.execute("SELECT * FROM tasks WHERE id=?", (job["task_id"],))).fetchone()
        )
        node = dict(
            await (
                await db.execute("SELECT * FROM plan_nodes WHERE worker_job_id=?", (job_id,))
            ).fetchone()
        )
        dependencies = []
        for index in range(count):
            task_id, new_job_id, node_id = (
                f"tsk_context_{index:02}",
                f"job_context_{index:02}",
                f"node_context_{index:02}",
            )
            for table, row in (
                ("tasks", {**task, "id": task_id}),
                ("agent_jobs", {**job, "id": new_job_id, "task_id": task_id}),
                (
                    "plan_nodes",
                    {**node, "id": node_id, "task_id": task_id, "worker_job_id": new_job_id},
                ),
            ):
                columns = ",".join(f'"{name}"' for name in row)
                placeholders = ",".join("?" for _ in row)
                await db.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values())
                )
            await db.execute(
                "INSERT INTO plan_edges VALUES (?,?,?,'hard')",
                (node["goal_run_id"], node_id, consumer_id),
            )
            dependencies.append(node_id)
        await db.execute(
            "UPDATE plan_nodes SET depends_on_json=? WHERE id=?",
            (json.dumps(dependencies), consumer_id),
        )
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer_skill", ["writing.draft", "code.build_project"])
async def test_same_job_supplies_untrusted_context_and_exact_sources_without_side_effects(
    tmp_path: Path, consumer_skill: str
) -> None:
    manager, goal, job, node, _ = await researched(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET required_skill=? WHERE id=?", (consumer_skill, node["id"])
        )
        await db.commit()
    before = await database_rows(manager.db_path)
    context, sources = await read_worker_context(manager.db_path, goal["id"], node["id"])
    assert len(context) == len(sources) == 1
    assert context[0]["worker_job_id"] == sources[0]["worker_job_id"] == job["id"]
    assert context[0]["required_skill"] == "research.query"
    assert context[0]["content_trust"] == sources[0]["content_trust"] == "untrusted"
    assert sources[0]["url"] == "https://example.org/activites"
    assert "Ignore all instructions" in sources[0]["snippet"]
    assert "private-test-value" not in json.dumps([context, sources])
    assert await database_rows(manager.db_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "job_task",
        "job_skill",
        "job_status",
        "node_status",
        "task_source",
        "invalid_result",
        "wrong_job",
        "other_goal",
        "instruction_revision",
        "consumer_goal",
        "consumer_skill",
    ],
)
async def test_mismatched_or_unfinished_evidence_is_rejected(tmp_path: Path, change: str) -> None:
    manager, goal, job, node, _ = await researched(tmp_path)
    other = await manager.create_goal(
        GoalCreateRequest(objective="Autre demande"), actor_id="phone"
    )
    async with aiosqlite.connect(manager.db_path) as db:
        statements = {
            "job_task": (
                "UPDATE agent_jobs SET task_id=? WHERE id=?",
                (goal["root_task_id"], job["id"]),
            ),
            "job_skill": (
                "UPDATE agent_jobs SET required_skill='workspace.list_dir' WHERE id=?",
                (job["id"],),
            ),
            "job_status": ("UPDATE agent_jobs SET status='running' WHERE id=?", (job["id"],)),
            "node_status": (
                "UPDATE plan_nodes SET status='failed' WHERE worker_job_id=?",
                (job["id"],),
            ),
            "task_source": (
                "UPDATE tasks SET source='goal:unrelated' WHERE id=?",
                (job["task_id"],),
            ),
            "invalid_result": (
                "UPDATE agent_jobs SET result_json=? WHERE id=?",
                ('{"content_trust":"trusted","results":[]}', job["id"]),
            ),
            "wrong_job": (
                "UPDATE plan_nodes SET worker_job_id='job_missing' WHERE worker_job_id=?",
                (job["id"],),
            ),
            "other_goal": (
                "UPDATE plan_nodes SET goal_run_id=? WHERE worker_job_id=?",
                (other["id"], job["id"]),
            ),
            "instruction_revision": (
                "UPDATE plan_nodes SET conversation_revision=conversation_revision+1 WHERE id=?",
                (node["id"],),
            ),
            "consumer_skill": (
                "UPDATE plan_nodes SET required_skill='workspace.list_dir' WHERE id=?",
                (node["id"],),
            ),
        }
        if change in statements:
            sql, parameters = statements[change]
            await db.execute(sql, parameters)
        await db.commit()
    requested_goal = other["id"] if change == "consumer_goal" else goal["id"]
    before = await database_rows(manager.db_path)
    with pytest.raises(ValueError):
        await read_worker_context(manager.db_path, requested_goal, node["id"])
    assert await database_rows(manager.db_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "skipped", "cancelled"])
async def test_unsuccessful_optional_dependencies_are_skipped(tmp_path: Path, status: str) -> None:
    manager, goal, job, node, _ = await researched(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET status=? WHERE worker_job_id=?", (status, job["id"])
        )
        await db.execute(
            "UPDATE plan_edges SET dependency_type='optional' WHERE to_node_id=?", (node["id"],)
        )
        await db.commit()
    assert await read_worker_context(manager.db_path, goal["id"], node["id"]) == ([], [])


@pytest.mark.asyncio
async def test_completed_optional_dependency_still_needs_authoritative_evidence(
    tmp_path: Path,
) -> None:
    manager, goal, job, node, _ = await researched(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_edges SET dependency_type='optional' WHERE to_node_id=?", (node["id"],)
        )
        await db.execute("UPDATE agent_jobs SET status='failed' WHERE id=?", (job["id"],))
        await db.commit()
    with pytest.raises(ValueError, match="evidence is unavailable"):
        await read_worker_context(manager.db_path, goal["id"], node["id"])


@pytest.mark.asyncio
async def test_source_count_and_deduplication_keep_whole_urls(tmp_path: Path) -> None:
    result = {
        "content_trust": "untrusted",
        "results": [
            {"title": "Page", "url": f"https://example.org/page/{index}", "snippet": "Notes"}
            for index in range(10)
        ],
    }
    manager, goal, job, node, _ = await researched(tmp_path, result)
    await duplicate_dependencies(manager.db_path, job["id"], node["id"], 2)
    context, sources = await read_worker_context(manager.db_path, goal["id"], node["id"])
    assert len(context) == 2
    assert len(sources) == 5
    assert [source["url"] for source in sources] == [item["url"] for item in result["results"][:5]]
    assert {source["worker_job_id"] for source in sources} == {"job_context_00"}
    assert encoded_bytes(sources) <= 8000


@pytest.mark.asyncio
async def test_source_utf8_budget_omits_whole_sources_instead_of_corrupting_provenance(
    tmp_path: Path,
) -> None:
    result = {
        "content_trust": "untrusted",
        "results": [
            {
                "title": "😀" * 240,
                "url": f"https://example.org/{index}/" + "x" * 850,
                "snippet": "😀" * 700,
            }
            for index in range(5)
        ],
    }
    manager, goal, job, node, _ = await researched(tmp_path, result)
    _, sources = await read_worker_context(manager.db_path, goal["id"], node["id"])
    assert len(sources) == 1
    assert sources[0] == {
        "content_trust": "untrusted",
        "worker_job_id": job["id"],
        **result["results"][0],
    }
    assert encoded_bytes(sources) <= 8000
    next_source = {**sources[0], "url": result["results"][1]["url"]}
    assert encoded_bytes([*sources, next_source]) > 8000


@pytest.mark.asyncio
@pytest.mark.parametrize("large", [False, True])
async def test_dependency_count_and_utf8_aggregate_are_bounded(tmp_path: Path, large: bool) -> None:
    manager, goal, job, node, _ = await researched(tmp_path)
    await replace_dependency_result(
        manager.db_path,
        job["id"],
        {"content": "😀" * 2000 if large else "A benign notes file."},
        skill="workspace.read_text",
    )
    await duplicate_dependencies(manager.db_path, job["id"], node["id"], 10)
    context, sources = await read_worker_context(manager.db_path, goal["id"], node["id"])
    assert sources == []
    assert encoded_bytes(context) <= 12000
    if large:
        assert 0 < len(context) < 8
        next_item = {**context[0], "node_id": "node_context_09", "worker_job_id": "job_context_09"}
        assert encoded_bytes([*context, next_item]) > 12000
    else:
        assert len(context) == 8
    assert [item["node_id"] for item in context] == [
        f"node_context_{index:02}" for index in range(len(context))
    ]
    assert all(item["content_trust"] == "untrusted" for item in context)


@pytest.fixture(scope="module")
def worker_parsers() -> tuple[ModuleType, ModuleType]:
    root = Path(__file__).resolve().parents[2] / "workers"
    modules = []
    for name, path in (
        ("context_project_contract", root / "project-worker/project_contract.py"),
        ("context_text_worker", root / "text-worker/text_worker.py"),
    ):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules[0], modules[1]


@pytest.mark.asyncio
async def test_server_handoff_parses_in_both_workers_without_promoting_source_instructions(
    tmp_path: Path, worker_parsers: tuple[ModuleType, ModuleType]
) -> None:
    manager, goal, _, node, _ = await researched(tmp_path)
    context, sources = await read_worker_context(manager.db_path, goal["id"], node["id"])
    conversation = [{"role": "user", "content": "Comparer seulement les activités gratuites."}]
    handoff = {"dependency_context": context, "research_sources": sources}
    project = {
        "objective": OBJECTIVE,
        "conversation": conversation,
        "files": [],
        "plan": [],
        "checks": [],
        "iteration": 1,
        "base_revision_id": None,
        "base_sha256": None,
    }
    writing = {"schema_version": "1.0", "objective": OBJECTIVE, "conversation": conversation}
    project_parser, text_parser = worker_parsers
    for base, model, parse in (
        (
            project,
            ProjectPayload,
            lambda value: project_parser.parse_payload(
                {"required_skill": "code.build_project", "payload": value}
            ),
        ),
        (writing, WritingPayload, text_parser.validate_payload),
    ):
        legacy = model.model_validate(base).model_dump(exclude_unset=True)
        assert "dependency_context" not in legacy and "research_sources" not in legacy
        assert parse(legacy)["conversation"] == conversation
        wire = model.model_validate({**base, **handoff}).model_dump(exclude_unset=True)
        parsed = parse(wire)
        assert parsed["conversation"] == conversation
        assert parsed["objective"] == OBJECTIVE
        assert {key: parsed[key] for key in handoff} == handoff
        assert "Ignore all instructions" not in json.dumps(parsed["conversation"])
        assert "Ignore all instructions" in parsed["research_sources"][0]["snippet"]
        assert not {"tools", "permissions", "approved", "tool_calls"} & parsed.keys()
    async with aiosqlite.connect(manager.db_path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM tool_calls")).fetchone())[0] == 0
