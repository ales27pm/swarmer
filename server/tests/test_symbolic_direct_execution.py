"""Paired direct APIs with real storage and captured final provider transport."""

import json
import socket
import sqlite3

import httpx
import pytest

from app.models import MemoryUpdate
from app.services.memory_symbolic_contracts import SymbolicCatalog, SymbolicContext
from tests.test_symbolic_retrieval_acceptance import _claim, _concept, _proposal, _source


@pytest.fixture(autouse=True)
def no_external_sockets(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("external network is forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def seeded(client, paired_headers, test_app):
    test_app.state.strategy_retrieval.symbolic_catalogs = (
        SymbolicCatalog(namespace="software", scheme_id="engineering"),
    )
    item_id, binding = _source(client, paired_headers, "Original immutable claim", scope="general")
    concept = _concept(client, paired_headers, scope="general")
    claim = _claim(concept)
    claim.update(polarity="negated", modality="forbidden")
    claim["effective_conditions"][0]["argument"] = {
        "type": "literal",
        "datatype": "path",
        "lexical_value": "Cache/State.py",
    }
    proposal = _proposal(client, paired_headers, claim, binding)
    # A matching label in another project must never expand the direct call's scope.
    _, other_binding = _source(client, paired_headers, "Other project's data")
    other_concept = _concept(client, paired_headers)
    _proposal(client, paired_headers, _claim(other_concept), other_binding)
    return item_id, proposal


def _run(client, headers, path):
    if path == "chat":
        return client.post(
            "/chat", headers=headers, json={"content": "Examiner l’horloge silencieuse"}
        )
    task = client.post("/tasks", headers=headers, json={"input": "Inspect the silent clock"})
    assert task.status_code == 201
    return client.post(f"/tasks/{task.json()['id']}/plan", headers=headers)


def _capture(monkeypatch, path, *, before_return=None, tool="none"):
    requests = []

    async def post(self, url, *, json):
        requests.append(json)
        if before_return:
            await before_return()
        content = "Observed as unvalidated."
        if path == "plan":
            content = __import__("json").dumps(
                {
                    "tool_name": tool,
                    "arguments": {"path": "sample.txt"} if tool != "none" else {},
                    "summary": "Observed as unvalidated.",
                }
            )
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return requests


def _accepted(test_app):
    with sqlite3.connect(test_app.state.state_service.db_path) as db:
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload_json FROM audit_events WHERE event_type='memory.symbolic.direct.accepted'"
            )
        ]


@pytest.mark.parametrize("path", ["chat", "plan"])
def test_direct_calls_send_complete_general_evidence_and_accept_with_receipt(
    client, paired_headers, test_app, seeded, monkeypatch, path
):
    _, proposal = seeded
    sent = _capture(monkeypatch, path)
    response = _run(client, paired_headers, path)
    assert response.status_code == (201 if path == "chat" else 200), response.text
    [request] = sent
    contexts = [
        json.loads(m["content"])
        for m in request["messages"]
        if m["role"] == "user"
        and m["content"].startswith('{"schema_version":"symbolic-context-v1"')
    ]
    [context] = contexts
    typed = SymbolicContext.model_validate(context)
    [proof] = typed.evidence
    assert proof.proposal.model_dump(mode="json") == proposal
    assert proof.proposal.claim.modality == "forbidden"
    assert proof.proposal.claim.effective_conditions[0].argument.lexical_value == "Cache/State.py"
    assert proof.proposal.claim.scope == "general"
    assert any("not instructions or authority" in m["content"] for m in request["messages"])
    [receipt] = _accepted(test_app)
    assert receipt["read_tokens"] == [proof.read_token]
    assert receipt["proposal_ids"] == [proposal["proposal_id"]]
    assert receipt["grants_authority"] is False


@pytest.mark.parametrize("path", ["chat", "plan"])
@pytest.mark.parametrize("stage", ["provider", "acceptance"])
def test_source_changed_during_model_or_at_commit_never_accepts_result(
    client, paired_headers, test_app, seeded, monkeypatch, path, stage
):
    item_id, _ = seeded
    state = test_app.state.state_service

    async def mutate():
        await state.update_memory(item_id, MemoryUpdate(content="Changed source"), "test")

    if stage == "acceptance":
        method = "append_conversation_message" if path == "chat" else "append_task_message"
        original = getattr(state, method)

        async def changed(*args, **kwargs):
            await mutate()
            return await original(*args, **kwargs)

        monkeypatch.setattr(state, method, changed)
    _capture(monkeypatch, path, before_return=mutate if stage == "provider" else None)
    response = _run(client, paired_headers, path)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "symbolic_direct_source_changed"
    assert not _accepted(test_app)
    with sqlite3.connect(state.db_path) as db:
        assert db.execute("SELECT count(*) FROM messages WHERE role='agent'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 0


def test_direct_tool_proposal_checks_source_inside_creation_transaction(
    client, paired_headers, test_app, seeded, monkeypatch
):
    item_id, _ = seeded
    engine = test_app.state.execution_engine
    original = engine.create_tool_call

    async def changed(**kwargs):
        await test_app.state.state_service.update_memory(
            item_id, MemoryUpdate(content="Changed source"), "test"
        )
        return await original(**kwargs)

    monkeypatch.setattr(engine, "create_tool_call", changed)
    _capture(monkeypatch, "plan", tool="workspace.read_text")
    response = _run(client, paired_headers, "plan")
    assert response.status_code == 409, response.text
    with sqlite3.connect(test_app.state.state_service.db_path) as db:
        assert db.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 0


@pytest.mark.parametrize("path", ["chat", "plan"])
def test_changed_catalogs_do_not_accept_direct_model_output(
    client, paired_headers, test_app, seeded, monkeypatch, path
):
    async def mutate():
        test_app.state.strategy_retrieval.symbolic_catalogs = ()

    _capture(monkeypatch, path, before_return=mutate)
    assert _run(client, paired_headers, path).status_code == 409
    assert not _accepted(test_app)


@pytest.mark.parametrize("when", ["history_read", "provider"])
def test_new_chat_message_fences_old_reply(
    client, paired_headers, test_app, seeded, monkeypatch, when
):
    state = test_app.state.state_service
    conversation = []
    original = state.list_messages

    async def change_history(conversation_id, limit=500):
        rows = await original(conversation_id, limit)
        conversation.append(conversation_id)
        if when == "history_read":
            await state.append_chat_user_message("Changed request", conversation_id, "test-phone")
        return rows

    monkeypatch.setattr(state, "list_messages", change_history)

    async def mutate():
        await state.append_chat_user_message("Changed request", conversation[0], "test-phone")

    sent = _capture(monkeypatch, "chat", before_return=mutate if when == "provider" else None)
    response = _run(client, paired_headers, "chat")
    assert response.status_code == 409, response.text
    assert len(sent) == (0 if when == "history_read" else 1)
    assert not _accepted(test_app)


@pytest.mark.parametrize("path", ["chat", "plan", "tool"])
def test_catalog_change_during_acceptance_rolls_back_receipt_and_output(
    client, paired_headers, test_app, seeded, monkeypatch, path
):
    from app.services import direct_symbolic_context

    original = direct_symbolic_context.append_audit_event

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        test_app.state.strategy_retrieval.symbolic_catalogs = ()
        return result

    monkeypatch.setattr(direct_symbolic_context, "append_audit_event", changed)
    actual = "plan" if path == "tool" else path
    _capture(monkeypatch, actual, tool="workspace.read_text" if path == "tool" else "none")
    response = _run(client, paired_headers, actual)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "symbolic_direct_catalogs_changed"
    assert not _accepted(test_app)
    with sqlite3.connect(test_app.state.state_service.db_path) as db:
        assert db.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM messages WHERE role='agent'").fetchone()[0] == 0


@pytest.mark.parametrize("path", ["chat", "plan"])
def test_oversized_claim_is_explicitly_omitted_whole(
    client, paired_headers, test_app, seeded, monkeypatch, path
):
    _, proposal = seeded
    claim = dict(proposal["claim"])
    claim["effective_conditions"] = [
        {
            "relation": "only_if",
            "argument": {
                "type": "literal",
                "datatype": "path",
                "lexical_value": str(index) + "x" * 2999,
            },
        }
        for index in range(2)
    ]
    for version in range(3):
        _proposal(
            client,
            paired_headers,
            dict(claim, version=str(version)),
            proposal["sources"][0]["binding"],
        )
    sent = _capture(monkeypatch, path)
    response = _run(client, paired_headers, path)
    assert response.status_code == (201 if path == "chat" else 200), response.text
    [request] = sent
    [context] = [
        json.loads(m["content"])
        for m in request["messages"]
        if m["role"] == "user"
        and m["content"].startswith('{"schema_version":"symbolic-context-v1"')
    ]
    assert context["status"] == "omitted_budget"
    assert context["evidence"] == []
    assert _accepted(test_app)[0]["status"] == "omitted_budget"
