"""A read passage must retain its admitted search, task, URL and digest identity."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from jsonschema import Draft202012Validator

from app.models import AgentCreate
from app.services.agent_dispatcher import AgentDispatchConflict
from app.services.remote_job_policy import validate_remote_job
from app.services.result_aggregator import summarize_untrusted_worker_output
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest, SwarmPlanProposal
from app.services.worker_context import read_worker_context
from app.services.writing_contracts import WritingResearchSource
from app.services.writing_drafts import read_research_sources
from tests.test_goal_manager import _manager
from tests.test_planner_worker_argument_schema import schema_and_wire

REQUEST = {
    "focus": "Comparer deux formats pour conserver des notes personnelles.",
    "queries": [
        "format A persistence official documentation",
        "format B serialization documentation",
    ],
    "max_results_per_query": 1,
    "max_pages": 2,
}


def receipt() -> dict[str, Any]:
    sources = [
        {
            "title": "Format " + label,
            "url": f"https://example.org/{label}",
            "snippet": "Extrait de recherche.",
            "query_indices": [i],
        }
        for i, label in enumerate(("a", "b"))
    ]
    return {
        "schema_version": "1.0",
        "content_trust": "untrusted",
        "collection_status": "complete",
        "searches": [
            {
                "query": query,
                "status": "completed",
                "searched_at": "2026-09-29T02:00:00Z",
                "result_urls": [sources[i]["url"]],
            }
            for i, query in enumerate(REQUEST["queries"])
        ],
        "results": sources,
        "pages": [
            {
                "requested_url": item["url"],
                "final_url": item["url"],
                "status": "read",
                "fetched_at": "2026-09-29T02:00:01Z",
                "http_status": 200,
                "content_type": "text/plain",
                "text": "Documentation: " + item["title"],
                "content_sha256": hashlib.sha256(
                    ("Documentation: " + item["title"]).encode()
                ).hexdigest(),
                "body_sha256": hashlib.sha256(
                    ("Documentation: " + item["title"]).encode()
                ).hexdigest(),
                "truncated": False,
            }
            for item in sources
        ],
    }


def collection_node() -> dict[str, Any]:
    return {
        "temporary_id": "research",
        "node_type": "worker",
        "title": "Documenter les formats",
        "objective": REQUEST["focus"],
        "required_skill": "research.collect",
        "dependencies": [],
        "expected_output": "Passages sourcés des deux formats, avec limites.",
        "priority": 10,
        "worker_arguments": copy.deepcopy(REQUEST),
    }


async def started(tmp_path: Path) -> tuple[Any, dict[str, Any], list[str], dict[str, Any]]:
    proposal = SwarmPlanProposal.model_validate(
        {
            "schema_version": "1.0",
            "objective": REQUEST["focus"],
            "rationale_summary": "Lire puis rédiger.",
            "completion_criteria": ["Comparer les deux formats avec leurs sources."],
            "max_parallelism": 1,
            "nodes": [
                collection_node(),
                {
                    "temporary_id": "write",
                    "node_type": "worker",
                    "title": "Comparer les formats",
                    "objective": REQUEST["focus"],
                    "required_skill": "writing.draft",
                    "dependencies": ["research"],
                    "expected_output": "Comparaison sourcée",
                    "priority": 5,
                },
            ],
        }
    )
    manager = await _manager(tmp_path / "collect.db", proposal)
    agents = []
    for skill in ("research.collect", "writing.draft"):
        agent = await manager.state_service.register_agent(
            AgentCreate(name=skill, endpoint="https://worker.invalid", skills=[skill]), "phone"
        )
        await manager.state_service.heartbeat_agent(agent["id"], "online", agent["credential"])
        agents.append(agent["id"])
    goal = await manager.create_goal(
        GoalCreateRequest(objective=REQUEST["focus"]), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agents[0])
    assert job and job["payload"] == REQUEST
    return manager, goal, agents, job


async def submit(manager: Any, agent: str, job: dict[str, Any], value: dict[str, Any]) -> Any:
    completed, _ = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="completed",
        result=value,
        error=None,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    await manager.on_job_result(completed)
    return completed


@pytest.mark.asyncio
async def test_admitted_collect_passages_reach_writer_and_readback(tmp_path: Path) -> None:
    manager, goal, agents, job = await started(tmp_path)
    await submit(manager, agents[0], job, receipt())
    writer = await manager.agent_dispatcher.claim(agents[1])
    assert writer is not None
    sources = writer["payload"]["research_sources"]
    assert [s["url"] for s in sources] == ["https://example.org/a", "https://example.org/b"]
    for source in sources:
        assert source["worker_job_id"] == job["id"]
        assert source["evidence"]["text"].startswith("Documentation:")
        assert source["evidence"]["fetched_at"] == "2026-09-29T02:00:01Z"
        assert WritingResearchSource.model_validate(source).model_dump() == source
    assert validate_remote_job("writing.draft", writer["payload"]) == writer["payload"]
    node = next(
        n
        for n in await manager.graph.list_nodes(goal["id"])
        if n["required_skill"] == "writing.draft"
    )
    assert await read_research_sources(manager.db_path, goal["id"], node["id"]) == sources
    assert "not task fulfillment" in summarize_untrusted_worker_output(receipt())
    # A persisted result cannot later be reused for an unrelated admitted query.
    async with aiosqlite.connect(manager.db_path) as db:
        changed = {**REQUEST, "queries": ["other", REQUEST["queries"][1]]}
        await db.execute(
            "UPDATE agent_jobs SET payload_json=? WHERE id=?", (json.dumps(changed), job["id"])
        )
        await db.commit()
    with pytest.raises(ValueError, match="does not match its input"):
        await read_worker_context(manager.db_path, goal["id"], node["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["query", "extra_page", "text_hash", "foreign_url"])
async def test_mismatched_collect_receipt_cannot_complete_job(tmp_path: Path, change: str) -> None:
    manager, _, agents, job = await started(tmp_path)
    value = receipt()
    if change == "query":
        value["searches"][0]["query"] = "different request"
    elif change == "extra_page":
        value["pages"].append(copy.deepcopy(value["pages"][0]))
    elif change == "foreign_url":
        value["pages"][0]["requested_url"] = "https://other.example.org/page"
    else:
        value["pages"][0]["text"] = "Changed since hash."
    with pytest.raises(AgentDispatchConflict, match="invalid_research_collection_receipt"):
        await submit(manager, agents[0], job, value)
    writer = await manager.agent_dispatcher.claim(agents[1])
    assert writer is None


@pytest.mark.parametrize("consumer", ["planner", "evaluator"])
def test_collect_grammar_requires_explicit_queries_and_is_advertised_only(consumer: str) -> None:
    schema, wire = schema_and_wire(consumer, collection_node())
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(wire)) == []
    broken = collection_node()
    broken.pop("worker_arguments")
    with pytest.raises(ValueError, match="explicit bounded queries"):
        SwarmPlanProposal.model_validate(
            {
                "schema_version": "1.0",
                "objective": "Compare formats",
                "rationale_summary": "Read evidence",
                "completion_criteria": ["Compare"],
                "max_parallelism": 1,
                "nodes": [broken],
            }
        )


def test_page_excerpt_contract_matches_standalone_consumers() -> None:
    from app.services.research_contracts import project_research_collect_sources

    root = Path(__file__).resolve().parents[2]
    sources = project_research_collect_sources(receipt(), "job_collection")
    for name in ("text-worker/text_worker.py", "project-worker/project_contract.py"):
        spec = importlib.util.spec_from_file_location(
            "consumer_" + name.split("/")[0], root / "workers" / name
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parse = (
            module._research_sources if name.startswith("text") else module.research_sources_value
        )
        assert parse(sources) == sources
        for field, wrong in (
            ("excerpt_sha256", "0" * 64),
            ("final_url", "https://example.org/other"),
            ("requested_url", "https://127.0.0.1/private"),
            ("fetched_at", "2026-01-01"),
        ):
            bad = copy.deepcopy(sources)
            bad[0]["evidence"][field] = wrong
            with pytest.raises(ValueError):
                parse(bad)
            with pytest.raises(ValueError):
                WritingResearchSource.model_validate(bad[0])


def test_real_collect_receipt_preserves_direct_source_through_projection() -> None:
    import time

    from app.services.research_contracts import (
        project_research_collect_sources,
        valid_research_collect_receipt,
    )

    path = Path(__file__).resolve().parents[2] / "workers/research-worker/research_collect.py"
    spec = importlib.util.spec_from_file_location("standalone_collect", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    request = {
        "focus": "transaction concurrency",
        "queries": ["persistence"],
        "source_urls": ["https://docs.example.org/known"],
        "required_domains": ["example.org"],
    }
    value = module.collect(
        request,
        lambda q, n: {"content_trust": "untrusted", "results": []},
        lambda u, d: module.RawPage(
            200,
            {"content-type": "text/html"},
            (
                "<main>"
                + "<p>Unrelated introduction.</p>" * 250
                + "<p>Transaction concurrency permits one writer.</p></main>"
            ).encode(),
        ),
        check_active=lambda: None,
        deadline=time.monotonic() + 10,
    )
    assert valid_research_collect_receipt(value, request)
    source = project_research_collect_sources(value, "job_collection")[0]
    assert source["snippet"] == ""
    assert "Transaction concurrency permits one writer." in source["evidence"]["text"]
    assert WritingResearchSource.model_validate(source).evidence is not None
