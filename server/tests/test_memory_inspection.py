from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.memory_inspection import (
    MAX_PAGE_BYTES,
    MemoryInspectionCursorError,
    MemoryInspectionEvidenceError,
    read_memory_usage,
)
from app.services.state_service import StateService

STAMP = "2026-09-28T22:00:00+00:00"


async def seed(path: Path) -> None:
    await StateService(path).initialize()
    async with aiosqlite.connect(path) as db:
        for name in ("a", "b", "legacy"):
            await db.execute(
                """INSERT INTO tasks(id,title,input,mode,source,status,priority,created_at,updated_at)
                VALUES(?,?,?,'chat',?,'completed',0,?,?)""",
                (f"tsk_{name}", name, name, f"goal:goal_{name}", STAMP, STAMP),
            )
            await db.execute(
                """INSERT INTO goal_runs(id,root_task_id,objective,status,autonomy_profile,
                planner_source,max_steps,max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
                conversation_revision,created_at,updated_at) VALUES(?,?,?,'completed','manual','test',5,1,0,60,5,2,?,?)""",
                (f"goal_{name}", f"tsk_{name}", name, STAMP, STAMP),
            )
            if name != "legacy":
                await db.execute(
                    "INSERT INTO coding_projects VALUES(?,?,?)", (f"project_{name}", STAMP, STAMP)
                )
                await db.execute(
                    "INSERT INTO goal_project_links VALUES(?,?)",
                    (f"goal_{name}", f"project_{name}"),
                )
        await db.execute("INSERT INTO goal_conversations VALUES('conv_a','goal_a')")
        await db.execute("INSERT INTO goal_conversation_links VALUES('goal_a','conv_a',NULL)")
        await db.execute(
            """INSERT INTO goal_messages(id,conversation_id,goal_run_id,role,content,created_at)
            VALUES('gmsg_a','conv_a','goal_a','user','Conserver les rendez-vous validés.',?)""",
            (STAMP,),
        )
        await db.execute(
            """INSERT INTO project_memory_items(id,project_id,source_kind,source_id,source_goal_id,
            source_order,summary,content_sha256,updated_at) VALUES('pmem_a','project_a','message','gmsg_a','goal_a',1,'Rendez-vous',?,?)""",
            ("a" * 64, STAMP),
        )
        for identity, scope, sensitivity in [
            ("mem_general", "general", "normal"),
            ("mem_global", "global", "normal"),
            ("mem_a", "project:project_a", "normal"),
            ("mem_b", "project:project_b", "normal"),
            ("mem_private", "general", "sensitive"),
        ]:
            await db.execute(
                """INSERT INTO memory_items(id,scope,kind,content,sensitivity,created_at,updated_at)
                VALUES(?,?,'fact','current text must not replace recorded quote',?,?,?)""",
                (identity, scope, sensitivity, STAMP, STAMP),
            )
        await db.execute(
            """INSERT INTO audit_events(event_type,actor_type,actor_id,payload_json,created_at)
            VALUES('memory.remembered','device','phone','{"memory_id":"mem_general"}',?)""",
            (STAMP,),
        )
        await db.execute(
            """INSERT INTO episodes(id,goal_run_id,root_task_id,objective_summary,plan_summary,outcome,
            duration_ms,worker_types_json,failure_tags_json,created_at,updated_at)
            VALUES('ep_b','goal_b','tsk_b','other objective','other plan','completed',1,'[]','[]',?,?)""",
            (STAMP, STAMP),
        )
        await db.commit()


async def call(
    path: Path,
    name: str = "one",
    *,
    items: list[dict] | None = None,
    date: str = STAMP,
    goal: str = "a",
) -> None:
    items = (
        items
        if items is not None
        else [
            {"id": "mem_general", "summary": "Recorded original preference."},
            {"id": "pmem_a", "source_id": "gmsg_a", "summary": "Recorded rendez-vous requirement."},
        ]
    )
    cards, refs = [], {}
    for item in items:
        prefix = (
            "project-memory"
            if item["id"].startswith("pmem_")
            else "episode"
            if item["id"].startswith("ep_")
            else "memory"
        )
        kind = "project_memory_hint" if prefix == "project-memory" else prefix
        card_id = prefix + ":" + item["id"]
        cards.append({"card_id": card_id, "kind": kind, "summary": item["summary"]})
        refs[card_id] = [item["id"]] + ([item["source_id"]] if "source_id" in item else [])
    payload = {
        "purpose": "planner",
        "cards": cards,
        "reasoning": "NEVER EXPOSE RAW THINKING",
        "query": "NEVER EXPOSE QUERY",
    }
    provenance = {
        "source_ids": [identity for group in refs.values() for identity in group],
        "card_provenance": refs,
    }
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """INSERT INTO goal_contexts(id,goal_run_id,root_task_id,purpose,context_json,provenance_json,approx_token_count,created_at)
            VALUES(?,?,?,'planner',?,?,10,?)""",
            (
                "ctx_" + name,
                "goal_" + goal,
                "tsk_" + goal,
                json.dumps(payload),
                json.dumps(provenance),
                date,
            ),
        )
        await db.execute(
            """INSERT INTO goal_model_calls(id,goal_run_id,role,provider_source,model_id,context_id,input_digest,status,conversation_revision,created_at)
            VALUES(?,?,'planner','test','safe:7b',?,'digest','failed',1,?)""",
            ("gmc_" + name, "goal_" + goal, "ctx_" + name, date),
        )
        await db.commit()


async def retrieval(
    path: Path, name: str = "one", *, mode: str = "lexical", items: list[dict] | None = None
) -> None:
    payload = {
        "goal_id": "goal_a",
        "project_id": "project_a",
        "conversation_revision": 1,
        "mode": mode,
        "reason": "embedding_unavailable",
        "provider_fingerprint": "a" * 64,
        "items": items
        if items is not None
        else [{"id": "pmem_a", "source_id": "gmsg_a", "summary": "Recorded retrieval quotation."}],
    }
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """INSERT INTO goal_memory_queries(id,project_id,goal_run_id,purpose,conversation_revision,provider_identity,
            logical_fingerprint,query_sha256,status,context_json,created_at,expires_at)
            VALUES(?,'project_a','goal_a','planner',1,?,'logical','query','completed',?,?,?)""",
            ("gmq_" + name, "a" * 64, json.dumps(payload), STAMP, STAMP),
        )
        await db.commit()


async def worker(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """INSERT INTO tasks(id,title,input,mode,source,status,priority,created_at,updated_at)
            VALUES('tsk_child','child','benign','chat','goal:goal_a','created',0,?,?)""",
            (STAMP, STAMP),
        )
        payload = {
            "memory": {
                "mode": "hybrid",
                "reason": "semantic_and_lexical",
                "items": [
                    {"id": "pmem_a", "source_id": "gmsg_a", "summary": "Worker payload quotation."}
                ],
            },
            "files": [{"content": "NEVER FILE CONTENTS"}],
        }
        await db.execute(
            """INSERT INTO agent_jobs(id,task_id,required_skill,payload_json,status,created_at,updated_at)
            VALUES('job_a','tsk_child','code.build_project',?,'queued',?,?)""",
            (json.dumps(payload), STAMP, STAMP),
        )
        await db.execute(
            """INSERT INTO plan_nodes(id,goal_run_id,task_id,node_type,title,objective,required_skill,status,worker_job_id,expected_output,created_at,updated_at,conversation_revision)
            VALUES('node_a','goal_a','tsk_child','worker','child','benign','code.build_project','dispatched','job_a','file',?,?,1)""",
            (STAMP, STAMP),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_distinct_stages_terminal_goal_exact_quotes_and_no_writes(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    await call(path)
    await retrieval(path)
    await worker(path)
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    page = await read_memory_usage(path, "goal_a")
    assert page is not None
    assert page.current_conversation_revision == 2
    assert {e.evidence_stage for e in page.entries} == {
        "retrieved",
        "attached_to_model_call",
        "included_in_worker_job",
    }
    by = {e.evidence_stage: e for e in page.entries}
    assert by["attached_to_model_call"].status == "failed"
    assert by["included_in_worker_job"].status == "queued"
    assert by["retrieved"].retrieval.mode == "lexical"
    assert by["included_in_worker_job"].retrieval.mode == "hybrid"
    assert by["attached_to_model_call"].retrieval.mode == "unknown"
    assert all(e.conversation_revision == 1 for e in page.entries)
    assert all(i.source_state == "unknown" for e in page.entries for i in e.items)
    assert by["attached_to_model_call"].items[0].verification == "user_asserted"
    assert by["attached_to_model_call"].items[0].summary == "Recorded original preference."
    assert by["retrieved"].model_call_id is None
    assert by["included_in_worker_job"].model_id is None
    assert "NEVER" not in page.model_dump_json() and "current text" not in page.model_dump_json()
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before
    legacy = await read_memory_usage(path, "goal_legacy")
    assert legacy and legacy.project_id is None and legacy.availability == "no_records"
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM goal_project_links WHERE goal_run_id='goal_legacy'"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_scope_sensitive_and_deleted_sources_rechecked(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    await call(
        path,
        items=[
            {"id": "mem_b", "summary": "CROSS_PROJECT_SECRET"},
            {"id": "ep_b", "summary": "FOREIGN EPISODE"},
            {"id": "mem_private", "summary": "PRIVATE"},
            {"id": "mem_general", "summary": "saved"},
            {"id": "pmem_a", "source_id": "gmsg_a", "summary": "historical"},
        ],
    )
    page = await read_memory_usage(path, "goal_a")
    assert page
    entry = page.entries[0]
    assert entry.omitted_item_count == 2
    assert all(i.summary is None for i in entry.items if i.id == "mem_private")
    assert "mem_b" not in page.model_dump_json() and "ep_b" not in page.model_dump_json()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE memory_items SET scope='project:project_b' WHERE id='mem_general'")
        db.execute("DELETE FROM goal_messages WHERE id='gmsg_a'")
    changed = await read_memory_usage(path, "goal_a")
    assert changed
    assert changed.entries[0].omitted_item_count == 3
    item = next(i for i in changed.entries[0].items if i.id == "pmem_a")
    assert item.source_state == "missing" and item.summary is None
    assert (
        "saved" not in changed.model_dump_json() and "historical" not in changed.model_dump_json()
    )


@pytest.mark.asyncio
async def test_scope_and_sensitivity_admitted_before_item_limit(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    items = []
    with sqlite3.connect(path) as db:
        for number in range(130):
            identity = f"mem_hidden{number}"
            db.execute(
                "INSERT INTO memory_items(id,scope,kind,content,sensitivity,created_at,updated_at) VALUES(?,'general','fact','hidden','sensitive',?,?)",
                (identity, STAMP, STAMP),
            )
            items.append({"id": identity, "summary": "PRIVATE"})
    items.append({"id": "mem_general", "summary": "VISIBLE"})
    await call(path, items=items)
    page = await read_memory_usage(path, "goal_a")
    assert page
    assert page.entries[0].items[0].summary == "VISIBLE"
    assert len(page.entries[0].items) == 100 and page.entries[0].omitted_item_count == 31


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,change",
    [
        ("context", "UPDATE goal_contexts SET root_task_id='tsk_b'"),
        ("context", "UPDATE goal_contexts SET goal_run_id='goal_b'"),
        ("context", "UPDATE goal_contexts SET node_id='node_a'"),
        ("job", "UPDATE plan_nodes SET task_id='tsk_b'"),
        ("job", "UPDATE plan_nodes SET node_type='synthesis'"),
        ("job", "UPDATE agent_jobs SET required_skill='writing.draft'"),
        ("job", "UPDATE tasks SET source='goal:goal_b' WHERE id='tsk_child'"),
    ],
)
async def test_exact_relations_only(tmp_path: Path, target: str, change: str) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    await (call(path) if target == "context" else worker(path))
    with sqlite3.connect(path) as db:
        db.execute(change)
    page = await read_memory_usage(path, "goal_a")
    assert page and page.availability == "no_records"


@pytest.mark.asyncio
async def test_pagination_equal_dates_backdated_insert_scope_and_deletion(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    for number in range(5):
        await call(path, str(number))
    whole = await read_memory_usage(path, "goal_a")
    first = await read_memory_usage(path, "goal_a", limit=2)
    assert whole and first and first.next_cursor and len(first.next_cursor) <= 512
    for bad in ("nope", "a" * 513):
        with pytest.raises(MemoryInspectionCursorError):
            await read_memory_usage(path, "goal_a", cursor=bad)
    with pytest.raises(MemoryInspectionCursorError):
        await read_memory_usage(path, "goal_b", cursor=first.next_cursor)
    await call(path, "later", date="2000-01-01T00:00:00+00:00")
    ids = [e.id for e in first.entries]
    cursor = first.next_cursor
    while cursor:
        page = await read_memory_usage(path, "goal_a", limit=2, cursor=cursor)
        assert page
        ids += [e.id for e in page.entries]
        cursor = page.next_cursor
    assert ids == [e.id for e in whole.entries]
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM goal_model_calls WHERE id='gmc_4'")
    with pytest.raises(MemoryInspectionCursorError):
        await read_memory_usage(path, "goal_a", cursor=first.next_cursor)


@pytest.mark.asyncio
async def test_output_bytes_bounded_with_exact_omission_and_no_skipped_receipts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    items = []
    with sqlite3.connect(path) as db:
        for number in range(110):
            identity = f"mem_public{number}"
            db.execute(
                "INSERT INTO memory_items(id,scope,kind,content,created_at,updated_at) VALUES(?,'general','fact','visible',?,?)",
                (identity, STAMP, STAMP),
            )
            items.append({"id": identity, "summary": "🦉" * 800})
    for number in range(3):
        await call(path, str(number), items=items)
    cursor = None
    ids = []
    while True:
        page = await read_memory_usage(path, "goal_a", cursor=cursor)
        assert page
        assert len(page.model_dump_json().encode()) <= MAX_PAGE_BYTES
        for entry in page.entries:
            assert len(entry.items) + entry.omitted_item_count == 110
            ids.append(entry.id)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert ids == ["model_call:gmc_2", "model_call:gmc_1", "model_call:gmc_0"]


@pytest.mark.asyncio
async def test_bad_provenance_unavailable_not_empty_and_private_urls_redacted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    await call(
        path,
        items=[
            {
                "id": "mem_general",
                "summary": "Voir http://127.0.0.1/private?token=123 puis secret=real-secret /home/me/private",
            }
        ],
    )
    page = await read_memory_usage(path, "goal_a")
    assert page
    assert "127.0.0.1" not in page.model_dump_json() and "real-secret" not in page.model_dump_json()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE goal_contexts SET provenance_json='{}'")
    with pytest.raises(MemoryInspectionEvidenceError):
        await read_memory_usage(path, "goal_a")


def test_endpoint_auth_no_store_legacy_and_errors(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI
) -> None:
    path = test_app.state.state_service.db_path
    asyncio.run(seed(path))
    route = "/goals/goal_legacy/memory-usage"
    assert client.get(route).status_code == 401
    response = client.get(route, headers=paired_headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["availability"] == "no_records" and response.json()["project_id"] is None
    assert client.get("/goals/goal_missing/memory-usage", headers=paired_headers).status_code == 404
    assert client.get(route + "?cursor=" + "x" * 513, headers=paired_headers).status_code == 400
    assert client.get(route + "?limit=0", headers=paired_headers).status_code == 422


@pytest.mark.asyncio
async def test_read_connection_is_query_only_and_cannot_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import memory_inspection

    path = tmp_path / "state.db"
    await seed(path)
    await call(path)
    original = memory_inspection._Sources.items

    async def inspect_connection(self, candidates):
        setting = await (await self.db.execute("PRAGMA query_only")).fetchone()
        assert tuple(setting) == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await self.db.execute("DELETE FROM memory_items")
        return await original(self, candidates)

    monkeypatch.setattr(memory_inspection._Sources, "items", inspect_connection)
    page = await read_memory_usage(path, "goal_a")
    assert page and page.entries[0].items


@pytest.mark.asyncio
async def test_evaluator_receipt_requires_exact_provenance_and_preserves_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    await call(path)
    memory = {
        "mode": "semantic",
        "reason": "semantic_ranked",
        "items": [
            {"id": "pmem_a", "source_id": "gmsg_a", "summary": "Exact evaluator quote"},
            {
                "id": "mem_general",
                "source_id": "mem_general",
                "summary": "Not present in provenance",
            },
        ],
    }
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE goal_contexts SET purpose='evaluator', context_json=?,provenance_json=?",
            (
                json.dumps(
                    {"goal_run_id": "goal_a", "conversation_revision": 1, "project_memory": memory}
                ),
                json.dumps({"source_ids": ["pmem_a", "gmsg_a"]}),
            ),
        )
        db.execute("UPDATE goal_model_calls SET role='evaluator'")
    page = await read_memory_usage(path, "goal_a")
    assert page and page.entries[0].purpose == "evaluator"
    assert page.entries[0].retrieval.mode == "semantic"
    assert page.entries[0].items[0].summary == "Exact evaluator quote"
    assert page.entries[0].omitted_item_count == 1
    assert "Not present" not in page.model_dump_json()


@pytest.mark.asyncio
async def test_each_page_rechecks_current_source_sensitivity_and_never_substitutes_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    await call(path, "older")
    await call(path, "newer", date="2026-09-29T22:00:00+00:00")
    first = await read_memory_usage(path, "goal_a", limit=1)
    assert first and first.next_cursor
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE memory_items SET sensitivity='sensitive', content='CURRENT_PRIVATE' WHERE id='mem_general'"
        )
    second = await read_memory_usage(path, "goal_a", limit=1, cursor=first.next_cursor)
    assert second
    item = next(i for i in second.entries[0].items if i.id == "mem_general")
    assert item.source_state == "redacted" and item.summary is None
    assert (
        "Recorded original" not in second.model_dump_json()
        and "CURRENT_PRIVATE" not in second.model_dump_json()
    )


@pytest.mark.asyncio
async def test_model_node_must_link_to_its_actual_goal_task(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    await worker(path)
    await call(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE goal_model_calls SET node_id='node_a'")
        db.execute("UPDATE goal_contexts SET node_id='node_a'")
    page = await read_memory_usage(path, "goal_a")
    assert page and len(page.entries) == 2
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tasks SET source='goal:goal_b' WHERE id='tsk_child'")
    invalid = await read_memory_usage(path, "goal_a")
    assert invalid and invalid.availability == "no_records"


def test_corrupt_receipt_returns_unavailable_instead_of_empty_success(
    client: TestClient, paired_headers: dict[str, str], test_app: FastAPI
) -> None:
    path = test_app.state.state_service.db_path
    asyncio.run(seed(path))
    asyncio.run(call(path, items=[{"id": "mem_general", "summary": "bad\ud800"}]))
    response = client.get("/goals/goal_a/memory-usage", headers=paired_headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "memory receipts unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"goal_run_id": "goal_b"},
        {"conversation_revision": 9},
        {"conversation_revision": True},
        {"project_id": "project_b"},
        {"root_task_id": "tsk_b"},
    ],
)
async def test_evaluator_payload_metadata_matches_historical_call(
    tmp_path: Path, patch: dict
) -> None:
    path = tmp_path / "state.db"
    await seed(path)
    await call(path)
    payload = {
        "goal_run_id": "goal_a",
        "conversation_revision": 1,
        "project_memory": {
            "mode": "lexical",
            "items": [{"id": "pmem_a", "source_id": "gmsg_a", "summary": "Historical quote"}],
        },
        **patch,
    }
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE goal_contexts SET purpose='evaluator', context_json=?,provenance_json=?",
            (json.dumps(payload), json.dumps({"source_ids": ["pmem_a", "gmsg_a"]})),
        )
        db.execute("UPDATE goal_model_calls SET role='evaluator'")
    with pytest.raises(MemoryInspectionEvidenceError):
        await read_memory_usage(path, "goal_a")
