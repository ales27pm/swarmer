from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.models import AgentCreate, TaskCreate, TaskRecord
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.message_board import MessageBoardService
from app.services.state_service import StateService
from app.services.writing_contracts import UnsupportedCitationError, validate_writing_result

LIBRARY_URL = "https://www.ville.sorel-tracy.qc.ca/loisirs/loisirs-et-culture/bibliotheques"
QUERY_URL = "https://example.org/library?lang=fr#services"
PAREN_URL = "https://example.org/wiki/Library_(public)"


def payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "objective": "Résumer les bibliothèques à partir des extraits fournis.",
        "conversation": [],
        "research_sources": [
            {
                "content_trust": "untrusted",
                "worker_job_id": f"job_source_{index}",
                "title": "Bibliothèque",
                "url": url,
                "snippet": "Services de bibliothèque.",
            }
            for index, url in enumerate((LIBRARY_URL, QUERY_URL, PAREN_URL))
        ],
    }


def result(text: str, summary: str = "Résumé des extraits fournis.") -> dict[str, str]:
    return {"schema_version": "1.0", "content_trust": "untrusted", "text": text, "summary": summary}


@pytest.fixture(scope="module")
def text_worker() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "workers/text-worker/text_worker.py"
    spec = importlib.util.spec_from_file_location("citation_text_worker", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "text",
    [
        LIBRARY_URL,
        f"'{LIBRARY_URL}'.",
        f"Consulter {LIBRARY_URL}.",
        f"Sources : {LIBRARY_URL}, puis les extraits.",
        f"[Bibliothèques]({LIBRARY_URL}).",
        f"[{LIBRARY_URL}]({LIBRARY_URL})",
        f"<{LIBRARY_URL}>; source fournie.",
        f"[Détails]({QUERY_URL}).",
        f"<{QUERY_URL}>.",
        f"[Article]({PAREN_URL}).",
        "Les extraits fournis sont insuffisants pour répondre à cette demande.",
    ],
)
def test_exact_citations_and_insufficient_evidence_pass_both_boundaries(
    text_worker: ModuleType, text: str
) -> None:
    value = result(text)
    assert validate_writing_result(value, payload=payload()) == value
    assert text_worker.validate_result(value, payload()) == value


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ville.sorel-tracy.qc.ca/contact",
        LIBRARY_URL + "/contact",
        LIBRARY_URL + "'other-path",
        QUERY_URL + "'other-fragment",
        LIBRARY_URL + "-other",
        LIBRARY_URL.replace(".qc.ca/", ".qc.ca.evil.example/"),
        LIBRARY_URL + "?redirect=other",
        LIBRARY_URL + "#other",
        QUERY_URL.replace("lang=fr", "lang=en"),
        QUERY_URL.replace("#services", "#contact"),
        QUERY_URL + ".",  # ambiguous query/fragment punctuation is not rewritten
        LIBRARY_URL + "?",
        LIBRARY_URL + "#",
        "https://evil.example/" + LIBRARY_URL,
        "https://evil.example/",
        LIBRARY_URL + ")" * 1000,
    ],
)
@pytest.mark.parametrize("field", ["text", "summary"])
def test_changed_destinations_or_summary_only_citations_fail_without_output_disclosure(
    text_worker: ModuleType, url: str, field: str
) -> None:
    value = result("PRIVATE-DRAFT-SENTINEL")
    value[field] += f" [Source]({url})"
    with pytest.raises(UnsupportedCitationError, match="^unsupported_citation$"):
        validate_writing_result(value, payload=payload())
    with pytest.raises(text_worker.GenerationError, match="^unsupported_citation$") as error:
        text_worker.validate_result(value, payload())
    assert text_worker.failure_reason(error.value) == "unsupported_citation"
    assert "PRIVATE-DRAFT-SENTINEL" not in str(error.value)


def test_unsourced_writing_remains_compatible(text_worker: ModuleType) -> None:
    value = result("Une suggestion : https://example.org/other.")
    for source_payload in (None, {**payload(), "research_sources": []}):
        assert validate_writing_result(value, payload=source_payload) == value
        assert text_worker.validate_result(value, source_payload) == value


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["text", "summary"])
async def test_direct_submission_checks_persisted_sources_before_accepting_any_draft(
    tmp_path: Path, field: str
) -> None:
    state = StateService(tmp_path / "state.db")
    await state.initialize()
    task = await state.create_task(
        TaskRecord.new(TaskCreate(input="Library summary"), source="test")
    )
    agent = await state.register_agent(
        AgentCreate(name="writer", endpoint="http://127.0.0.1:1", skills=["writing.draft"]),
        "test",
    )
    await state.heartbeat_agent(agent["id"], "online", agent["credential"])
    dispatcher = AgentDispatcher(state.db_path, MessageBoardService(state.db_path))
    request = payload()
    queued = await dispatcher.queue_job(task.id, "writing.draft", request)
    # A caller cannot alter the stored evidence by mutating its old payload.
    request["research_sources"][0]["url"] = "https://www.ville.sorel-tracy.qc.ca/contact"
    claimed = await dispatcher.claim(agent["id"])
    assert claimed is not None
    proof = {
        "lease_id": claimed["lease_id"],
        "lease_generation": claimed["lease_generation"],
    }
    bad = result("PRIVATE-DRAFT-SENTINEL")
    bad[field] += " [Information](https://www.ville.sorel-tracy.qc.ca/contact)"
    with pytest.raises(AgentDispatchConflict, match="^unsupported_citation$"):
        await dispatcher.submit_result(
            agent["id"],
            queued["id"],
            claimed["claim_token"],
            status="completed",
            result=bad,
            error=None,
            **proof,
        )
    current = await dispatcher.get_job(queued["id"])
    assert current is not None and current["status"] == "claimed" and current["result"] is None
    with sqlite3.connect(state.db_path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM audit_events WHERE task_id=? AND event_type='agent.job.completed'",
                (task.id,),
            ).fetchone()[0]
            == 0
        )
    good = result(f"Source fournie : [bibliothèque]({LIBRARY_URL})")
    accepted, changed = await dispatcher.submit_result(
        agent["id"],
        queued["id"],
        claimed["claim_token"],
        status="completed",
        result=good,
        error=None,
        **proof,
    )
    assert changed and accepted["status"] == "completed" and accepted["result"] == good


@pytest.mark.asyncio
async def test_invented_contact_link_cannot_complete_a_real_writing_node(tmp_path: Path) -> None:
    from app.services.writing_drafts import read_writing_draft
    from tests.test_goal_research_sources import researched

    manager, goal, _, node, writer = await researched(tmp_path)
    with pytest.raises(AgentDispatchConflict, match="^unsupported_citation$"):
        await manager.agent_dispatcher.submit_result(
            writer["claimed_by"],
            writer["id"],
            writer["claim_token"],
            status="completed",
            result=result("Renseignements : https://www.ville.sorel-tracy.qc.ca/contact"),
            error=None,
            lease_id=writer["lease_id"],
            lease_generation=writer["lease_generation"],
        )
    nodes = await manager.graph.list_nodes(goal["id"])
    unchanged = next(value for value in nodes if value["id"] == node["id"])
    assert unchanged["status"] == node["status"] != "completed"
    assert await read_writing_draft(manager.db_path, goal["id"], node["id"]) is None
    current = await manager.agent_dispatcher.get_job(writer["id"])
    assert current is not None and current["status"] == "claimed" and current["result"] is None
