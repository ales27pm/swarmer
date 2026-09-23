from __future__ import annotations

import hashlib

import pytest

from app.services.project_contracts import ProjectPayload, ProjectResult, project_digest
from app.services.project_guidance import validate_guidance_reads
from tests.test_project_progress import payload, result


def snapshots(path="src/new.py", reads=None):
    files = [
        {"path": "AGENTS.md", "content": "Preserve user data."},
        {"path": "src/AGENTS.md", "content": "Use transactions."},
        {"path": "web/AGENTS.md", "content": "Use HTML."},
    ]
    before = payload(files=files, base_sha256=project_digest(files))
    after = result(
        files=[*files, {"path": path, "content": "value = 1\n"}],
        base_sha256=project_digest(files),
        guidance_reads=reads or [],
    )
    return before, after


def receipts():
    return [
        {"path": p, "sha256": hashlib.sha256(s.encode()).hexdigest()}
        for p, s in [("AGENTS.md", "Preserve user data."), ("src/AGENTS.md", "Use transactions.")]
    ]


def test_missing_receipt_rejects_new_file():
    with pytest.raises(ValueError, match="must be read"):
        validate_guidance_reads(*snapshots())


def test_valid_ancestor_receipts_accept_new_file_without_sibling():
    validate_guidance_reads(*snapshots(reads=receipts()))


def test_guidance_read_is_tied_to_exact_old_content():
    bad = receipts()
    bad[1]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source revision"):
        validate_guidance_reads(*snapshots(reads=bad))


def test_new_folder_under_different_guidance_is_rejected():
    with pytest.raises(ValueError, match="must be read"):
        validate_guidance_reads(*snapshots(path="web/new.py", reads=receipts()))


def test_read_only_iteration_needs_no_receipt():
    before, after = snapshots()
    after = after.model_copy(update={"files": before.files})
    validate_guidance_reads(before, after)


def test_updating_guidance_uses_old_revision_and_retains_new_contents():
    before, after = snapshots(reads=receipts())
    after = after.model_copy(
        update={
            "files": [
                f.model_copy(update={"content": "Use safe transactions."})
                if f.path == "src/AGENTS.md"
                else f
                for f in before.files
            ]
        }
    )
    validate_guidance_reads(before, after)


def test_legacy_result_without_guidance_stays_decodable():
    assert ProjectResult.model_validate_json(result().model_dump_json()).guidance_reads == []
    assert isinstance(payload(), ProjectPayload)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True])
async def test_capture_enforces_guidance_without_rewriting_previous_snapshot(tmp_path, valid):
    import aiosqlite

    from app.services.goal_project import GoalProjectConflict
    from tests.test_goal_project_runtime import _project

    manager, detail, agent = await _project(tmp_path)
    goal = detail["goal"]["id"]
    service = manager.project_applications
    assert service is not None

    async def submit(files, reads):
        job = await manager.agent_dispatcher.claim(agent)
        assert job is not None
        body = {
            "schema_version": "1.0",
            "action": "continue",
            "message": "Changed the requested file.",
            "plan": ["Implement contacts", "Test persistence"],
            "files": files,
            "checks": [],
            "run_instructions": "python app.py",
            "runtime": "python",
            "base_revision_id": job["payload"]["base_revision_id"],
            "base_sha256": job["payload"]["base_sha256"],
            "guidance_reads": reads,
        }
        accepted, _ = await manager.agent_dispatcher.submit_result(
            agent,
            job["id"],
            job["claim_token"],
            status="completed",
            error=None,
            lease_id=job["lease_id"],
            lease_generation=job["lease_generation"],
            result=body,
        )
        return accepted

    originals = [
        {"path": "AGENTS.md", "content": "Preserve user data."},
        {"path": "app.py", "content": "value = 1\n"},
    ]
    first = await submit(originals, [])
    await manager.on_job_result(first)
    preview = await service.get_project(goal)
    assert preview is not None
    next_job = await submit(
        [originals[0], {"path": "app.py", "content": "value = 2\n"}],
        receipts()[:1] if valid else [],
    )
    current = await manager.get_goal(goal)
    node = next(n for n in current["nodes"] if n["worker_job_id"] == next_job["id"])
    if valid:
        await service.capture_result(goal, node["id"], next_job["id"])
        updated = await service.get_project(goal)
        assert updated["guidance_reads"] == receipts()[:1]
        assert updated["revision"] == preview["revision"] + 1
    else:
        with pytest.raises(GoalProjectConflict, match="AGENTS.md must be read"):
            await service.capture_result(goal, node["id"], next_job["id"])
        assert await service.get_project(goal) == preview
    async with aiosqlite.connect(manager.db_path) as db:
        stored = await (
            await db.execute(
                "SELECT sha256 FROM project_revisions WHERE id=?", (preview["revision_id"],)
            )
        ).fetchone()
        assert stored[0] == preview["sha256"]
