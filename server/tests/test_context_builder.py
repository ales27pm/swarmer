from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from app.models import TaskCreate, TaskRecord
from app.services.context_builder import ContextBuilder, ContextCard
from app.services.state_service import StateService
from app.services.swarm_contracts import GoalEvaluationContext


async def _seed_goal(db_path: Path) -> tuple[str, str, str, str]:
    state = StateService(db_path)
    await state.initialize()
    root = await state.create_task(
        TaskRecord.new(
            TaskCreate(input="Summarize password=hunter2 from /home/alice/private"),
            source="test",
        )
    )
    now = datetime.now(UTC).isoformat()
    goal_id = "goal_context_test"
    upstream_id = "node_upstream"
    node_id = "node_target"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO goal_runs(
                id,root_task_id,objective,status,autonomy_profile,planner_source,max_steps,
                max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
                completion_criteria_json,current_phase,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                goal_id,
                root.id,
                "Produce a safe project summary",
                "running",
                "assisted",
                "ubuntu_local",
                8,
                2,
                1,
                600,
                5,
                json.dumps(["Produce a safe result"]),
                "executing",
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO plan_nodes(
                id,goal_run_id,node_type,title,objective,required_skill,status,
                expected_output,result_summary,created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                upstream_id,
                goal_id,
                "worker",
                "Prior read",
                "Read token=upstream-secret",
                "workspace.read_text",
                "completed",
                "A bounded summary",
                "Found /root/private and password: prior-secret " + "x" * 200,
                now,
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO plan_nodes(
                id,goal_run_id,node_type,title,objective,required_skill,status,
                expected_output,error_summary,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                node_id,
                goal_id,
                "worker",
                "Target",
                "Inspect api_key=target-secret",
                "workspace.list_dir",
                "ready",
                "Safe listing",
                None,
                now,
                now,
            ),
        )
        await db.execute(
            "INSERT INTO plan_edges(goal_run_id,from_node_id,to_node_id,dependency_type) "
            "VALUES(?,?,?,?)",
            (goal_id, upstream_id, node_id, "hard"),
        )
        await db.execute(
            """
            INSERT INTO memory_items(
                id,scope,kind,content,summary,sensitivity,confidence,pinned,
                metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "mem_context",
                "goal",
                "fact",
                "Bearer abcdefghijklmnop at /etc/private",
                None,
                "normal",
                0.9,
                1,
                None,
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO memory_items(
                id,scope,kind,content,summary,sensitivity,confidence,pinned,
                metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "mem_context_sensitive",
                "goal",
                "fact",
                "SENSITIVE_MEMORY_MUST_NOT_ENTER_MODEL_CONTEXT",
                None,
                "secret",
                1.0,
                1,
                None,
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO agents(
                id,name,version,endpoint,model_id,status,skills_json,last_seen_at,
                max_concurrency,capacity_json,runtime,supported_protocol_version,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "agent_context",
                "Context worker",
                "1.2.3",
                "https://worker.invalid",
                "local-model",
                "online",
                json.dumps(["workspace.list_dir"]),
                now,
                1,
                "{}",
                "python",
                "mongars-worker-v0.9",
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO plan_nodes(
                id,goal_run_id,node_type,title,objective,status,expected_output,
                error_summary,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "node_failed",
                goal_id,
                "synthesis",
                "Failed synthesis",
                "Combine safely",
                "failed",
                "Final answer",
                "authorization=forbidden-secret at /var/private",
                now,
                now,
            ),
        )
        await db.commit()
    return goal_id, root.id, upstream_id, node_id


async def _seed_budget_sources(
    db_path: Path,
    *,
    goal_id: str,
    node_id: str,
) -> None:
    state = StateService(db_path)
    historical_roots = [
        await state.create_task(
            TaskRecord.new(TaskCreate(input=f"Historical objective {index}"), source="test")
        )
        for index in range(1, 4)
    ]
    now = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(db_path) as db:
        for index in range(2, 4):
            upstream_id = f"node_upstream_{index}"
            await db.execute(
                """
                INSERT INTO plan_nodes(
                    id,goal_run_id,node_type,title,objective,required_skill,status,
                    expected_output,result_summary,created_at,updated_at,completed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    upstream_id,
                    goal_id,
                    "worker",
                    f"Prior read {index}",
                    f"Read upstream {index}",
                    "workspace.read_text",
                    "completed",
                    "A bounded summary",
                    f"result-{index}-" + ("y" * 120) + f"-tail-{index}",
                    now,
                    now,
                    now,
                ),
            )
            await db.execute(
                "INSERT INTO plan_edges(goal_run_id,from_node_id,to_node_id,dependency_type) "
                "VALUES(?,?,?,?)",
                (goal_id, upstream_id, node_id, "hard"),
            )
            await db.execute(
                """
                INSERT INTO memory_items(
                    id,scope,kind,content,summary,sensitivity,confidence,pinned,
                    metadata_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"mem_context_{index}",
                    "goal",
                    "fact",
                    f"Memory summary {index}",
                    None,
                    "normal",
                    0.8,
                    1,
                    None,
                    now,
                    now,
                ),
            )
            await db.execute(
                """
                INSERT INTO agents(
                    id,name,version,endpoint,model_id,status,skills_json,last_seen_at,
                    max_concurrency,capacity_json,runtime,supported_protocol_version,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"agent_context_{index}",
                    f"Context worker {index}",
                    "1.2.3",
                    f"https://worker-{index}.invalid",
                    "local-model",
                    "online",
                    json.dumps(["workspace.list_dir"]),
                    now,
                    1,
                    "{}",
                    "python",
                    "mongars-worker-v0.9",
                    now,
                    now,
                ),
            )
        for index, root in enumerate(historical_roots, start=1):
            historical_goal_id = f"goal_history_{index}"
            await db.execute(
                """
                INSERT INTO goal_runs(
                    id,root_task_id,objective,status,autonomy_profile,planner_source,max_steps,
                    max_parallelism,max_replans,max_runtime_seconds,max_model_calls,
                    completion_criteria_json,current_phase,created_at,updated_at,completed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    historical_goal_id,
                    root.id,
                    f"Historical goal {index}",
                    "completed",
                    "assisted",
                    "ubuntu_local",
                    4,
                    1,
                    0,
                    60,
                    2,
                    "[]",
                    "completed",
                    now,
                    now,
                    now,
                ),
            )
            await db.execute(
                """
                INSERT INTO episodes(
                    id,goal_run_id,root_task_id,objective_summary,plan_summary,outcome,
                    score,duration_ms,worker_types_json,failure_tags_json,
                    user_feedback_score,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"episode_context_{index}",
                    historical_goal_id,
                    root.id,
                    f"Historical summary {index}",
                    "Read then synthesize",
                    "completed",
                    0.9,
                    100,
                    '["worker"]',
                    "[]",
                    4.0,
                    now,
                    now,
                ),
            )
        await db.commit()


@pytest.mark.asyncio
async def test_context_is_deterministic_bounded_provenanced_and_redacted(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    goal_id, root_id, upstream_id, node_id = await _seed_goal(db_path)
    builder = ContextBuilder(
        db_path,
        max_tokens=2_048,
        max_items=20,
        max_agent_cards=1,
        max_upstream_chars=72,
    )
    await builder.initialize()

    first = await builder.build(goal_run_id=goal_id, node_id=node_id, purpose="planner")
    second = await builder.build(goal_run_id=goal_id, node_id=node_id, purpose="planner")

    assert first.cards == second.cards
    assert first.provenance_ids == second.provenance_ids
    assert first.card_ids == second.card_ids
    assert first.approx_token_count == second.approx_token_count <= 2_048
    assert root_id in first.provenance_ids
    assert node_id in first.provenance_ids
    assert upstream_id in first.provenance_ids
    assert "agent:agent_context:1.2.3" in first.card_ids
    upstream_chars = sum(len(card.summary) for card in first.cards if card.kind == "upstream")
    assert upstream_chars <= 72

    encoded = json.dumps(first.as_dict(), sort_keys=True)
    for forbidden in (
        "hunter2",
        "upstream-secret",
        "prior-secret",
        "target-secret",
        "forbidden-secret",
        "abcdefghijklmnop",
        "/home/alice",
        "/root/private",
        "/etc/private",
        "/var/private",
        "SENSITIVE_MEMORY_MUST_NOT_ENTER_MODEL_CONTEXT",
    ):
        assert forbidden not in encoded
    assert "<redacted-secret>" in encoded
    assert "<protected-path>" in encoded

    restored = await builder.get(first.id)
    assert restored == first
    with pytest.raises(ValueError, match="bounded material"):
        await builder.extend_provenance(
            first.id,
            ("ep_selected", "mem_selected", "ep_selected"),
        )
    extended = await builder.get(first.id)
    assert extended is not None
    assert extended.cards == first.cards
    assert extended.provenance_ids == first.provenance_ids
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT context_json,provenance_json FROM goal_contexts WHERE id=?",
                (first.id,),
            )
        ).fetchone()
    assert row is not None
    persisted = f"{row[0]} {row[1]}"
    assert "hunter2" not in persisted
    assert "/home/alice" not in persisted


@pytest.mark.asyncio
async def test_context_truncation_is_stable_and_respects_item_and_token_budgets(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, node_id = await _seed_goal(db_path)
    builder = ContextBuilder(
        db_path,
        max_tokens=96,
        max_items=3,
        max_agent_cards=4,
        max_upstream_chars=500,
    )
    await builder.initialize()

    first = await builder.build(goal_run_id=goal_id, node_id=node_id)
    second = await builder.build(goal_run_id=goal_id, node_id=node_id)

    assert first.cards == second.cards
    assert first.approx_token_count <= 96
    assert len([card for card in first.cards if card.kind != "agent_card"]) <= 3
    assert [card.kind for card in first.cards[:3]] == ["goal", "root_task", "node"]


@pytest.mark.asyncio
async def test_context_enforces_independent_source_and_per_node_result_budgets(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, upstream_id, node_id = await _seed_goal(db_path)
    await _seed_budget_sources(db_path, goal_id=goal_id, node_id=node_id)
    builder = ContextBuilder(
        db_path,
        max_tokens=4_096,
        max_memory_items=2,
        max_episode_items=1,
        max_agent_cards=2,
        max_upstream_results=2,
        max_result_chars_per_node=24,
    )
    await builder.initialize()

    first = await builder.build(goal_run_id=goal_id, node_id=node_id)
    second = await builder.build(goal_run_id=goal_id, node_id=node_id)

    assert first.cards == second.cards
    assert first.provenance_ids == second.provenance_ids
    assert first.card_ids == second.card_ids
    assert first.approx_token_count == second.approx_token_count <= 4_096
    independently_counted_tokens = max(
        1,
        math.ceil(
            len(
                json.dumps(
                    first.model_payload(),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            )
            / 4
        ),
    )
    assert first.approx_token_count == independently_counted_tokens
    assert sum(card.kind == "memory" for card in first.cards) == 2
    assert sum(card.kind == "episode" for card in first.cards) == 1
    assert sum(card.kind == "agent_card" for card in first.cards) == 2
    assert sum(card.kind == "upstream" for card in first.cards) == 2
    assert upstream_id in first.provenance_ids
    assert "node_upstream_2" in first.provenance_ids
    assert "node_upstream_3" not in first.provenance_ids
    assert "mem_context" not in first.provenance_ids
    assert "episode_context_2" not in first.provenance_ids
    assert "agent_context_3" not in first.provenance_ids
    encoded = json.dumps(first.as_dict(), sort_keys=True)
    assert "tail-2" not in encoded
    assert "tail-3" not in encoded
    for card in first.cards:
        if card.kind == "upstream":
            assert len(card.summary.partition("result=")[2]) <= 24


@pytest.mark.asyncio
async def test_context_accepts_zero_optional_source_budgets(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, upstream_id, node_id = await _seed_goal(db_path)
    await _seed_budget_sources(db_path, goal_id=goal_id, node_id=node_id)
    builder = ContextBuilder(
        db_path,
        max_tokens=2_048,
        max_memory_items=0,
        max_episode_items=0,
        max_agent_cards=0,
        max_upstream_results=0,
        max_result_chars_per_node=0,
    )
    await builder.initialize()

    context = await builder.build(goal_run_id=goal_id, node_id=node_id)

    assert not {"memory", "episode", "agent_card", "upstream"}.intersection(
        card.kind for card in context.cards
    )
    assert upstream_id not in context.provenance_ids
    failure = next(card for card in context.cards if card.card_id == "failure:node_failed")
    assert "omitted by context budget" in failure.summary


@pytest.mark.asyncio
async def test_additional_material_is_redacted_budgeted_and_owns_its_provenance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, _ = await _seed_goal(db_path)
    builder = ContextBuilder(db_path, max_tokens=2_048)
    await builder.initialize()

    context = await builder.build_for_goal(
        goal_id,
        additional_cards=(
            ContextCard(
                card_id="strategy:success:0",
                kind="strategy_hint",
                summary="Use token=strategy-secret from /home/alice/project safely.",
                provenance_ids=("episode_strategy",),
            ),
        ),
    )

    payload = context.model_payload()
    encoded = json.dumps(payload, sort_keys=True)
    assert set(payload) == {"schema_version", "purpose", "cards"}
    assert "strategy-secret" not in encoded
    assert "/home/alice" not in encoded
    assert "episode_strategy" not in encoded
    assert "episode_strategy" in context.provenance_ids
    expected_tokens = max(
        1,
        math.ceil(
            len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            )
            / 4
        ),
    )
    assert context.approx_token_count == expected_tokens
    record = await builder.get_record(context.id)
    assert record is not None
    assert record.payload == payload
    assert record.approx_token_count == expected_tokens


@pytest.mark.asyncio
async def test_evaluator_payload_is_exactly_recorded_redacted_and_token_bounded(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    goal_id, _, _, node_id = await _seed_goal(db_path)
    builder = ContextBuilder(db_path, max_tokens=512)
    await builder.initialize()
    raw = GoalEvaluationContext.model_validate(
        {
            "schema_version": "1.0",
            "goal_run_id": goal_id,
            "objective": "Inspect password=evaluator-secret at /root/private " + "x" * 2_000,
            "completion_criteria": ["Return verified evidence from /etc/private"],
            "node_results": [
                {
                    "node_id": node_id,
                    "title": "Inspect token=node-secret",
                    "status": "completed",
                    "expected_output": "Evidence " + "y" * 2_000,
                    "result_summary": "Found /var/private with api_key=result-secret "
                    + "z" * 2_000,
                    "failure_reason": None,
                }
            ],
            "known_node_ids": [node_id],
            "remaining_step_budget": 4,
            "remaining_model_call_budget": 3,
            "elapsed_seconds": 10,
            "state_fingerprint": "0" * 64,
        }
    )

    bounded, record = await builder.build_evaluation_context(
        raw,
        provenance_ids=(goal_id, node_id),
    )

    payload = bounded.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True)
    assert record.payload == payload
    assert record.model_payload() == payload
    assert record.provenance_ids == (goal_id, node_id)
    assert record.approx_token_count <= 512
    assert record.approx_token_count == max(
        1,
        math.ceil(
            len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            )
            / 4
        ),
    )
    for forbidden in (
        "evaluator-secret",
        "node-secret",
        "result-secret",
        "/root/private",
        "/etc/private",
        "/var/private",
    ):
        assert forbidden not in encoded
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT context_json,approx_token_count FROM goal_contexts WHERE id=?",
                (record.id,),
            )
        ).fetchone()
    assert row is not None
    assert json.loads(str(row[0])) == payload
    assert int(row[1]) == record.approx_token_count
