"""M09 product behavior uses real accepted SQL receipts, never provider verdicts."""

import hashlib
import json

import aiosqlite
import pytest

from app.services.memory_lessons import MemoryLessonService
from app.services.memory_lessons_contracts import (
    LessonAssessment,
    LessonProposal,
    LessonWithdrawal,
    ProfileApproval,
)
from tests.test_project_execution_experiences import finished_measurement


async def assess_fixture(tmp_path, *, change_approval=None):
    values = await lesson_fixture(tmp_path)
    service, _, project, approval, proposal, accepted = values
    if change_approval:
        change_approval(approval)
    await service.approve_profile(
        "python-fixture", ProfileApproval.model_validate(approval), "operator"
    )
    candidate = await service.propose(project, LessonProposal.model_validate(proposal), "device")
    result = await service.assess(
        project,
        candidate.lesson_id,
        LessonAssessment(
            request_id="assess-1", expected_version=1, acceptance_ids=[accepted["id"]]
        ),
        "device",
    )
    return values, result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("qualification_kind", "synthetic_only", "profile_synthetic_only"),
        ("runtime_image_id", "sha256:" + "b" * 64, "producer_profile_mismatch"),
        ("runner_sha256", "b" * 64, "producer_profile_mismatch"),
        ("policy_sha256", "b" * 64, "producer_profile_mismatch"),
    ],
)
async def test_profile_declarations_cannot_override_measurements(tmp_path, field, value, reason):
    _, result = await assess_fixture(tmp_path, change_approval=lambda a: a.update({field: value}))
    assert result.applicability == "needs_revalidation"
    assert reason in result.reasons
    assert result.promotion == "none" and not result.grants_authority


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,reason",
    [
        ("minimum", "insufficient_tests"),
        ("oracle", "criterion_oracle_mismatch"),
        ("dependency", "execution_conditions_mismatch"),
        ("harness", "execution_conditions_mismatch"),
    ],
)
async def test_criterion_oracle_and_actual_environment_must_match(tmp_path, mode, reason):
    def change(approval):
        if mode == "minimum":
            approval["criteria"][0]["minimum_tests"] = 2
        elif mode == "oracle":
            approval["criteria"][0]["oracle_files"][0]["sha256"] = "b" * 64
        else:
            approval["profiles"][0][mode + "_sha256"] = "b" * 64

    _, result = await assess_fixture(tmp_path, change_approval=change)
    assert result.observation == "unknown" and result.applicability == "needs_revalidation"
    assert reason in result.reasons


@pytest.mark.asyncio
async def test_unknown_profile_is_retained_as_candidate_needing_revalidation(tmp_path):
    service, _, project, _, proposal, accepted = await lesson_fixture(tmp_path)
    candidate = await service.propose(project, LessonProposal.model_validate(proposal), "device")
    result = await service.assess(
        project,
        candidate.lesson_id,
        LessonAssessment(
            request_id="assess-1", expected_version=1, acceptance_ids=[accepted["id"]]
        ),
        "device",
    )
    assert result.observation == "unknown" and "profile_unknown" in result.reasons
    assert result.applicability == "needs_revalidation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime,profiles",
    [
        ("node", ["node_build", "node_test"]),
        ("python_node", ["python_build", "python_test", "node_build", "node_test"]),
    ],
)
async def test_valid_receipt_from_different_runtime_is_unknown_not_storage_error(
    tmp_path, runtime, profiles
):
    service, _, project, approval, proposal, accepted = await lesson_fixture(tmp_path)
    approval.update(
        runtime=runtime,
        profiles=[
            {"profile": name, "harness_sha256": "d" * 64, "dependency_sha256": "c" * 64}
            for name in profiles
        ],
    )
    approval["criteria"][0]["test_profiles"] = [profiles[-1]]
    proposal.update(runtime=runtime, profiles=profiles)
    await service.approve_profile(
        "python-fixture", ProfileApproval.model_validate(approval), "operator"
    )
    candidate = await service.propose(project, LessonProposal.model_validate(proposal), "device")
    observed = await service.assess(
        project,
        candidate.lesson_id,
        LessonAssessment(
            request_id="different-runtime", expected_version=1, acceptance_ids=[accepted["id"]]
        ),
        "device",
    )
    assert observed.observation == "unknown" and observed.applicability == "needs_revalidation"
    assert "producer_profile_mismatch" in observed.reasons


@pytest.mark.asyncio
async def test_failure_outside_criterion_profiles_is_not_a_comparable_counterexample(tmp_path):
    import copy

    from app.services.project_execution_receipts import PROFILE_COMMANDS

    def mixed_report(result):
        receipt = result["execution_receipt"]
        result["runtime"] = receipt["runtime"] = "python_node"
        for name in ("node_build", "node_test"):
            test = name.endswith("_test")
            item = copy.deepcopy(receipt["profiles"][int(test)])
            item.update(
                profile=name,
                check_index=len(receipt["profiles"]),
                exit_code=int(test),
                test_failures=int(test),
            )
            receipt["profiles_expected"].append(name)
            receipt["profiles"].append(item)
            result["checks"].append(
                {
                    "command": PROFILE_COMMANDS[name],
                    "status": "failed" if test else "passed",
                    "exit_code": int(test),
                    "duration_ms": 3,
                    "output": "Synthetic unrelated Node test outcome",
                }
            )

    service, _, project, approval, proposal, accepted = await lesson_fixture(
        tmp_path, mutate_result=mixed_report
    )
    approval["runtime"] = proposal["runtime"] = "python_node"
    assert approval["criteria"][0]["test_profiles"] == ["python_test"]
    await service.approve_profile(
        "python-fixture", ProfileApproval.model_validate(approval), "operator"
    )
    candidate = await service.propose(project, LessonProposal.model_validate(proposal), "device")
    result = await service.assess(
        project,
        candidate.lesson_id,
        LessonAssessment(
            request_id="mixed-assessment", expected_version=1, acceptance_ids=[accepted["id"]]
        ),
        "device",
    )
    assert result.lifecycle == "assessed"
    assert result.observation == "incomplete" and result.applicability == "needs_revalidation"
    assert "non_criterion_failure" in result.reasons and "comparable_failure" not in result.reasons


@pytest.mark.asyncio
async def test_changed_criterion_and_new_source_invalidate_without_read_mutation(tmp_path):
    from tests.test_memory_symbolic_integration import database_snapshot

    (service, manager, project, _, _, _), result = await assess_fixture(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE goal_runs SET completion_criteria_json='[\"Changed requirement\"]'"
        )
        await db.commit()
    before = await database_snapshot(manager.db_path)
    current = await service.get(project, result.lesson_id)
    assert current.applicability == "needs_revalidation" and "criterion_changed" in current.reasons
    assert await database_snapshot(manager.db_path) == before


@pytest.mark.asyncio
async def test_atomic_audit_failure_does_not_leave_profile_or_idempotency_record(
    tmp_path, monkeypatch
):
    from app.services import memory_lessons
    from tests.test_memory_symbolic_integration import database_snapshot

    service, manager, _, approval, _, _ = await lesson_fixture(tmp_path)
    before = await database_snapshot(manager.db_path)

    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(memory_lessons, "append_audit_event", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await service.approve_profile(
            "python-fixture", ProfileApproval.model_validate(approval), "operator"
        )
    assert await database_snapshot(manager.db_path) == before


@pytest.mark.asyncio
async def test_cross_project_or_mutated_request_id_cannot_select_other_evidence(tmp_path):
    from app.services.memory_lessons import LessonError

    service, _, project, approval, proposal, _ = await lesson_fixture(tmp_path)
    await service.approve_profile(
        "python-fixture", ProfileApproval.model_validate(approval), "operator"
    )
    candidate = await service.propose(project, LessonProposal.model_validate(proposal), "device")
    with pytest.raises(LessonError, match="project_not_found"):
        await service.get("project_other", candidate.lesson_id)
    proposal["profile_version"] = 2
    with pytest.raises(LessonError, match="lesson_request_reused"):
        await service.propose(project, LessonProposal.model_validate(proposal), "device")


@pytest.mark.asyncio
async def test_page_has_explicit_bounded_cursor_and_withdrawal_retains_history(tmp_path):
    service, _manager, project, _, proposal, _ = await lesson_fixture(tmp_path)
    ids = []
    for index in range(3):
        proposal["request_id"] = f"propose-{index}"
        ids.append(
            (
                await service.propose(project, LessonProposal.model_validate(proposal), "device")
            ).lesson_id
        )
    page = await service.list(project, limit=2)
    assert [v.lesson_id for v in page.items] == sorted(ids)[:2]
    assert page.has_more and page.next_after_id == sorted(ids)[1]
    last = await service.list(project, after_id=page.next_after_id, limit=2)
    assert [v.lesson_id for v in last.items] == sorted(ids)[2:]
    assert not last.has_more and last.next_after_id is None
    assert page.consistency == "page_snapshot"


async def lesson_fixture(tmp_path, *, failed=False, incomplete=False, mutate_result=None):
    if mutate_result:
        from app.services.project_context import ProjectContextService
        from tests.test_project_execution_persistence import receipt_job, submit

        manager, detail, agent, claim, result = await receipt_job(tmp_path)
        mutate_result(result)
        job, _ = await submit(manager, agent, claim, result)
        await manager.on_job_result(job)
        state = await ProjectContextService(manager.db_path).refresh(detail["goal"]["id"])
    else:
        manager, detail, job, result, _, state = await finished_measurement(
            tmp_path, failed=failed, incomplete=incomplete
        )
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        goal = await (
            await db.execute("SELECT * FROM goal_runs WHERE id=?", (detail["goal"]["id"],))
        ).fetchone()
        accepted = await (
            await db.execute(
                "SELECT * FROM project_execution_acceptances WHERE job_id=?", (job["id"],)
            )
        ).fetchone()
        revision = await (
            await db.execute("SELECT * FROM project_revisions WHERE worker_job_id=?", (job["id"],))
        ).fetchone()
    criterion = json.loads(goal["completion_criteria_json"])[0]
    criterion_sha = hashlib.sha256(criterion.encode()).hexdigest()
    receipt = result["execution_receipt"]
    approval = {
        "request_id": "approve-1",
        "expected_version": 0,
        "runtime": "python",
        "runtime_image_id": receipt["runtime_image_id"],
        "runner_sha256": receipt["runner_sha256"],
        "policy_sha256": receipt["policy_sha256"],
        "profiles": [
            {"profile": name, "harness_sha256": "d" * 64, "dependency_sha256": "c" * 64}
            for name in receipt["profiles_expected"]
        ],
        "producer_agent_ids": [accepted["producer_agent_id"]],
        "qualification_kind": "isolated_runtime",
        "qualification_receipt_sha256": "a" * 64,
        "criteria": [
            {
                "criterion_sha256": criterion_sha,
                "oracle_files": [
                    {"path": "app.py", "sha256": hashlib.sha256(b"value=1\n").hexdigest()}
                ],
                "test_profiles": ["python_test"],
                "minimum_tests": 1,
            }
        ],
    }
    proposal = {
        "request_id": "propose-1",
        "expected_version": 0,
        "goal_id": goal["id"],
        "conversation_revision": goal["conversation_revision"],
        "criterion_index": 0,
        "criterion_sha256": criterion_sha,
        "revision_id": revision["id"],
        "source_sha256": revision["sha256"],
        "profile_id": "python-fixture",
        "profile_version": 1,
        "runtime": "python",
        "profiles": receipt["profiles_expected"],
        "note_ref": None,
    }
    return (
        MemoryLessonService(manager.db_path),
        manager,
        state["project_id"],
        approval,
        proposal,
        accepted,
    )


@pytest.mark.asyncio
async def test_candidate_assessment_withdrawal_never_creates_policy_or_attestation(tmp_path):
    service, manager, project, approval, proposal, accepted = await lesson_fixture(tmp_path)
    await service.approve_profile(
        "python-fixture", ProfileApproval.model_validate(approval), "operator"
    )
    candidate = await service.propose(project, LessonProposal.model_validate(proposal), "device")
    assert candidate.lifecycle == "candidate"
    assessment = LessonAssessment(
        request_id="assess-1", expected_version=1, acceptance_ids=[accepted["id"]]
    )
    assessed = await service.assess(project, candidate.lesson_id, assessment, "device")
    assert assessed.observation == "passed"
    assert assessed.applicability == "reported_conditions_match"
    assert assessed.producer_assurance == "authenticated_lease_only"
    assert assessed.evidence_scope == "explicit_selection"
    assert assessed.promotion == "none" and assessed.grants_authority is False
    replay = await service.assess(project, candidate.lesson_id, assessment, "device")
    assert replay.version == assessed.version == 2
    withdrawn = await service.withdraw(
        project,
        candidate.lesson_id,
        LessonWithdrawal(request_id="withdraw-1", expected_version=2),
        "device",
    )
    assert withdrawn.lifecycle == "withdrawn"
    replay = await service.assess(project, candidate.lesson_id, assessment, "device")
    assert replay.lifecycle == "withdrawn" and replay.version == 3
    async with aiosqlite.connect(manager.db_path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM memory_items")).fetchone())[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed,incomplete,expected", [(True, False, "quarantined"), (False, True, "assessed")]
)
async def test_measured_failure_is_not_confused_with_incomplete_measurement(
    tmp_path, failed, incomplete, expected
):
    service, _, project, approval, proposal, accepted = await lesson_fixture(
        tmp_path, failed=failed, incomplete=incomplete
    )
    await service.approve_profile(
        "python-fixture", ProfileApproval.model_validate(approval), "operator"
    )
    candidate = await service.propose(project, LessonProposal.model_validate(proposal), "device")
    result = await service.assess(
        project,
        candidate.lesson_id,
        LessonAssessment(
            request_id="assess-1", expected_version=1, acceptance_ids=[accepted["id"]]
        ),
        "device",
    )
    assert result.lifecycle == expected
    assert result.observation == ("failed" if failed else "incomplete")
    assert result.grants_authority is False and result.promotion == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,reason",
    [
        ("zero", "insufficient_tests"),
        ("truncated", "output_truncated"),
        ("infra", "non_test_failure"),
    ],
)
async def test_zero_tests_truncation_and_infrastructure_failure_never_become_applicable(
    tmp_path, mode, reason
):
    def change(result):
        if mode == "zero":
            result["execution_receipt"]["profiles"][1].update(tests_executed=0, exit_code=5)
            result["checks"][1].update(status="failed", exit_code=5)
        elif mode == "truncated":
            result["checks"][1]["output"] = "some output\n[output truncated]"
        else:
            result["execution_receipt"]["profiles"][0]["exit_code"] = 1
            result["checks"][0].update(status="failed", exit_code=1)

    service, _, project, approval, proposal, accepted = await lesson_fixture(
        tmp_path, mutate_result=change
    )
    await service.approve_profile(
        "python-fixture", ProfileApproval.model_validate(approval), "operator"
    )
    candidate = await service.propose(project, LessonProposal.model_validate(proposal), "device")
    assessed = await service.assess(
        project,
        candidate.lesson_id,
        LessonAssessment(
            request_id="assess-1", expected_version=1, acceptance_ids=[accepted["id"]]
        ),
        "device",
    )
    assert assessed.applicability == "needs_revalidation" and reason in assessed.reasons
    assert assessed.lifecycle != "quarantined" and assessed.observation != "passed"


@pytest.mark.asyncio
async def test_canonical_note_remains_untrusted_and_revokes_as_a_whole(tmp_path):
    from app.models import MemoryCreate, MemoryUpdate
    from app.services.state_service import StateService
    from tests.test_memory_canonical_store import ReviewedNormalizer

    service, manager, project, approval, proposal, accepted = await lesson_fixture(tmp_path)
    normalizer = ReviewedNormalizer()
    state = StateService(manager.db_path, canonical_language="en", memory_normalizer=normalizer)
    note = await state.create_memory(
        MemoryCreate(content="Ne pas envoyer automatiquement.", scope="project:" + project),
        "device",
    )
    proposal["note_ref"] = {"memory_id": note["id"], "revision": 1}
    await service.approve_profile(
        "python-fixture", ProfileApproval.model_validate(approval), "operator"
    )
    candidate = await service.propose(project, LessonProposal.model_validate(proposal), "device")
    assert candidate.note.original_content == "Ne pas envoyer automatiquement."
    assert candidate.note.canonical_content == "Do not send automatically."
    assert candidate.note.content_trust == "untrusted"
    calls = len(normalizer.calls)
    assessed = await service.assess(
        project,
        candidate.lesson_id,
        LessonAssessment(
            request_id="assess-1", expected_version=1, acceptance_ids=[accepted["id"]]
        ),
        "device",
    )
    assert assessed.applicability == "reported_conditions_match" and len(normalizer.calls) == calls
    await state.update_memory(
        note["id"], MemoryUpdate(content="Garder les dates exactes."), "device"
    )
    changed = await service.get(project, candidate.lesson_id)
    assert changed.note is None and "note_unavailable" in changed.reasons
    assert changed.applicability == "needs_revalidation"
