from __future__ import annotations

import copy
import hashlib

import pytest

from app.services.agent_capsule import required_capsule_identity, validate_agent_capsule
from app.services.project_compaction import ProjectCompactionService, ProjectContextBudgetExceeded
from app.services.project_context import ProjectContextService
from tests.test_goal_project_runtime import _project
from tests.test_project_compaction import Provider, setup
from tests.test_project_memory import _count, _messages


@pytest.mark.asyncio
async def test_small_project_fits_after_optional_context_is_selected(tmp_path):
    manager, goal, context, provider, service = await setup(
        tmp_path, context_tokens=10_000, output_tokens=2_000, overhead_tokens=1_000
    )
    requirement = "Keep all data local and never send a network request. " * 18
    await _messages(manager, goal, [requirement])
    payload = {
        "objective": "Finish the task interface and validate it.",
        "conversation": [
            {"role": "user", "content": requirement},
            {"role": "assistant", "content": "Historical proposal. " * 45},
            {"role": "user", "content": requirement},
            {"role": "assistant", "content": "Latest check failed: missing app.js."},
        ],
        "files": [{"path": "index.html", "content": "<main>TODO</main>"}],
        "base_revision_id": "revision_current",
        "base_sha256": "a" * 64,
        "focus_paths": ["index.html"],
        "checks": [{"status": "failed", "output": "app.js is missing", "exit_code": 1}],
        "research_sources": [{"worker_job_id": "job_research", "url": "https://docs.test"}],
        "dependency_context": [{"worker_job_id": "job_dependency", "summary": "Required"}],
        "memory": {
            "mode": "lexical",
            "reason": "historical",
            "items": [
                {
                    "id": f"pmem_{i}",
                    "source_id": f"msg_{i}",
                    "score": 0.8,
                    "summary": f"Older hint {i}. " + "Earlier proposed architecture. " * 35,
                }
                for i in range(4)
            ],
        },
    }
    original = copy.deepcopy(payload)
    state = await context.refresh(goal)
    calls = await _count(manager, goal)
    result = await service.prepare(goal, payload)

    assert service._count(result) <= 7_000
    assert result["durable_context"] == context.prompt_state(state)
    assert result["conversation"][-1] == original["conversation"][-1]
    assert [m for m in result["conversation"] if m["role"] == "user"] == [
        original["conversation"][2]
    ]
    for field in (
        "files",
        "base_revision_id",
        "base_sha256",
        "focus_paths",
        "checks",
        "research_sources",
        "dependency_context",
    ):
        assert result[field] == original[field]
    assert result["context_compaction"]["selection"]["memory_items_omitted"] > 0
    assert result["context_compaction"]["selection"]["pinned_user_copies_omitted"] == 1
    assert payload == original
    assert await context.refresh(goal) == state
    assert provider.calls == [] and await _count(manager, goal) == calls


@pytest.mark.asyncio
async def test_exact_duplicate_feedback_does_not_displace_distinct_corrections(tmp_path):
    _, goal, _, provider, service = await setup(tmp_path)
    payload = {
        "conversation": [
            {"role": "assistant", "content": "Use JSON."},
            {"role": "assistant", "content": "Correction: use SQLite."},
            {"role": "assistant", "content": "Use JSON."},
            {"role": "user", "content": "A new instruction not yet in the pinned state."},
        ]
    }
    result = await service.prepare(goal, payload)
    assert result["conversation"] == payload["conversation"][1:]
    assert result["context_compaction"]["selection"]["assistant_copies_omitted"] == 1
    assert provider.calls == []


@pytest.mark.asyncio
async def test_irreducible_payload_fails_before_charging_compaction(tmp_path):
    manager, goal, context, provider, service = await setup(
        tmp_path, context_tokens=4_000, output_tokens=500, overhead_tokens=300
    )
    await _messages(manager, goal, ["Mandatory original requirement. " * 120])
    state = await context.refresh(goal)
    calls = await _count(manager, goal)
    with pytest.raises(ProjectContextBudgetExceeded, match="pinned requirements preserved"):
        await service.prepare(goal, {"conversation": []})
    assert provider.calls == []
    assert await _count(manager, goal) == calls
    assert await context.refresh(goal) == state


@pytest.mark.asyncio
async def test_experience_selection_keeps_recent_sources_and_full_required_guides(
    tmp_path, monkeypatch
):
    _, goal, context, provider, service = await setup(
        tmp_path, context_tokens=8_000, output_tokens=500, overhead_tokens=300
    )
    state = await context.refresh(goal)
    guide = "Preserve all local files and validate the current source. " * 20
    state["project_guidance"] = [
        {
            "path": "AGENTS.md",
            "scope": "",
            "source_revision_id": "revision_current",
            "sha256": hashlib.sha256(guide.encode()).hexdigest(),
            "content": guide,
        }
    ]
    state["base_revision_id"] = "revision_current"
    state["experiences"] = {
        "omitted_count": 2,
        "items": [
            {
                "source_id": f"node_{i}",
                "worker_job_id": f"job_{i}",
                "required_skill": "code.build_project",
                "outcome": "failed",
                "observation_kind": "measured_failure",
                "source_revision_id": f"revision_{i}",
                "source_sha256": str(i) * 64,
                "summary": f"Observation {i}: " + "check failed " * 45,
                "content_trust": "untrusted",
                "applicability": "historical",
            }
            for i in range(6)
        ],
    }
    original = copy.deepcopy(state)

    async def frozen(_goal, *, expected_fingerprint=None):
        assert expected_fingerprint in {None, original["fingerprint"]}
        return copy.deepcopy(state)

    monkeypatch.setattr(context, "refresh", frozen)
    capsule = context.prompt_state(state)
    payload = {
        "conversation": [{"role": "assistant", "content": "Latest repair: fix app.js."}],
        "files": [{"path": "AGENTS.md", "content": guide}],
        "base_revision_id": "revision_current",
        "base_sha256": "a" * 64,
    }
    result = await service.prepare(goal, payload)
    projected = validate_agent_capsule(result["durable_context"])
    remaining = projected["experiences"]["items"]
    assert 0 < len(remaining) < 6
    assert remaining == original["experiences"]["items"][: len(remaining)]
    assert projected["experiences"]["omitted_count"] == 2 + 6 - len(remaining)
    assert required_capsule_identity(projected) == required_capsule_identity(capsule)
    assert projected["fingerprint"] == capsule["fingerprint"]
    assert result["files"] == payload["files"]
    assert state == original and provider.calls == []
    assert result["context_compaction"]["token_budget"]["input_tokens"] == service._count(result)


@pytest.mark.asyncio
async def test_project_handoffs_are_measured_before_admission(tmp_path):
    manager, detail, _ = await _project(tmp_path)
    goal = detail["goal"]["id"]
    project = manager.project_applications
    assert project is not None
    context = ProjectContextService(manager.db_path)
    provider = Provider()
    project.context = context
    project.compaction = ProjectCompactionService(
        context,
        manager,
        provider,
        enabled=True,
        context_tokens=5_000,
        output_tokens=500,
        overhead_tokens=300,
    )
    await project.compaction.initialize()
    conversation = await manager.recent_conversation(goal)
    node = detail["nodes"][0]
    before = await project.payload(goal, node, conversation)
    assert before["research_sources"] == []
    calls = await _count(manager, goal)
    dependencies = [
        {
            "content_trust": "untrusted",
            "node_id": "node_source",
            "worker_job_id": "job_source",
            "required_skill": "research.query",
            "summary": "Required source handoff. " * 80,
        }
    ]
    sources = [
        {
            "content_trust": "untrusted",
            "worker_job_id": "job_source",
            "title": "Source",
            "url": "https://example.org/reference",
            "snippet": "Source excerpt. " * 40,
        }
    ]
    with pytest.raises(ProjectContextBudgetExceeded, match="pinned requirements preserved"):
        await project.payload(
            goal, node, conversation, dependency_context=dependencies, research_sources=sources
        )
    assert await _count(manager, goal) == calls and provider.calls == []
    # The gateway neither drops required evidence nor mutates the caller's data.
    assert sources[0]["snippet"] == "Source excerpt. " * 40
    assert dependencies[0]["summary"] == "Required source handoff. " * 80
