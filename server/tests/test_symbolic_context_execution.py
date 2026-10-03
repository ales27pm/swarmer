"""Real SQL symbolic activation through planner context, receipts, and late fences."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from app.main import create_app
from app.models import MemoryUpdate
from app.services.context_builder import ContextBuilder, ContextCard
from app.services.episode_memory import EpisodeMemoryService
from app.services.goal_manager import GoalManagerConflict
from app.services.memory_concepts import (
    ConceptLabel,
    EffectiveCondition,
    LanguageAnnotation,
    TypedLiteral,
)
from app.services.memory_symbolic_contracts import SymbolicCatalog, SymbolicEvidence
from app.services.memory_symbolic_search import (
    revalidate_symbolic_evidence,
    search_symbolic_evidence,
)
from app.services.strategy_retrieval import StrategyRetrieval
from app.services.swarm_contracts import GoalCreateRequest, GoalStartRequest
from app.settings import Settings
from tests.test_goal_manager import _manager, _parallel_plan
from tests.test_memory_symbolic_store import concept, identity, memory, propose

CATALOGS = (SymbolicCatalog(namespace="software", scheme_id="engineering"),)


async def prepared(tmp_path: Path):
    manager = await _manager(tmp_path, _parallel_plan())
    state = manager.state_service
    first, a = await memory(state, "Dossier Z91: garder les dates.")
    second, b = await memory(state, "Dossier Y72: conserver les noms.")
    label = await concept(
        state,
        labels=[
            ConceptLabel(
                text="silent clock", language=LanguageAnnotation(tag="en", origin="declared")
            ),
            ConceptLabel(
                text="horloge silencieuse",
                language=LanguageAnnotation(tag="fr-CA", origin="declared"),
            ),
        ],
    )
    proposal = await propose(
        state,
        [a.bindings[0], b.bindings[0]],
        subject=identity(label.concept_id),
        polarity="negated",
        modality="forbidden",
        version="v01.2+RC",
        effective_conditions=[
            EffectiveCondition(
                relation="only_after",
                argument=TypedLiteral(datatype="path", lexical_value="Cache/State.py"),
            )
        ],
    )
    episodes = EpisodeMemoryService(state.db_path)
    await episodes.initialize()
    manager.strategy_retrieval = StrategyRetrieval(
        state.db_path, episodes, symbolic_catalogs=CATALOGS
    )
    manager.context_builder = ContextBuilder(state.db_path, max_tokens=8192)
    created = await manager.create_goal(
        GoalCreateRequest(objective="Inspect the silent clock", max_model_calls=5), actor_id="test"
    )
    goal = await manager._mark_start_requested(created["id"])
    return manager, goal, proposal, label, first, second


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["Inspect the silent clock", "Examiner l’horloge silencieuse"])
async def test_explicit_activation_reaches_planner_as_whole_unvalidated_data(
    tmp_path, monkeypatch, query
):
    manager, goal, proposal, _, first, second = await prepared(tmp_path)
    # Exercise trusted Settings -> real application wiring -> agent context;
    # only the final planner remains the deterministic provider from _manager.
    app = create_app(
        Settings(
            _env_file=None,
            db_path=manager.db_path,
            workspace_root=tmp_path / "workspace",
            memory_symbolic_catalogs=list(CATALOGS),
        )
    )
    assert app.state.strategy_retrieval.symbolic_catalogs == CATALOGS
    assert app.state.agent_dispatcher.symbolic_catalogs == CATALOGS
    manager.strategy_retrieval = app.state.strategy_retrieval
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE goal_runs SET objective=? WHERE id=?", (query, goal["id"]))
        await db.commit()
    goal = await manager.graph.get_goal(goal["id"])
    seen = []
    original = manager.planner.propose

    async def capture(payload):
        seen.append(payload)
        return await original(payload)

    monkeypatch.setattr(manager.planner, "propose", capture)
    await manager._obtain_plan(goal, GoalStartRequest())
    [payload] = seen
    [card] = [card for card in payload["cards"] if card["kind"] == "symbolic_memory_hint"]
    proof = SymbolicEvidence.model_validate(card["symbolic"])
    assert proof.proposal == proposal
    assert proof.validation_status == "unvalidated" and not proof.grants_authority
    assert proof.proposal.claim.effective_conditions[0].argument.lexical_value == "Cache/State.py"
    async with aiosqlite.connect(manager.db_path) as db:
        saved = await (
            await db.execute("SELECT id,provenance_json FROM goal_contexts WHERE purpose='planner'")
        ).fetchone()
        [call] = await (await db.execute("SELECT role FROM goal_model_calls")).fetchall()
    assert call[0] == "planner"  # SQL discovery adds no model call.
    receipt = json.loads(saved[1])["strategy_retrieval"]
    retained = receipt["symbolic"][card["card_id"]]
    assert {s["binding"]["memory_id"] for s in retained["proposal"]["sources"]} == {
        first["id"],
        second["id"],
    }
    restored = await manager.context_builder.get(saved[0])
    assert next(c for c in restored.cards if c.symbolic is not None).as_model_dict() == card


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["context_build", "reservation"])
@pytest.mark.parametrize("change", ["secondary_source", "label", "project", "catalogs"])
async def test_late_symbolic_change_never_reaches_planner(tmp_path, monkeypatch, stage, change):
    manager, goal, _, label, _, second = await prepared(tmp_path)
    sent = []

    async def forbidden(payload):
        sent.append(payload)
        pytest.fail("stale symbolic evidence reached planner")

    monkeypatch.setattr(manager.planner, "propose", forbidden)

    async def mutate():
        if change == "secondary_source":
            await manager.state_service.update_memory(
                second["id"], MemoryUpdate(content="Changed source"), "test"
            )
        elif change == "catalogs":
            manager.strategy_retrieval.symbolic_catalogs = ()
        else:
            async with aiosqlite.connect(manager.db_path) as db:
                if change == "label":
                    await db.execute(
                        "UPDATE memory_symbolic_labels SET text='replacement label' WHERE concept_id=? AND ordinal=0",
                        (label.concept_id,),
                    )
                else:
                    await db.execute(
                        "INSERT INTO coding_projects VALUES('elsewhere','2026','2026')"
                    )
                    await db.execute(
                        "UPDATE goal_project_links SET project_id='elsewhere' WHERE goal_run_id=?",
                        (goal["id"],),
                    )
                await db.commit()

    if stage == "context_build":
        original = manager.context_builder.build_for_goal

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            await mutate()
            return result

        monkeypatch.setattr(manager.context_builder, "build_for_goal", changed)
    else:
        original = manager._reserve_model_call

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            await mutate()
            return result

        monkeypatch.setattr(manager, "_reserve_model_call", changed)
    with pytest.raises(GoalManagerConflict):
        await manager._obtain_plan(goal, GoalStartRequest())
    assert not sent
    async with aiosqlite.connect(manager.db_path) as db:
        rows = await (await db.execute("SELECT status FROM goal_model_calls")).fetchall()
    assert rows == ([] if stage == "context_build" else [("failed",)])


@pytest.mark.asyncio
async def test_budget_omits_whole_symbolic_card_without_hiding_later_small_card(tmp_path):
    manager, goal, _, _, _, _ = await prepared(tmp_path)
    hints = await manager.strategy_retrieval.retrieve("silent clock", goal_run_id=goal["id"])
    [proof] = hints.symbolic
    symbolic = ContextCard("proof", "symbolic_memory_hint", "Unvalidated", (), symbolic=proof)
    small = ContextCard("later", "strategy_hint", "Useful later context", ("source",))
    builder = ContextBuilder(manager.db_path, max_tokens=120)
    cards = builder._bounded_cards((symbolic, small), max_cards=2, purpose="planner")
    assert [card.card_id for card in cards] == ["later"]
    assert not any("symbolic" in card.as_model_dict() for card in cards)


@pytest.mark.asyncio
async def test_revalidation_rejects_fabricated_match_even_with_recomputed_token(tmp_path):
    import hashlib

    manager, _, _, _, _, _ = await prepared(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("BEGIN")
        [proof] = await search_symbolic_evidence(
            db, "silent clock", allowed_scopes=("general",), catalogs=CATALOGS
        )
        changed = proof.model_dump()
        changed["matches"][0]["value"] = "fabricated label"
        changed["read_token"] = hashlib.sha256(
            json.dumps(
                {k: v for k, v in changed.items() if k != "read_token"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert not await revalidate_symbolic_evidence(
            db,
            SymbolicEvidence.model_validate(changed),
            allowed_scopes=("general",),
            catalogs=CATALOGS,
        )


@pytest.mark.asyncio
async def test_omission_receipt_does_not_claim_symbolic_evidence_was_presented(
    tmp_path, monkeypatch
):
    manager, goal, proposal, _, _, _ = await prepared(tmp_path)
    manager.context_builder = ContextBuilder(manager.db_path, max_tokens=500)
    seen = []
    original = manager.planner.propose

    async def capture(payload):
        seen.append(payload)
        return await original(payload)

    monkeypatch.setattr(manager.planner, "propose", capture)
    await manager._obtain_plan(goal, GoalStartRequest())
    assert all("symbolic" not in card for card in seen[0]["cards"])
    async with aiosqlite.connect(manager.db_path) as db:
        [row] = await (
            await db.execute("SELECT provenance_json FROM goal_contexts WHERE purpose='planner'")
        ).fetchall()
    receipt = json.loads(row[0])["strategy_retrieval"]
    assert receipt["symbolic"] == {}
    assert receipt["symbolic_omitted_budget"] == [proposal.proposal_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,matched",
    [
        ("Cache/State.py", True),
        ("tmp/Cache/State.py", False),
        ("Cache/State.py.bak", False),
        ("Cache/State.py\u0301", False),
        ("🧪Cache/State.py", False),
        ("Cache/State.py#L12", False),
        ("Cache/State.py?raw=1", False),
        ("Cache/State.py%20backup", False),
        ("Cache/State.py-suffix", False),
        ("Cache/State.py:metadata", False),
        ("prefix\\Cache/State.py", False),
        ("Inspect `Cache/State.py` now", True),
        ("v01.2+RC", True),
        ("v01.2+RC.1", False),
        ("v01.2+rc", False),
    ],
)
async def test_exact_identity_does_not_match_another_path_or_version(tmp_path, query, matched):
    manager, _, _, _, _, _ = await prepared(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("BEGIN")
        evidence = await search_symbolic_evidence(
            db, query, allowed_scopes=("general",), catalogs=CATALOGS
        )
    assert bool(evidence) is matched
    if evidence:
        assert all(match.channel == "exact_identity" for match in evidence[0].matches)


async def canonical_symbolic(tmp_path):
    from tests.test_memory_search_views import canonical_state

    state, item = await canonical_state(tmp_path)
    described = await state.symbolic_memory.describe_source(item["id"], scope="general")
    label = await concept(
        state,
        labels=[
            ConceptLabel(
                text="horloge silencieuse", language=LanguageAnnotation(tag="fr", origin="declared")
            )
        ],
    )
    proposal = await propose(state, [described.bindings[0]], subject=identity(label.concept_id))
    return state, item, label, proposal


@pytest.mark.asyncio
async def test_canonical_symbolic_only_match_uses_same_french_original_presentation(tmp_path):
    from app.models import MemorySearch
    from app.services.memory_symbolic_contracts import SymbolicSearchOptions

    state, item, _, proposal = await canonical_symbolic(tmp_path)
    found = await state.search_memory(
        MemorySearch(
            query="horloge silencieuse",
            scope="general",
            symbolic=SymbolicSearchOptions(catalogs=list(CATALOGS)),
        )
    )
    assert len(state.memory_normalizer.calls) == 1  # No duplicate query normalization.
    assert [row["id"] for row in found] == [item["id"]]
    assert found[0]["search_kind"] == "symbolic"
    assert found[0]["content"] == "Keep the exact dates."
    assert found[0]["presentation"]["mode"] == "original"
    assert found[0]["presentation"]["content"] == "Garder les dates exactes."
    assert found[0]["symbolic_evidence"][0]["proposal"] == proposal.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["receipt", "head"])
async def test_unqualified_canonical_symbolic_only_memory_is_explicitly_refused(tmp_path, change):
    from app.models import MemorySearch
    from app.services.memory_normalization import MemoryNormalizationError
    from app.services.memory_symbolic_contracts import SymbolicSearchOptions
    from app.services.memory_symbolic_store import SymbolicStoreError

    state, _, _, _ = await canonical_symbolic(tmp_path)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE memory_canonical_receipts SET status='failed'"
            if change == "receipt"
            else "UPDATE memory_text_heads SET revision=revision+1"
        )
        await db.commit()
    with pytest.raises((MemoryNormalizationError, SymbolicStoreError)):
        await state.search_memory(
            MemorySearch(
                query="horloge silencieuse",
                scope="general",
                symbolic=SymbolicSearchOptions(catalogs=list(CATALOGS)),
            )
        )


@pytest.mark.asyncio
async def test_symbolic_label_changed_during_presentation_is_not_returned(tmp_path, monkeypatch):
    from app.models import MemorySearch
    from app.services import state_service as state_module
    from app.services.memory_normalization import MemoryNormalizationError
    from app.services.memory_symbolic_contracts import SymbolicSearchOptions

    state, _, label, _ = await canonical_symbolic(tmp_path)
    original = state_module.finalize_memory_search

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with aiosqlite.connect(state.db_path) as db:
            await db.execute(
                "UPDATE memory_symbolic_labels SET text='different' WHERE concept_id=?",
                (label.concept_id,),
            )
            await db.commit()
        return result

    monkeypatch.setattr(state_module, "finalize_memory_search", changed)
    with pytest.raises(MemoryNormalizationError, match="symbolic_source_changed"):
        await state.search_memory(
            MemorySearch(
                query="horloge silencieuse",
                scope="general",
                symbolic=SymbolicSearchOptions(catalogs=list(CATALOGS)),
            )
        )


@pytest.mark.asyncio
async def test_unrelated_proposals_do_not_add_sql_qualification_roundtrips(tmp_path):
    from app.services.memory_symbolic_search import search_symbolic_memories

    manager, _, proposal, _, _, _ = await prepared(tmp_path)
    unrelated = await propose(
        manager.state_service,
        [source.binding for source in proposal.sources],
        subject=identity("unrelated"),
    )

    async def count(query):
        statements = []
        async with aiosqlite.connect(manager.db_path) as db:
            await db.execute("BEGIN")
            await db.set_trace_callback(
                lambda sql: (
                    statements.append(sql)
                    if sql.lstrip().upper().startswith(("SELECT", "WITH"))
                    else None
                )
            )
            value = await search_symbolic_memories(
                db, query, allowed_scopes=("general",), catalogs=CATALOGS, limit=1
            )
        return len(statements), value

    baseline_missing, _ = await count("nonmatching token")
    baseline_hit, expected = await count("silent clock")
    async with aiosqlite.connect(manager.db_path) as db:
        await db.executemany(
            """INSERT INTO memory_symbolic_proposals SELECT ?,scope,namespace,scheme_id,claim_json,claim_sha256,validation_status,grants_authority,created_at FROM memory_symbolic_proposals WHERE id=?""",
            [(f"unrelated_{i}", unrelated.proposal_id) for i in range(500)],
        )
        await db.executemany(
            """INSERT INTO memory_symbolic_sources SELECT ?,ordinal,memory_id,revision,view_id,field,field_sha256,document_sha256,scope,origin FROM memory_symbolic_sources WHERE proposal_id=?""",
            [(f"unrelated_{i}", unrelated.proposal_id) for i in range(500)],
        )
        await db.commit()
    after_missing, empty = await count("nonmatching token")
    after_hit, actual = await count("silent clock")
    assert empty == ([], []) and actual == expected
    assert after_missing == baseline_missing == 1
    assert after_hit == baseline_hit


@pytest.mark.asyncio
async def test_valid_oversized_symbolic_evidence_is_wholly_omitted_for_planner(
    tmp_path, monkeypatch
):
    manager, goal, proposal, _, _, _ = await prepared(tmp_path)
    labels = [
        ConceptLabel(
            text=f"label{i:03}",
            language=LanguageAnnotation(tag="en", origin="declared"),
            role="pref" if i == 0 else "alt",
        )
        for i in range(64)
    ]
    label = await concept(manager.state_service, labels=labels)
    await propose(
        manager.state_service,
        [source.binding for source in proposal.sources],
        subject=identity(label.concept_id),
        object=identity("Archive.py"),
    )
    query = " ".join([label.text for label in labels] + ["Archive.py"])
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE goal_runs SET objective=? WHERE id=?", (query, goal["id"]))
        await db.commit()
    goal = await manager.graph.get_goal(goal["id"])
    hints = await manager.strategy_retrieval.retrieve(query, goal_run_id=goal["id"])
    assert hints.symbolic_status == "omitted_budget" and hints.symbolic == ()
    seen = []
    original = manager.planner.propose

    async def capture(payload):
        seen.append(payload)
        return await original(payload)

    monkeypatch.setattr(manager.planner, "propose", capture)
    await manager._obtain_plan(goal, GoalStartRequest())
    assert not any("symbolic" in card for card in seen[0]["cards"])
    assert any(card["card_id"] == "strategy:symbolic:availability" for card in seen[0]["cards"])
    async with aiosqlite.connect(manager.db_path) as db:
        [row] = await (
            await db.execute("SELECT provenance_json FROM goal_contexts WHERE purpose='planner'")
        ).fetchall()
    receipt = json.loads(row[0])["strategy_retrieval"]
    assert receipt["symbolic_status"] == "omitted_budget" and receipt["symbolic"] == {}


@pytest.mark.asyncio
async def test_symbolic_only_malformed_public_metadata_keeps_explicit_error_path(tmp_path):
    from app.models import MemorySearch
    from app.services.memory_normalization import MemoryNormalizationError
    from app.services.memory_symbolic_contracts import SymbolicSearchOptions

    manager, _, _, _, first, _ = await prepared(tmp_path)
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE memory_items SET metadata_json='{' WHERE id=?", (first["id"],))
        await db.commit()
    with pytest.raises(MemoryNormalizationError):
        await manager.state_service.search_memory(
            MemorySearch(
                query="silent clock",
                scope="general",
                symbolic=SymbolicSearchOptions(catalogs=list(CATALOGS)),
            )
        )
