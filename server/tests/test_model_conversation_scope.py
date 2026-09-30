from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.execution_engine import ExecutionEngine
from app.services.goal_manager import GoalManagerConflict
from app.services.goal_project import GoalProjectService
from app.services.planner_continuation_context import continuation_cards
from app.services.planner_provider import DeterministicSwarmPlannerProvider
from app.services.project_compaction import ProjectCompactionService
from app.services.project_context import ProjectContextService
from app.services.swarm_contracts import GoalCreateRequest, GoalMessageRequest, GoalStartRequest
from app.services.writing_drafts import writing_completion_failure_locked
from tests.test_goal_context_payloads import _CapturingEvaluator, _CapturingPlanner, _synthesis_plan
from tests.test_goal_runtime_recovery import _manager, _worker_plan
from tests.test_goal_writing import RESULT, plan, register
from tests.test_project_compaction import Provider

OBJECTIVE = "Compare local storage choices."
FOREIGN = "ORIGIN_ALPHA must remain under the Alpha storage policy."
OWN = "ORIGIN_BETA must remain under the Beta storage policy."


async def _lineage(tmp_path: Path, scope: str = "split") -> tuple[Any, str, str]:
    manager = await _manager(tmp_path / "state.db", _worker_plan())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    projects = GoalProjectService(
        manager.db_path, ExecutionEngine(manager.db_path, workspace, manager.permission_policy)
    )
    manager.project_applications = projects
    parent = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.reply_goal(
        parent["id"],
        GoalMessageRequest(message=FOREIGN, client_message_id="parent-guidance"),
        actor_id="phone",
    )
    await manager.cancel_goal(parent["id"], actor_id="phone")
    child = await manager.reply_goal(
        parent["id"],
        GoalMessageRequest(message=OWN, client_message_id="child-guidance"),
        actor_id="phone",
    )
    child_id = child["goal"]["id"]
    # Explicit historical fixtures: new continuations now share one identity,
    # but readers must still handle split, missing and ambiguous old mappings.
    async with aiosqlite.connect(manager.db_path) as db:
        if scope == "split":
            await db.execute("INSERT INTO coding_projects VALUES('legacy_split','now','now')")
            await db.execute(
                "UPDATE goal_project_links SET project_id='legacy_split' WHERE goal_run_id=?",
                (child_id,),
            )
        if scope in {"unlinked", "target_linked"}:
            await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (parent["id"],))
        if scope in {"unlinked", "source_linked"}:
            await db.execute("DELETE FROM goal_project_links WHERE goal_run_id=?", (child_id,))
        await db.commit()
    return manager, str(parent["id"]), child_id


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["split", "same", "unlinked", "source_linked", "target_linked"])
async def test_model_history_resolves_source_scope_without_changing_ui_history(
    tmp_path: Path, scope: str
) -> None:
    manager, _, child = await _lineage(tmp_path, scope)
    history = await manager.conversation_messages(child)
    assert FOREIGN in [item["content"] for item in history["messages"]]
    result = await manager.recent_conversation(child)
    assert OWN in [item["content"] for item in result]
    assert (FOREIGN in [item["content"] for item in result]) == (scope in {"same", "unlinked"})
    assert await manager.conversation_messages(child) == history


@pytest.mark.asyncio
async def test_foreign_saturation_is_filtered_before_limit(tmp_path: Path) -> None:
    manager, parent, child = await _lineage(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        conversation = await (
            await db.execute(
                "SELECT conversation_id FROM goal_conversation_links WHERE goal_run_id=?", (child,)
            )
        ).fetchone()
        await db.executemany(
            """INSERT INTO goal_messages(id,conversation_id,goal_run_id,role,content,created_at)
            VALUES(?,?,?,'user',?,'2026-09-29T00:00:00Z')""",
            [(f"gmsg_foreign_{index}", conversation[0], parent, FOREIGN) for index in range(110)],
        )
        await db.commit()
    assert await manager.recent_conversation(child, limit=1) == [{"role": "user", "content": OWN}]


@pytest.mark.asyncio
async def test_source_goal_and_conversation_must_agree(tmp_path: Path) -> None:
    manager, _, child = await _lineage(tmp_path)
    other = await manager.create_goal(
        GoalCreateRequest(objective="Other lineage"), actor_id="phone"
    )
    async with aiosqlite.connect(manager.db_path) as db:
        # Synthetic invalid ownership: a shared project never licenses a message
        # whose actual owner belongs to another conversation.
        await db.execute(
            "UPDATE goal_project_links SET project_id=(SELECT project_id FROM goal_project_links WHERE goal_run_id=?) WHERE goal_run_id=?",
            (child, other["id"]),
        )
        await db.execute(
            """UPDATE goal_messages SET goal_run_id=? WHERE goal_run_id=? AND content=?""",
            (other["id"], child, OWN),
        )
        await db.commit()
    # A message attached to the target conversation but owned by a different
    # conversation does not gain authority merely by sharing its project.
    assert await manager.recent_conversation(child) == []
    assert await continuation_cards(manager.db_path, child) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["split", "same", "unlinked", "source_linked", "target_linked"])
async def test_protected_latest_instruction_has_the_same_scope_as_model_history(
    tmp_path: Path, scope: str
) -> None:
    manager, parent, child = await _lineage(tmp_path, scope)
    async with aiosqlite.connect(manager.db_path) as db:
        conversation = await (
            await db.execute(
                "SELECT conversation_id FROM goal_conversation_links WHERE goal_run_id=?", (child,)
            )
        ).fetchone()
        await db.executemany(
            """INSERT INTO goal_messages(id,conversation_id,goal_run_id,role,content,created_at)
            VALUES(?,?,?,'user',?,'2026-09-29T00:00:00Z')""",
            [(f"gmsg_foreign_{index}", conversation[0], parent, FOREIGN) for index in range(110)],
        )
        await db.commit()
    cards = await continuation_cards(manager.db_path, child)
    instruction = next(card.summary for card in cards if card.kind == "latest_user_message")
    assert instruction == (FOREIGN if scope in {"same", "unlinked"} else OWN)


@pytest.mark.asyncio
async def test_missing_goal_keeps_explicit_failure(tmp_path: Path) -> None:
    manager, _, _ = await _lineage(tmp_path)
    with pytest.raises(GoalManagerConflict, match="goal not found"):
        await manager.recent_conversation("goal_missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", ["writing.draft", "code.build_project"])
@pytest.mark.parametrize("compact", [False, True])
async def test_worker_payload_uses_scoped_conversation(
    tmp_path: Path, skill: str, compact: bool
) -> None:
    manager, _, child = await _lineage(tmp_path)
    context = ProjectContextService(manager.db_path)
    manager.project_applications.context = context
    provider = Provider()
    if compact:
        service = ProjectCompactionService(context, manager, provider, enabled=True)
        await service.initialize()
        manager.project_applications.compaction = service
    payload = await manager._worker_payload(
        await manager.graph.get_goal(child),
        {"goal_run_id": child, "required_skill": skill, "objective": OWN},
    )
    assert provider.calls == []
    assert FOREIGN not in [item["content"] for item in payload["conversation"]]
    assert OWN in [item["content"] for item in payload["conversation"]]


@pytest.mark.asyncio
async def test_planner_and_evaluator_receive_scoped_conversation(tmp_path: Path) -> None:
    manager, _, child = await _lineage(tmp_path)
    planner = _CapturingPlanner(_synthesis_plan(OBJECTIVE, suffix="scope"))
    evaluator = _CapturingEvaluator()
    manager.planner = planner
    manager.evaluator = evaluator
    await manager.start_goal(child, GoalStartRequest())
    assert planner.payloads and evaluator.contexts
    assert FOREIGN not in json.dumps(planner.payloads)
    assert OWN in json.dumps(planner.payloads)
    assert FOREIGN not in json.dumps([context.model_dump() for context in evaluator.contexts])
    assert OWN in json.dumps([context.model_dump() for context in evaluator.contexts])


@pytest.mark.asyncio
@pytest.mark.parametrize("message_owner", ["own", "foreign"])
async def test_completion_constraints_follow_the_same_scope_as_writer_payload(
    tmp_path: Path, message_owner: str
) -> None:
    manager, parent, child = await _lineage(tmp_path, "same")
    await manager.reply_goal(
        child,
        GoalMessageRequest(
            message="Write a note of 150 to 200 words.", client_message_id="own-word-limits"
        ),
        actor_id="phone",
    )
    # Build the accepted reply first, then reproduce an old split mapping.
    # New replies correctly reject a known conflicting lineage.
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("INSERT INTO coding_projects VALUES('legacy_split','now','now')")
        await db.execute(
            "UPDATE goal_project_links SET project_id='legacy_split' WHERE goal_run_id=?", (child,)
        )
        await db.commit()
    manager.planner = DeterministicSwarmPlannerProvider(
        plan().model_copy(update={"objective": OBJECTIVE}, deep=True)
    )
    agent = await register(manager)
    await manager.start_goal(child, GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None
    assert job["payload"]["requirements"] == {"min_words": 150, "max_words": 200}
    recorded, _ = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="completed",
        result={**RESULT, "text": "word " * 160},
        error=None,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
    )
    await manager.on_job_result(recorded)
    async with aiosqlite.connect(manager.db_path) as db:
        conversation = await (
            await db.execute(
                "SELECT conversation_id FROM goal_conversation_links WHERE goal_run_id=?", (child,)
            )
        ).fetchone()
        # Isolate the constraint gate from revision fencing: a same-revision
        # stored instruction must still be checked against its own source scope.
        await db.execute(
            """INSERT INTO goal_messages(id,conversation_id,goal_run_id,role,content,created_at)
            VALUES('gmsg_later_requirement',?,?,'user',?,'2026-09-29T00:00:00Z')""",
            (
                conversation[0],
                child if message_owner == "own" else parent,
                "Write a note of 300 to 350 words.",
            ),
        )
        await db.commit()
        failure = await writing_completion_failure_locked(db, child)
    assert failure == ("writing_requirements_unmet" if message_owner == "own" else None)
