from pathlib import Path

import pytest

from app.services.project_context import ProjectContextConflict, ProjectContextService
from tests.test_goal_project_runtime import _project
from tests.test_project_memory import _messages


@pytest.mark.asyncio
async def test_context_preserves_old_requirements_and_restart(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    ids = await _messages(
        manager,
        goal_id,
        ["Never send email automatically."] + [f"Discussion {i}" for i in range(45)],
    )
    service = ProjectContextService(manager.db_path)
    first = await service.refresh(goal_id)
    assert any(item["source_id"] == ids[0] for item in first["requirements"])
    assert first["verified_results"] == []
    assert await ProjectContextService(manager.db_path).refresh(goal_id) == first
    source = await service.source(goal_id, ids[0])
    assert source["content"] == "Never send email automatically."


@pytest.mark.asyncio
async def test_context_rejects_stale_revision_and_cross_project_source(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    service = ProjectContextService(manager.db_path)
    before = await service.refresh(goal_id)
    await _messages(manager, goal_id, ["Changed requirement"])
    with pytest.raises(ProjectContextConflict, match="changed"):
        await service.refresh(goal_id, expected_fingerprint=before["fingerprint"])
    with pytest.raises(ProjectContextConflict, match="source not found"):
        await service.source(goal_id, "outside-project")


@pytest.mark.asyncio
async def test_duplicate_requests_share_sources_without_erasing_originals(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    ids = await _messages(manager, goal_id, ["Use SQLite", "Use SQLite"])
    service = ProjectContextService(manager.db_path)
    state = await service.refresh(goal_id)
    matches = [item for item in state["requirements"] if item["text"] == "Use SQLite"]
    assert len(matches) == 1 and matches[0]["source_ids"] == ids
    assert (await service.source(goal_id, ids[0]))["content"] == "Use SQLite"


@pytest.mark.asyncio
async def test_long_source_requirement_is_not_silently_truncated(tmp_path: Path) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    text = "Retain this detail. " * 250 + "Never erase the final constraint."
    ids = await _messages(manager, goal_id, [text])
    service = ProjectContextService(manager.db_path)
    state = await service.refresh(goal_id)
    item = next(item for item in state["requirements"] if item["source_id"] == ids[0])
    assert item["text"] == text
    assert (await service.source(goal_id, ids[0]))["content"] == text
