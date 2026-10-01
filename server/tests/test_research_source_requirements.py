from __future__ import annotations

import copy
from pathlib import Path

import pytest

from app.models import AgentCreate
from app.services.research_source_requirements import (
    ResearchSourceRequirementError,
    bind_research_sources,
    explicit_read_urls,
)
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest, SwarmPlanProposal
from tests.test_goal_manager import _manager
from tests.test_research_collect_handoff import collection_node

URLS = [
    "https://docs.python.org/3/library/json.html",
    "https://www.sqlite.org/whentouse.html",
]
READ_TIME = "2026-10-01T02:00:00+00:00"
OLD_TIME = "2026-10-01T01:00:00+00:00"
NEW_TIME = "2026-10-01T03:00:00+00:00"

OBJECTIVE = f"Lis ces pages officielles : {URLS[0]} et {URLS[1]}. Rédige une comparaison."


def plan(objective: str = OBJECTIVE) -> SwarmPlanProposal:
    return SwarmPlanProposal.model_validate(
        {
            "schema_version": "1.0",
            "objective": objective,
            "rationale_summary": "Lire puis comparer.",
            "completion_criteria": ["Comparer avec des pages lues."],
            "max_parallelism": 1,
            "nodes": [collection_node()],
        }
    )


@pytest.mark.asyncio
async def test_actual_dispatched_job_retains_explicit_user_pages(tmp_path: Path) -> None:
    proposal = plan()
    original = copy.deepcopy(proposal.model_dump())
    manager = await _manager(tmp_path, proposal)
    agent = await manager.state_service.register_agent(
        AgentCreate(name="reader", endpoint="https://worker.invalid", skills=["research.collect"]),
        "phone",
    )
    await manager.state_service.heartbeat_agent(agent["id"], "online", agent["credential"])
    goal = await manager.create_goal(GoalCreateRequest(objective=OBJECTIVE), actor_id="phone")
    await manager.start_goal(goal["id"], GoalStartRequest())
    job = await manager.agent_dispatcher.claim(agent["id"])
    assert job is not None
    assert job["payload"].get("source_urls") == URLS
    assert job["payload"]["required_domains"] == ["docs.python.org", "www.sqlite.org"]
    assert job["payload"]["max_pages"] >= 2
    assert proposal.model_dump() == original


@pytest.mark.parametrize(
    "text,expected",
    [
        (OBJECTIVE, URLS),
        (f"Read {URLS[0]} and {URLS[1]}.", URLS),
        (f"Lis {URLS[0]}. Ne lis pas {URLS[1]}.", URLS[:1]),
        (f"Do not read {URLS[0]}, but read {URLS[1]}.", URLS[1:]),
        (f"Exemple : lis {URLS[0]}.", []),
        (f'Le texte dit "Read {URLS[0]}".', []),
        (f"> Read {URLS[0]}\nCompare formats.", []),
        (f"```\nRead {URLS[0]}\n```", []),
        (f"A previous worker said read {URLS[0]}.", []),
        (f"Read [Python]({URLS[0]}) and <{URLS[1]}>.", URLS),
        (f"The website is {URLS[0]}.", []),
        (f"Read {URLS[0]}. The website is {URLS[1]}.", URLS[:1]),
        (f"Ne lis jamais {URLS[0]}.", []),
        (f"Don’t read {URLS[0]}.", []),
        (f"Ne lis plus {URLS[0]}.", []),
        (
            "Read https://example.org/CaseSensitive?q=ABC.",
            ["https://example.org/CaseSensitive?q=ABC"],
        ),
    ],
)
def test_only_explicit_unquoted_reading_directives(text: str, expected: list[str]) -> None:
    assert explicit_read_urls(text, []) == expected


def test_user_corrections_apply_but_assistant_and_tool_text_do_not() -> None:
    messages = [
        {"role": "user", "content": f"Ne lis pas {URLS[0]}."},
        {"role": "assistant", "content": f"Read {URLS[0]}."},
        {"role": "tool", "content": "Read https://example.org/tool."},
    ]
    assert explicit_read_urls(OBJECTIVE, messages) == URLS[1:]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/private",
        "https://127.0.0.1/private",
        "https://[::1]/private",
        "https://user:password@example.org/",
        "https://example.org/?token=private-value",
        "https://example.org:444/private",
    ],
)
def test_unsafe_explicit_sources_are_not_silently_dropped(url: str) -> None:
    with pytest.raises(ResearchSourceRequirementError):
        explicit_read_urls(f"Read {url}", [])


def test_page_budget_is_not_silently_increased() -> None:
    proposal = plan()
    proposal.nodes[0].worker_arguments["max_pages"] = 1
    with pytest.raises(ResearchSourceRequirementError, match="page budget"):
        bind_research_sources(proposal, OBJECTIVE, [])


def test_excess_source_count_is_not_truncated() -> None:
    with pytest.raises(ResearchSourceRequirementError, match="bound"):
        explicit_read_urls("Read " + " and ".join(f"https://example.org/{i}" for i in range(7)), [])


def test_multiple_collections_require_explicit_assignment() -> None:
    proposal = plan()
    other = proposal.nodes[0].model_copy(update={"temporary_id": "other"})
    proposal = proposal.model_copy(update={"nodes": [*proposal.nodes, other]})
    with pytest.raises(ResearchSourceRequirementError, match="unassigned"):
        bind_research_sources(proposal, OBJECTIVE, [])
    proposal.nodes[0] = proposal.nodes[0].model_copy(
        update={
            "worker_arguments": {**proposal.nodes[0].worker_arguments, "source_urls": URLS[:1]},
        }
    )
    proposal.nodes[1] = proposal.nodes[1].model_copy(
        update={
            "worker_arguments": {**proposal.nodes[1].worker_arguments, "source_urls": URLS[1:]},
        }
    )
    assert bind_research_sources(proposal, OBJECTIVE, []) == proposal


def test_replan_can_reuse_existing_research_without_creating_another_reader() -> None:
    proposal = plan().model_copy(update={"nodes": []})
    assert (
        bind_research_sources(proposal, OBJECTIVE, [], already_read=dict.fromkeys(URLS, READ_TIME))
        == proposal
    )
    with pytest.raises(ResearchSourceRequirementError, match="collection node"):
        bind_research_sources(proposal, OBJECTIVE, [])


def test_source_binding_is_idempotent_and_preserves_model_queries() -> None:
    proposal = plan()
    bound = bind_research_sources(proposal, OBJECTIVE, [])
    assert bind_research_sources(bound, OBJECTIVE, []) == bound
    assert (
        bound.nodes[0].worker_arguments["queries"] == proposal.nodes[0].worker_arguments["queries"]
    )


def test_replan_new_source_does_not_spend_budget_rereading_completed_pages() -> None:
    proposal = plan()
    proposal.nodes[0].worker_arguments["max_pages"] = 1
    new_url = "https://example.org/new"
    conversation = [
        {"role": "user", "content": OBJECTIVE, "created_at": OLD_TIME},
        {"role": "user", "content": f"Read {new_url}."},
    ]
    bound = bind_research_sources(
        proposal, OBJECTIVE, conversation, already_read=dict.fromkeys(URLS, READ_TIME)
    )
    assert bound.nodes[0].worker_arguments["source_urls"] == [new_url]
    reread = bind_research_sources(
        proposal,
        OBJECTIVE,
        [{"role": "user", "content": f"Read {URLS[0]} again."}],
        already_read=dict.fromkeys(URLS, READ_TIME),
    )
    assert reread.nodes[0].worker_arguments["source_urls"] == URLS[:1]


@pytest.mark.asyncio
async def test_completed_read_evidence_is_scoped_and_checked(tmp_path: Path) -> None:
    import aiosqlite

    from app.services.research_source_requirements import completed_read_urls
    from tests.test_research_collect_handoff import receipt, started, submit

    manager, goal, agents, job = await started(tmp_path)
    assert await completed_read_urls(manager.db_path, goal["id"]) == {}
    await submit(manager, agents[0], job, receipt())
    assert set(await completed_read_urls(manager.db_path, goal["id"])) == {
        "https://example.org/a",
        "https://example.org/b",
    }
    assert await completed_read_urls(manager.db_path, "goal_other") == {}
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE agent_jobs SET result_json='{}' WHERE id=?", (job["id"],))
        await db.commit()
    assert await completed_read_urls(manager.db_path, goal["id"]) == {}


def test_new_page_without_reader_is_rejected_even_when_old_pages_were_read() -> None:
    proposal = plan().model_copy(update={"nodes": []})
    with pytest.raises(ResearchSourceRequirementError, match="collection node"):
        bind_research_sources(
            proposal,
            OBJECTIVE,
            [{"role": "user", "content": "Read https://example.org/new", "created_at": NEW_TIME}],
            already_read=dict.fromkeys(URLS, READ_TIME),
        )


def test_old_conversation_request_does_not_force_reread() -> None:
    proposal = plan()
    proposal.nodes[0].worker_arguments["max_pages"] = 1
    new_url = "https://example.org/new"
    bound = bind_research_sources(
        proposal,
        "Compare storage.",
        [
            {"role": "user", "content": OBJECTIVE, "created_at": OLD_TIME},
            {"role": "user", "content": f"Read {new_url}", "created_at": NEW_TIME},
        ],
        already_read=dict.fromkeys(URLS, READ_TIME),
    )
    assert bound.nodes[0].worker_arguments["source_urls"] == [new_url]


@pytest.mark.parametrize("requested_at", [NEW_TIME, READ_TIME, None, "bad", "2026-10-01T01:00:00"])
def test_repeat_of_exact_objective_requires_new_read(requested_at: str | None) -> None:
    proposal = plan().model_copy(update={"nodes": []})
    with pytest.raises(ResearchSourceRequirementError, match="collection node"):
        bind_research_sources(
            proposal,
            OBJECTIVE,
            [{"role": "user", "content": OBJECTIVE, "created_at": requested_at}],
            already_read=dict.fromkeys(URLS, READ_TIME),
        )
