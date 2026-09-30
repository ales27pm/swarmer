from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.services.goal_manager import GoalManager
from app.services.swarm_contracts import GoalCreateRequest, GoalMessageRequest, GoalStartRequest
from app.services.writing_drafts import (
    writing_completion_failure_locked,
    writing_completion_valid_locked,
)
from tests.test_goal_context_payloads import _CapturingEvaluator
from tests.test_goal_project_runtime import _project
from tests.test_goal_runtime_recovery import _manager
from tests.test_goal_writing import RESULT, done, plan, register
from tests.test_goal_writing_evaluation import completed_writing


async def save_files(
    manager: GoalManager,
    agent: str,
    files: list[dict[str, str]],
    *,
    research_url: str | None = None,
) -> dict[str, Any]:
    job = await manager.agent_dispatcher.claim(agent)
    assert job is not None
    if research_url is not None:
        # A persisted worker input is the citation allowlist, as for writing.draft.
        payload = {
            **job["payload"],
            "research_sources": [
                {
                    "content_trust": "untrusted",
                    "worker_job_id": "job_fixture_search",
                    "title": "Official documentation",
                    "url": research_url,
                    "snippet": "A research excerpt, not a visited page.",
                }
            ],
        }
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute(
                "UPDATE agent_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), job["id"])
            )
            await db.commit()
    completed, _ = await manager.agent_dispatcher.submit_result(
        agent,
        job["id"],
        job["claim_token"],
        status="completed",
        error=None,
        lease_id=job["lease_id"],
        lease_generation=job["lease_generation"],
        result={
            "schema_version": "1.0",
            "action": "continue",
            "message": "Documentation recorded.",
            "plan": ["Verify documentation"],
            "files": files,
            "checks": [],
            "run_instructions": "",
            "runtime": "python",
            "base_revision_id": job["payload"]["base_revision_id"],
            "base_sha256": job["payload"]["base_sha256"],
        },
    )
    await manager.on_job_result(completed)
    return job


async def request(manager: GoalManager, goal_id: str, text: str) -> None:
    await manager.conversations.append(
        goal_id,
        message=text,
        client_message_id="document-contract",
        reply_to_message_id=None,
        actor_id="phone",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,text,expected",
    [
        ("README.md", "Run python3 app.py", True),
        ("README.md", "Run app", False),
        ("docs/README.md", "Run python3 app.py", False),
    ],
)
async def test_named_document_is_checked_as_one_exact_file(
    tmp_path: Path, path: str, text: str, expected: bool
) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await save_files(manager, agent, [{"path": path, "content": text}])
    await request(manager, goal_id, "Le fichier README.md doit contenir exactement 4 mots.")
    async with aiosqlite.connect(manager.db_path) as db:
        assert await writing_completion_valid_locked(db, goal_id) is expected


@pytest.mark.asyncio
async def test_latest_revision_cannot_fall_back_to_older_conforming_file(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await save_files(manager, agent, [{"path": "README.md", "content": "Run python3 app.py"}])
    await save_files(manager, agent, [{"path": "README.md", "content": "Run app"}])
    await request(manager, goal_id, "Le fichier README.md doit contenir exactement 4 mots.")
    async with aiosqlite.connect(manager.db_path) as db:
        assert not await writing_completion_valid_locked(db, goal_id)
        assert await writing_completion_failure_locked(db, goal_id) == "writing_requirements_unmet"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["hash", "job", "status", "node_status", "task_status", "source", "snapshot"]
)
async def test_document_requires_intact_provenance(tmp_path: Path, corruption: str) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    job = await save_files(manager, agent, [{"path": "README.md", "content": "Run python3 app.py"}])
    await request(manager, goal_id, "Le fichier README.md doit contenir exactement 4 mots.")
    async with aiosqlite.connect(manager.db_path) as db:
        if corruption == "hash":
            await db.execute("UPDATE project_revisions SET sha256=?", ("0" * 64,))
        elif corruption == "job":
            await db.execute(
                "UPDATE plan_nodes SET worker_job_id=NULL WHERE worker_job_id=?", (job["id"],)
            )
        elif corruption == "status":
            await db.execute("UPDATE agent_jobs SET status='failed' WHERE id=?", (job["id"],))
        elif corruption == "node_status":
            await db.execute(
                "UPDATE plan_nodes SET status='running' WHERE worker_job_id=?", (job["id"],)
            )
        elif corruption == "task_status":
            await db.execute("UPDATE tasks SET status='failed' WHERE id=?", (job["task_id"],))
        elif corruption == "source":
            await db.execute("UPDATE tasks SET source='goal:other' WHERE id=?", (job["task_id"],))
        else:
            row = await (
                await db.execute("SELECT result_json FROM agent_jobs WHERE id=?", (job["id"],))
            ).fetchone()
            result = json.loads(row[0])
            result["files"][0]["content"] = "Different project contents"
            await db.execute(
                "UPDATE agent_jobs SET result_json=? WHERE id=?", (json.dumps(result), job["id"])
            )
        await db.commit()
        assert not await writing_completion_valid_locked(db, goal_id)
        assert (
            await writing_completion_failure_locked(db, goal_id)
            == "document_requirement_unverifiable"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "citation,domain,expected",
    [
        ("https://sqlite.org/about.html", "sqlite.org", None),
        ("https://sqlite.org/other.html", "sqlite.org", "writing_requirements_unmet"),
        ("https://sqlite.org.evil.example/about.html", "sqlite.org", "writing_requirements_unmet"),
        ("https://sqlite.org/about.html", "docs.python.org", "writing_requirements_unmet"),
        ("", "sqlite.org", "writing_requirements_unmet"),
    ],
)
async def test_named_document_obeys_exact_citation_allowlist_and_domain(
    tmp_path: Path, citation: str, domain: str, expected: str | None
) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await save_files(
        manager,
        agent,
        [{"path": "README.md", "content": f"Run python3 app.py {citation}"}],
        research_url="https://sqlite.org/about.html",
    )
    await request(
        manager,
        goal_id,
        f"Le fichier README.md doit contenir exactement 4 mots. Cite au moins 1 lien officiel de {domain}.",
    )
    async with aiosqlite.connect(manager.db_path) as db:
        assert await writing_completion_failure_locked(db, goal_id) == expected


@pytest.mark.asyncio
async def test_file_contents_are_not_concatenated_to_meet_word_count(tmp_path: Path) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await save_files(
        manager,
        agent,
        [
            {"path": "README.md", "content": "First half"},
            {"path": "guide.md", "content": "Second half"},
        ],
    )
    await request(manager, goal_id, "Write README.md in exactly 4 words.")
    async with aiosqlite.connect(manager.db_path) as db:
        assert await writing_completion_failure_locked(db, goal_id) == "writing_requirements_unmet"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "instruction",
    [
        "Write https://example.org/README.md in exactly 4 words.",
        "Write ../README.md in exactly 4 words.",
        "Write README.md and guide.md in exactly 4 words.",
        "Read README.md then write a note of exactly 4 words.",
        "Write a note of exactly 4 words.",
        "Write README.md in exactly 100001 words.",
    ],
)
async def test_unverifiable_document_is_reported_as_validation_limit(
    tmp_path: Path, instruction: str
) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await save_files(manager, agent, [{"path": "README.md", "content": "Run python3 app.py"}])
    await request(manager, goal_id, instruction)
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await writing_completion_failure_locked(db, goal_id)
            == "document_requirement_unverifiable"
        )


@pytest.mark.asyncio
async def test_conforming_prose_cannot_replace_an_explicit_document_file(tmp_path: Path) -> None:
    manager, _, goal_id, _ = await completed_writing(tmp_path, text="One two three four")
    await request(manager, goal_id, "Write README.md in exactly 4 words.")
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await writing_completion_failure_locked(db, goal_id)
            == "document_requirement_unverifiable"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "followup",
    [
        "Read docs/context.md first.",
        "The error mentions README.md. Continue.",
        "Do not modify docs/context.md.",
    ],
)
async def test_reading_or_mentioning_a_file_does_not_replace_the_output_contract(
    tmp_path: Path, followup: str
) -> None:
    manager, detail, agent = await _project(tmp_path)
    goal_id = detail["goal"]["id"]
    await save_files(manager, agent, [{"path": "README.md", "content": "Run python3 app.py"}])
    await request(manager, goal_id, "Write README.md in exactly 4 words.")
    await manager.conversations.append(
        goal_id,
        message=followup,
        client_message_id="reference",
        reply_to_message_id=None,
        actor_id="phone",
    )
    async with aiosqlite.connect(manager.db_path) as db:
        assert await writing_completion_failure_locked(db, goal_id) is None


async def writing_revisions(
    tmp_path: Path, *, followup: str = "Rédige une nouvelle note conforme de 150 à 200 mots."
) -> tuple[GoalManager, str, dict[str, Any], dict[str, Any]]:
    objective = "Rédige une note de 150 à 200 mots pour comparer SQLite et JSON."
    manager = await _manager(
        tmp_path / "state.db",
        plan().model_copy(update={"objective": objective}),
        evaluator=_CapturingEvaluator(),
    )
    agent = await register(manager)
    goal = await manager.create_goal(
        GoalCreateRequest(objective=objective, max_model_calls=20), actor_id="phone"
    )
    await manager.start_goal(goal["id"], GoalStartRequest())

    async def deliver() -> dict[str, Any]:
        job = await manager.agent_dispatcher.claim(agent)
        assert job is not None
        record, _ = await manager.agent_dispatcher.submit_result(
            agent,
            job["id"],
            job["claim_token"],
            status="completed",
            result={**RESULT, "text": "mot " * 160},
            error=None,
            lease_id=job["lease_id"],
            lease_generation=job["lease_generation"],
        )
        await manager.on_job_result(record)
        return job

    old = await deliver()
    # Model an immutable receipt accepted by an older backend, without weakening
    # the current submission validator to admit this historical defect.
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE agent_jobs SET result_json=? WHERE id=?",
            (json.dumps({**RESULT, "text": "mot " * 55}), old["id"]),
        )
        await db.commit()
    await manager.reply_goal(
        goal["id"],
        GoalMessageRequest(message=followup, client_message_id="repair"),
        actor_id="phone",
    )
    await manager.reconcile()
    new = await deliver()
    return manager, goal["id"], old, new


@pytest.mark.asyncio
async def test_corrected_current_note_supersedes_legacy_length_failure(tmp_path: Path) -> None:
    manager, goal_id, old, new = await writing_revisions(tmp_path)
    assert old["id"] != new["id"]
    async with aiosqlite.connect(manager.db_path) as db:
        assert await writing_completion_failure_locked(db, goal_id) is None
        receipt = await (
            await db.execute("SELECT result_json FROM agent_jobs WHERE id=?", (old["id"],))
        ).fetchone()
        assert json.loads(receipt[0])["text"] == "mot " * 55
    manager.evaluator = done()
    await manager._evaluate_if_quiescent(goal_id, explicit_user_action=True)
    assert (await manager.graph.get_goal(goal_id))["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["current_short", "old_shape", "old_source", "same_revision"]
)
async def test_supersession_keeps_current_and_provenance_checks(
    tmp_path: Path, corruption: str
) -> None:
    manager, goal_id, old, new = await writing_revisions(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        expected = "writing_evidence_invalid"
        if corruption == "current_short":
            for job, words in [(old, 160), (new, 55)]:
                await db.execute(
                    "UPDATE agent_jobs SET result_json=? WHERE id=?",
                    (json.dumps({**RESULT, "text": "mot " * words}), job["id"]),
                )
            expected = "writing_requirements_unmet"
        elif corruption == "old_shape":
            await db.execute(
                "UPDATE agent_jobs SET result_json=? WHERE id=?",
                (json.dumps({**RESULT, "content_trust": "trusted"}), old["id"]),
            )
        elif corruption == "old_source":
            await db.execute("UPDATE tasks SET source='goal:other' WHERE id=?", (old["task_id"],))
        else:
            await db.execute(
                "UPDATE plan_nodes SET conversation_revision=(SELECT conversation_revision FROM goal_runs WHERE id=?) WHERE worker_job_id=?",
                (goal_id, old["id"]),
            )
            expected = "writing_requirements_unmet"
        await db.commit()
        assert await writing_completion_failure_locked(db, goal_id) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "followup",
    [
        "Write README.md and CHANGELOG.md. Write 150 to 200 words.",
        "Write /tmp/README.md with 150 to 200 words.",
        "Write https://example.org/README.md with 150 to 200 words.",
    ],
)
async def test_ambiguous_or_invalid_file_request_cannot_be_satisfied_by_current_prose(
    tmp_path: Path, followup: str
) -> None:
    manager, goal_id, _, _ = await writing_revisions(tmp_path, followup=followup)
    async with aiosqlite.connect(manager.db_path) as db:
        assert (
            await writing_completion_failure_locked(db, goal_id)
            == "document_requirement_unverifiable"
        )


@pytest.mark.asyncio
async def test_multiple_current_documents_are_not_arbitrarily_combined_or_superseded(
    tmp_path: Path,
) -> None:
    manager, goal_id, old, _ = await writing_revisions(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE plan_nodes SET conversation_revision=(SELECT conversation_revision FROM goal_runs WHERE id=?) WHERE worker_job_id=?",
            (goal_id, old["id"]),
        )
        await db.execute(
            "UPDATE agent_jobs SET result_json=? WHERE id=?",
            (json.dumps({**RESULT, "text": "mot " * 160}), old["id"]),
        )
        await db.commit()
        assert (
            await writing_completion_failure_locked(db, goal_id)
            == "document_requirement_unverifiable"
        )
