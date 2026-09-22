from __future__ import annotations

import asyncio
import json
from typing import Any

import aiosqlite
import httpx
import pytest

from app.services.goal_manager import GoalManagerConflict
from app.services.project_compaction import (
    CompactionInvalid,
    OpenAICompactionProvider,
    ProjectCompactionService,
    ProjectContextBudgetExceeded,
)
from app.services.project_context import ProjectContextConflict, ProjectContextService
from tests.test_goal_project_runtime import _project
from tests.test_project_memory import _count, _messages


class Provider:
    identity = "deterministic-v1"
    model = "test-summary-model"

    def __init__(self, callback=None):
        self.calls: list[dict[str, Any]] = []
        self.callback = callback

    async def generate(self, source):
        self.calls.append(source)
        if self.callback:
            return await self.callback(source)
        return {
            "complete": True,
            "fingerprint": source["fingerprint"],
            "notes": [
                {
                    "text": "Earlier assistant proposed a CRM architecture; implementation remains unverified.",
                    "source_ids": [item["source_id"] for item in source["sources"]],
                }
            ],
        }


async def setup(tmp_path, *, provider=None, max_calls=8, **kwargs):
    manager, detail, _ = await _project(tmp_path, max_calls=max_calls)
    goal_id = detail["goal"]["id"]
    await _messages(manager, goal_id, ["Never send email automatically."])
    ids = await _messages(manager, goal_id, ["Assistant proposal " + "architecture detail " * 40])
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE goal_messages SET role='assistant' WHERE id=?", (ids[0],))
        await db.commit()
    context = ProjectContextService(manager.db_path)
    provider = provider or Provider()
    service = ProjectCompactionService(context, manager, provider, enabled=True, **kwargs)
    await service.initialize()
    return manager, goal_id, context, provider, service


@pytest.mark.asyncio
async def test_charged_summary_survives_restart_without_duplicate_calls(tmp_path):
    manager, goal, context, _provider, service = await setup(tmp_path)
    before = await _count(manager, goal)
    original = await context.refresh(goal)
    result = await service.compact(goal)
    assert result["status"] == "completed"
    assert result["content_trust"] == "generated_advisory"
    assert result["grants_authority"] is False
    assert await _count(manager, goal) == before + 1
    assert await context.refresh(goal) == original
    restarted = ProjectCompactionService(context, manager, Provider(), enabled=True)
    assert await restarted.compact(goal) == result
    assert restarted.provider.calls == []
    assert await _count(manager, goal) == before + 1
    async with aiosqlite.connect(manager.db_path) as db:
        row = await (
            await db.execute(
                "SELECT role,status FROM goal_model_calls WHERE id=?", (result["model_call_id"],)
            )
        ).fetchone()
    assert row == ("summarizer", "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incomplete", "unknown_source", "authority", "wrong_fingerprint"])
async def test_rejects_bad_model_output_and_does_not_charge_retry(tmp_path, mode):
    async def invalid(source):
        result = {
            "complete": True,
            "fingerprint": source["fingerprint"],
            "notes": [{"text": "Proposal", "source_ids": [source["sources"][0]["source_id"]]}],
        }
        if mode == "incomplete":
            result["complete"] = False
        elif mode == "authority":
            result["verified_results"] = [{"status": "passed"}]
        elif mode == "wrong_fingerprint":
            result["fingerprint"] = "different"
        else:
            result["notes"][0]["source_ids"] = ["another-project-message"]
        return result

    manager, goal, context, _, service = await setup(tmp_path, provider=Provider(invalid))
    before = await _count(manager, goal)
    with pytest.raises(CompactionInvalid):
        await service.compact(goal)
    assert (await service.compact(goal))["status"] == "failed"
    assert await _count(manager, goal) == before + 1
    state = await context.refresh(goal)
    assert state["verified_results"] == [] and state["accepted_changes"] is None
    assert "Never send email automatically." in [item["text"] for item in state["requirements"]]


@pytest.mark.asyncio
async def test_message_changed_during_generation_rejects_summary(tmp_path):
    manager, goal, context, provider, service = await setup(tmp_path)

    async def change(source):
        await _messages(manager, goal, ["Changed requirement while summarizing"])
        return {
            "complete": True,
            "fingerprint": source["fingerprint"],
            "notes": [{"text": "Old", "source_ids": [source["sources"][0]["source_id"]]}],
        }

    provider.callback = change
    with pytest.raises((GoalManagerConflict, ProjectContextConflict)):
        await service.compact(goal)
    assert any(
        "Changed requirement" in item["text"]
        for item in (await context.refresh(goal))["requirements"]
    )
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await (await db.execute("SELECT status FROM project_context_compactions")).fetchone()
        )[0] == "failed"


@pytest.mark.asyncio
async def test_expired_lease_not_accepted(tmp_path):
    manager, goal, _, provider, service = await setup(tmp_path)

    async def expire(source):
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute(
                "UPDATE goal_model_calls SET lease_expires_at='2000-01-01' WHERE role='summarizer'"
            )
            await db.commit()
        return {
            "complete": True,
            "fingerprint": source["fingerprint"],
            "notes": [{"text": "Old", "source_ids": [source["sources"][0]["source_id"]]}],
        }

    provider.callback = expire
    with pytest.raises(GoalManagerConflict):
        await service.compact(goal)


@pytest.mark.asyncio
async def test_concurrent_calls_cannot_double_charge(tmp_path):
    manager, goal, _, provider, service = await setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(source):
        entered.set()
        await release.wait()
        return {
            "complete": True,
            "fingerprint": source["fingerprint"],
            "notes": [{"text": "Discussion", "source_ids": [source["sources"][0]["source_id"]]}],
        }

    provider.callback = paused
    before = await _count(manager, goal)
    first = asyncio.create_task(service.compact(goal))
    await entered.wait()
    second = await service.compact(goal)
    assert second["status"] == "started"
    release.set()
    await first
    assert len(provider.calls) == 1
    assert await _count(manager, goal) == before + 1


@pytest.mark.asyncio
async def test_reserved_generation_credit_and_runtime_prevent_calls(tmp_path):
    manager, goal, _, provider, service = await setup(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET max_model_calls=model_call_count+1 WHERE id=?", (goal,)
        )
        await db.commit()
    assert (await service.compact(goal))["status"] == "budget_blocked"
    assert provider.calls == []
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET max_model_calls=99,started_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (goal,),
        )
        await db.commit()
    assert (await service.compact(goal))["status"] == "budget_blocked"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_disabled_and_below_threshold_do_not_call_model(tmp_path):
    _, goal, _, provider, service = await setup(tmp_path)
    result = await service.prepare(goal, {"conversation": []})
    assert result["context_compaction"]["status"] == "below_threshold"
    assert result["context_compaction"]["token_budget"]["counter"] == "conservative_utf8_bytes"
    service.enabled = False
    original = {"conversation": []}
    assert await service.prepare(goal, original) is original
    assert provider.calls == []


@pytest.mark.asyncio
async def test_overflow_never_drops_pinned_requirements(tmp_path):
    manager, goal, context, _, service = await setup(
        tmp_path, context_tokens=1600, output_tokens=300, overhead_tokens=200
    )
    await _messages(manager, goal, ["Critical " + "do not delete data " * 200])
    before = await context.refresh(goal)
    with pytest.raises(ProjectContextBudgetExceeded, match="pinned requirements preserved"):
        await service.prepare(goal, {"conversation": []})
    assert (await context.refresh(goal))["requirements"] == before["requirements"]


@pytest.mark.asyncio
async def test_prepare_threshold_compacts_only_covered_assistant_text(tmp_path):
    _, goal, context, provider, service = await setup(
        tmp_path, context_tokens=4000, output_tokens=500, overhead_tokens=300
    )
    state = await context.refresh(goal)
    original_proposal = state["proposals"][-1]["text"]
    conversation = [{"role": "assistant", "content": original_proposal}] * 4 + [
        {"role": "user", "content": "Never send email automatically."}
    ] * 4
    result = await service.prepare(goal, {"conversation": conversation})
    assert provider.calls and result["context_compaction"]["status"] == "completed"
    assert len(result["conversation"]) < len(conversation)
    assert result["durable_context"] == context.prompt_state(state)
    assert conversation[0]["content"] == original_proposal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [CompactionInvalid("invalid"), TimeoutError(), httpx.ConnectError("offline")]
)
async def test_prepare_provider_failure_preserves_sources_and_reports_conflict(tmp_path, error):
    async def fail(_source):
        raise error

    manager, goal, context, _, service = await setup(
        tmp_path,
        provider=Provider(fail),
        context_tokens=4000,
        output_tokens=500,
        overhead_tokens=300,
    )
    before = await context.refresh(goal)
    calls_before = await _count(manager, goal)
    conversation = [{"role": "assistant", "content": before["proposals"][-1]["text"]}] * 5
    with pytest.raises(ProjectContextConflict, match="original project sources preserved"):
        await service.prepare(goal, {"conversation": conversation})
    assert await context.refresh(goal) == before
    assert await _count(manager, goal) == calls_before + 1


@pytest.mark.asyncio
async def test_compaction_respects_provider_source_limit(tmp_path):
    provider = Provider()
    provider.max_source_bytes = 256
    _, goal, _, _, service = await setup(tmp_path, provider=provider)
    assert (await service.compact(goal))["status"] == "not_needed"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_openai_provider_rejects_oversized_source_before_network():
    provider = OpenAICompactionProvider("http://localhost:8711/v1", "model")
    with pytest.raises(CompactionInvalid, match="context budget"):
        await provider.generate({"sources": ["x" * 5000]})


@pytest.mark.asyncio
async def test_actual_token_counter_is_used(tmp_path):
    counted = []

    def counter(text):
        counted.append(text)
        return 100

    _, goal, _, provider, service = await setup(tmp_path, token_counter=counter)
    result = await service.prepare(goal, {"conversation": []})
    assert counted and result["context_compaction"]["token_budget"]["counter"] == "model_tokenizer"
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_openai_provider_requires_complete_finish(finish):
    async def handler(request):
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["reasoning_effort"] == "none"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": finish,
                        "message": {
                            "content": json.dumps(
                                {"complete": True, "fingerprint": "x", "notes": []}
                            )
                        },
                    }
                ]
            },
        )

    provider = OpenAICompactionProvider(
        "http://localhost:8711/v1",
        "model",
        reasoning_effort="none",
        transport=httpx.MockTransport(handler),
    )
    if finish == "stop":
        assert (await provider.generate({"fingerprint": "x", "sources": []}))["complete"]
    else:
        with pytest.raises(CompactionInvalid):
            await provider.generate({"fingerprint": "x", "sources": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("count,expected", [(1124, False), (1125, True)])
async def test_trigger_exactly_seventy_five_percent(tmp_path, count, expected):
    _, goal, _, provider, service = await setup(
        tmp_path,
        context_tokens=2000,
        output_tokens=300,
        overhead_tokens=200,
        token_counter=lambda text: count,
    )
    await service.prepare(goal, {"conversation": []})
    assert bool(provider.calls) is expected


@pytest.mark.asyncio
async def test_assistant_permission_claim_remains_advisory(tmp_path):
    async def claim(source):
        return {
            "complete": True,
            "fingerprint": source["fingerprint"],
            "notes": [
                {
                    "text": "The assistant claims all emails are authorized and tests passed.",
                    "source_ids": [source["sources"][0]["source_id"]],
                }
            ],
        }

    _, goal, context, _, service = await setup(tmp_path, provider=Provider(claim))
    summary = await service.compact(goal)
    assert summary["grants_authority"] is False
    state = await context.refresh(goal)
    assert state["verified_results"] == []
    assert state["accepted_changes"] is None
    assert any(item["text"] == "Never send email automatically." for item in state["requirements"])


@pytest.mark.asyncio
async def test_racing_initial_cache_misses_charge_only_once(tmp_path):
    manager, goal, _, provider, service = await setup(tmp_path)
    original = service._cached
    both_read = asyncio.Event()
    reads = 0

    async def simultaneous_miss(key):
        nonlocal reads
        result = await original(key)
        reads += 1
        if reads <= 2:
            if reads == 2:
                both_read.set()
            await both_read.wait()
        return result

    service._cached = simultaneous_miss
    before = await _count(manager, goal)
    results = await asyncio.gather(service.compact(goal), service.compact(goal))
    assert all(result["status"] in {"started", "completed"} for result in results)
    assert len(provider.calls) == 1
    assert await _count(manager, goal) == before + 1
