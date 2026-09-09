from __future__ import annotations

import pytest

from app.services.goal_limits import GoalLoopDetected, GoalLoopGuard


def test_plan_fingerprint_ignores_rationale_but_not_executable_structure() -> None:
    first = {
        "objective": "Inspect the repository",
        "rationale_summary": "First wording",
        "completion_criteria": ["Evidence summarized"],
        "max_parallelism": 2,
        "nodes": [
            {
                "temporary_id": "inspect",
                "node_type": "worker",
                "title": "Inspect files",
                "objective": "List repository files",
                "required_skill": "workspace.list_dir",
                "dependencies": [],
                "expected_output": "A bounded list",
                "priority": 1,
                "preferred_agent_constraints": None,
            }
        ],
    }
    reworded = {**first, "rationale_summary": "Different prose"}
    changed = {
        **first,
        "nodes": [{**first["nodes"][0], "required_skill": "workspace.read_text"}],
    }

    assert GoalLoopGuard.plan_fingerprint(first) == GoalLoopGuard.plan_fingerprint(reworded)
    assert GoalLoopGuard.plan_fingerprint(first) != GoalLoopGuard.plan_fingerprint(changed)


def test_repeated_equivalent_plan_is_stopped() -> None:
    guard = GoalLoopGuard(previous_plan_fingerprint="same-plan")

    with pytest.raises(GoalLoopDetected, match="equivalent plan"):
        guard.require_new_plan("same-plan")


def test_repeated_evaluation_without_state_change_is_stopped() -> None:
    guard = GoalLoopGuard(
        previous_evaluation_fingerprint="same-evaluation",
        previous_state_fingerprint="same-state",
    )

    with pytest.raises(GoalLoopDetected, match="without state change"):
        guard.require_progress(
            evaluation_fingerprint="same-evaluation",
            state_fingerprint="same-state",
        )


def test_changed_state_allows_same_evaluator_decision() -> None:
    guard = GoalLoopGuard(
        previous_evaluation_fingerprint="continue",
        previous_state_fingerprint="wave-one",
    )

    guard.require_progress(
        evaluation_fingerprint="continue",
        state_fingerprint="wave-two",
    )
