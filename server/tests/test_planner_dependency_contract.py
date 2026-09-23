from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import pytest
from jsonschema import Draft202012Validator

from app.services.goal_manager import GoalManagerConflict
from app.services.plan_validation import PlanValidationError, parse_swarm_plan_json
from app.services.planner_provider import SwarmPlannerProviderError, UbuntuSwarmPlannerProvider
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest
from tests.test_goal_runtime_recovery import _manager, _worker_plan
from tests.test_plan_validation import valid_plan
from tests.test_planner_provider import _wire_proposal


def response_for(proposal):
    return httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:8711/v1/chat/completions"),
        json={"choices": [{"message": {"content": json.dumps(_wire_proposal(proposal))}}]},
    )


@pytest.mark.parametrize("dependency_field", ["dependencies", "optional_dependencies"])
def test_goal_identifier_is_not_a_node_dependency_and_is_never_rewritten(dependency_field):
    plan = valid_plan()
    plan["nodes"][-1]["dependencies"] = []
    plan["nodes"][-1][dependency_field] = ["goal_0123456789abcdef0123456789abcdef"]
    before = deepcopy(plan)
    with pytest.raises(PlanValidationError, match="unknown dependency goal_"):
        parse_swarm_plan_json(json.dumps(plan))
    assert plan == before


@pytest.mark.parametrize("defect", ["cycle", "self", "duplicate", "repeat"])
def test_provider_parser_rejects_invalid_graphs_before_any_authoritative_transition(defect):
    plan = valid_plan()
    if defect == "cycle":
        plan["nodes"][0]["dependencies"] = ["synthesis"]
    elif defect == "self":
        plan["nodes"][0]["dependencies"] = ["inventory"]
    elif defect == "duplicate":
        plan["nodes"][1]["temporary_id"] = "inventory"
    else:
        plan["nodes"][2]["dependencies"] = ["inventory", "inventory"]
    with pytest.raises(PlanValidationError):
        parse_swarm_plan_json(json.dumps(plan))


def test_wire_grammar_prioritizes_real_workers_before_dependent_synthesis():
    schema = UbuntuSwarmPlannerProvider._response_format(
        available_skills=["research.query", "writing.draft", "workspace.read_text"]
    )["json_schema"]["schema"]
    Draft202012Validator.check_schema(schema)
    branches = schema["properties"]["nodes"]["items"]["anyOf"]
    assert all(branch["properties"]["node_type"]["const"] == "worker" for branch in branches[:-1])
    assert branches[-1]["properties"]["node_type"]["const"] == "synthesis"
    assert branches[-1]["properties"]["dependencies"]["minItems"] == 1
    assert "temporary_id" in branches[-1]["properties"]["dependencies"]["description"]


def test_graph_parser_keeps_valid_unordered_dags_unchanged():
    plan = valid_plan()
    plan["nodes"].reverse()
    parsed = parse_swarm_plan_json(json.dumps(plan))
    assert [node.temporary_id for node in parsed.nodes] == [
        node["temporary_id"] for node in plan["nodes"]
    ]
    assert parsed.nodes[0].dependencies == ["readme", "inventory"]


async def test_invalid_goal_dependency_keeps_failure_category_cooldown_and_one_charged_call(
    tmp_path: Path,
):
    manager = await _manager(tmp_path / "goal-dependency.db", _worker_plan())
    goal = await manager.create_goal(
        GoalCreateRequest(objective="Combine the verified documentation summaries"),
        actor_id="test-phone",
    )
    # No workers are invented: reproduce the screenshot's orphan synthesis against a goal ID.
    plan = _worker_plan(objective=f"goal:{goal['id']}").model_dump(mode="json")
    plan["nodes"][0].update(
        {"node_type": "synthesis", "required_skill": None, "dependencies": [goal["id"]]}
    )
    provider = UbuntuSwarmPlannerProvider(
        base_url="http://127.0.0.1:8711/v1", model="test-no-inference"
    )
    manager.planner = provider
    post = AsyncMock(return_value=response_for(plan))
    with patch("httpx.AsyncClient.post", post):
        with pytest.raises(GoalManagerConflict):
            await manager.start_goal(str(goal["id"]), GoalStartRequest())
        assert await manager.reconcile() == 0
    assert post.await_count == 1
    current = await manager.graph.get_goal(str(goal["id"]))
    assert current["current_phase"] == "planner_invalid_response"
    assert current["failure_reason"] == (
        "The planner response did not pass server validation. "
        "Le plan contient une dépendance inconnue."
    )
    assert current["model_call_count"] == 1
    assert await manager.graph.list_nodes(str(goal["id"])) == []
    async with aiosqlite.connect(manager.db_path) as db:
        assert await (
            await db.execute("SELECT status,error_category FROM goal_model_calls")
        ).fetchall() == [("failed", "invalid_response")]


async def test_provider_preserves_graph_rejection_cause_without_exposing_model_text():
    plan = valid_plan()
    plan["objective"] = "goal:goal_current"
    plan["nodes"][-1]["dependencies"] = ["goal_current"]
    context = {
        "cards": [
            {
                "kind": "goal",
                "card_id": "goal:goal_current",
                "summary": "Summarize repository evidence",
            },
            {"kind": "agent_card", "skills": ["workspace.list_dir", "workspace.read_text"]},
        ]
    }
    provider = UbuntuSwarmPlannerProvider(
        base_url="http://127.0.0.1:8711/v1", model="test-no-inference"
    )
    with (
        patch("httpx.AsyncClient.post", AsyncMock(return_value=response_for(plan))),
        pytest.raises(SwarmPlannerProviderError) as caught,
    ):
        await provider.propose(context)
    assert caught.value.category == "invalid_response"
    assert isinstance(caught.value.__cause__, PlanValidationError)
    assert "unknown dependency" in str(caught.value.__cause__)
    assert "goal_current" not in str(caught.value)
