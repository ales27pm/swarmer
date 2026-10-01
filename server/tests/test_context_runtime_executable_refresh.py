from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from app.services import project_context
from app.services.context_builder import safe_context_text
from app.services.project_context import ProjectContextConflict, ProjectContextService
from tests.test_goal_project_runtime import _project
from tests.test_project_memory import _messages


@pytest.mark.asyncio
async def test_redaction_policy_refresh_keeps_history_and_invalidates_old_capsule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, detail, _ = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    original = (
        "If available, use /usr/bin/chromium and /usr/bin/chromedriver for Selenium. "
        "Do not read /home/alice/private.txt; password=hidden-value"
    )
    source_ids = await _messages(manager, goal_id, [original])
    service = ProjectContextService(manager.db_path)
    digest = project_context.digest

    def legacy_fingerprint(value: object) -> str:
        if isinstance(value, dict) and "capsule_version" in value:
            value = {**value, "capsule_version": 2}
        return digest(value)

    def legacy_redaction(value: str | None, *, max_chars: int = 4000) -> str:
        return (
            safe_context_text(value, max_chars=max_chars)
            .replace("/usr/bin/chromium", "<protected-path>")
            .replace("/usr/bin/chromedriver", "<protected-path>")
        )

    # A persisted projection made before the policy change: same original
    # messages/revision, but the two public references were still redacted.
    with monkeypatch.context() as patch:
        patch.setattr(project_context, "digest", legacy_fingerprint)
        patch.setattr(project_context, "safe_context_text", legacy_redaction)
        previous = await service.refresh(goal_id)
    previous_requirement = next(
        item for item in previous["requirements"] if item["source_id"] == source_ids[0]
    )
    assert "/usr/bin/" not in previous_requirement["text"]

    current = await service.refresh(goal_id)
    assert current["fingerprint"] != previous["fingerprint"]
    assert current["version"] == previous["version"] + 1
    current_requirement = next(
        item for item in current["requirements"] if item["source_id"] == source_ids[0]
    )
    assert "/usr/bin/chromium" in current_requirement["text"]
    assert "/usr/bin/chromedriver" in current_requirement["text"]
    assert "private.txt" not in current_requirement["text"]
    assert "hidden-value" not in current_requirement["text"]
    assert current_requirement["source_ids"] == previous_requirement["source_ids"]
    assert service.prompt_state(current)["requirements"][-1] == {
        "source_id": source_ids[0],
        "text": current_requirement["text"],
    }
    assert await ProjectContextService(manager.db_path).refresh(goal_id) == current
    with pytest.raises(ProjectContextConflict, match="changed"):
        await service.refresh(goal_id, expected_fingerprint=previous["fingerprint"])

    async with aiosqlite.connect(manager.db_path) as db:
        source = await (
            await db.execute("SELECT content FROM goal_messages WHERE id=?", (source_ids[0],))
        ).fetchone()
        snapshots = await (
            await db.execute(
                "SELECT state_json FROM project_context_snapshots WHERE project_id=? ORDER BY version",
                (current["project_id"],),
            )
        ).fetchall()
    assert source is not None and source[0] == original
    states = [json.loads(row[0]) for row in snapshots]
    assert previous in states and current in states
    assert sum(item["fingerprint"] == current["fingerprint"] for item in states) == 1
