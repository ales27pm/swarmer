from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import httpx
import pytest

from app.main import create_app
from app.models import MemoryCreate
from app.services.embedding_service import HttpEmbeddingService
from app.services.episode_memory import EpisodeMemoryService
from app.services.model_request_execution import ModelExecutionControlError
from app.services.state_service import StateService
from app.services.strategy_retrieval import StrategyRetrieval
from app.settings import Settings
from tests.test_memory_normalization import Model
from tests.test_memory_normalization import provider as normalizer_provider
from tests.test_memory_presentation import Models
from tests.test_memory_presentation import provider as presenter_provider
from tests.test_strategy_retrieval import _new_goal


class RecordingExecutor:
    def __init__(self, *, reject_role=None, before=None):
        self.calls = []
        self.reject_role = reject_role
        self.before = before
        self.failure = ModelExecutionControlError("goal execution no longer admitted")

    async def execute(self, *, role, model_id, endpoint, request_body, operation):
        self.calls.append((role, model_id, endpoint, request_body))
        if self.before is not None:
            await self.before(role)
        if role == self.reject_role:
            raise self.failure
        return await operation()


@pytest.mark.parametrize("canonical", [False, True])
def test_startup_uses_canonical_strategy_only_when_enabled(tmp_path, canonical):
    options = (
        {
            "memory_canonical_language": "en",
            "memory_normalization_base_url": "http://127.0.0.1:11434/v1",
            "memory_translator_model": "translator",
            "memory_reviewer_model": "reviewer",
        }
        if canonical
        else {}
    )
    app = create_app(
        Settings(
            _env_file=None,
            db_path=tmp_path / "state.db",
            workspace_root=tmp_path / "workspace",
            **options,
        )
    )
    assert app.state.strategy_retrieval.canonical_memory is (
        app.state.state_service if canonical else None
    )
    assert app.state.strategy_retrieval.episode_memory is app.state.episode_memory


async def prepared(tmp_path: Path, monkeypatch, *, source_language="fr"):
    model = Model()
    model.language = source_language
    normalizer = normalizer_provider(model)
    presenter = presenter_provider(Models())
    state = StateService(
        tmp_path / "state.db",
        canonical_language="en",
        memory_normalizer=normalizer,
        memory_presenter=presenter,
    )
    await state.initialize()
    goal, root = await _new_goal(state.db_path, "execution")
    other_goal, other_root = await _new_goal(state.db_path, "other_execution")
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "INSERT INTO coding_projects SELECT 'other_project',created_at,updated_at "
            "FROM coding_projects WHERE id='project_strategy'"
        )
        await db.execute(
            "UPDATE goal_project_links SET project_id='other_project' WHERE goal_run_id=?",
            (other_goal,),
        )
        await db.commit()
    allowed = []
    for scope, sensitivity in (
        ("general", "normal"),
        ("project:project_strategy", "normal"),
        ("project:other_project", "normal"),
        ("general", "secret"),
    ):
        item = await state.create_memory(
            MemoryCreate(
                content=(
                    "Ne pas envoyer automatiquement."
                    if source_language == "fr"
                    else "Do not send automatically."
                ),
                kind="constraint",
                scope=scope,
                sensitivity=sensitivity,
            ),
            "test",
        )
        if scope != "project:other_project" and sensitivity == "normal":
            allowed.append(item)
    # The query remains French even when its matching stored source is English.
    model.language = "fr"
    episodes = EpisodeMemoryService(state.db_path)
    for goal_id, root_id in ((goal, root), (other_goal, other_root)):
        await episodes.record_episode(
            goal_run_id=goal_id,
            root_task_id=root_id,
            objective_summary="Ne pas envoyer automatiquement.",
            plan_summary="A private plan must not be replayed.",
            outcome="completed",
        )
    # Keep real HTTP provider parsing while preventing any real socket access.
    client = httpx.AsyncClient
    embedding_requests = []

    def response(request):
        body = json.loads(request.content)
        embedding_requests.append(body)
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}]})

    def isolated_client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(response))
        return client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", isolated_client)
    embeddings = HttpEmbeddingService("http://localhost:8711/v1", "fixture-embedding")
    state.embedding_service = episodes.embedding_service = embeddings
    retrieval = StrategyRetrieval(state.db_path, episodes, canonical_memory=state)
    return retrieval, state, goal, allowed, embedding_requests


@pytest.mark.asyncio
@pytest.mark.parametrize("source_language", ["fr", "en"])
async def test_real_strategy_passes_executor_to_all_memory_calls_and_preserves_scope(
    tmp_path,
    monkeypatch,
    source_language,
):
    retrieval, state, goal, allowed, requests = await prepared(
        tmp_path, monkeypatch, source_language=source_language
    )
    executor = RecordingExecutor()
    hints = await retrieval.retrieve(
        "Dois-je envoyer automatiquement ?",
        goal_run_id=goal,
        model_executor=executor,
    )
    assert [call[0] for call in executor.calls] == [
        "memory_embedder",
        "memory_embedder",
        "memory_normalizer",
        "memory_reviewer",
        "memory_embedder",
    ] + (["memory_presenter", "memory_presentation_reviewer"] if source_language == "en" else [])
    assert len(requests) == 3
    assert len(hints.successful) == 1 and not hints.failures
    assert {hint.source_id for hint in hints.memory} == {item["id"] for item in allowed}
    for hint in hints.memory:
        assert hint.text == "Ne pas envoyer automatiquement."
        assert hint.canonical_language == "en" and hint.presentation_language == "fr"
        assert hint.canonical_content_sha256 and hint.canonical_metadata_sha256
        assert hint.canonical_receipt_id and hint.source_revision
        assert (await state.get_memory(hint.source_id))["content"] == "Do not send automatically."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [
        "memory_embedder",
        "memory_normalizer",
        "memory_reviewer",
        "memory_presenter",
        "memory_presentation_reviewer",
    ],
)
async def test_execution_control_is_not_hidden_by_empty_or_lexical_results(
    tmp_path,
    monkeypatch,
    role,
):
    retrieval, _, goal, _, _ = await prepared(tmp_path, monkeypatch, source_language="en")
    executor = RecordingExecutor(reject_role=role)
    with pytest.raises(ModelExecutionControlError) as caught:
        await retrieval.retrieve(
            "Dois-je envoyer automatiquement ?",
            goal_run_id=goal,
            model_executor=executor,
        )
    assert caught.value is executor.failure
    assert executor.calls[-1][0] == role


@pytest.mark.asyncio
async def test_goal_scope_change_during_embedding_cannot_return_another_project(
    tmp_path,
    monkeypatch,
):
    retrieval, state, goal, _, _ = await prepared(tmp_path, monkeypatch)
    changed = False

    async def mutate(role):
        nonlocal changed
        if role == "memory_embedder" and not changed:
            changed = True
            async with aiosqlite.connect(state.db_path) as db:
                await db.execute(
                    "UPDATE goal_project_links SET project_id='other_project' WHERE goal_run_id=?",
                    (goal,),
                )
                await db.commit()

    hints = await retrieval.retrieve(
        "Dois-je envoyer automatiquement ?",
        goal_run_id=goal,
        model_executor=RecordingExecutor(before=mutate),
    )
    assert hints.provenance_ids == ()
    assert hints.memory == hints.successful == hints.failures == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["content", "scope", "metadata"])
async def test_final_snapshot_still_fences_sources_after_executor_driven_search(
    tmp_path,
    monkeypatch,
    change,
):
    retrieval, state, goal, allowed, _ = await prepared(tmp_path, monkeypatch)

    class ChangedAfterSearch(StrategyRetrieval):
        async def _canonical_memory_hints(self, query, *, goal_run_id, model_executor=None):
            hints = await super()._canonical_memory_hints(
                query,
                goal_run_id=goal_run_id,
                model_executor=model_executor,
            )
            assert hints
            async with aiosqlite.connect(self.db_path, timeout=0) as db:
                if change == "content":
                    statement = "UPDATE memory_items SET content='Send automatically.' WHERE id=?"
                elif change == "scope":
                    statement = "UPDATE memory_items SET scope='project:other_project' WHERE id=?"
                else:
                    statement = "UPDATE memory_items SET metadata_json='{}' WHERE id=?"
                await db.executemany(statement, [(item["id"],) for item in allowed])
                await db.commit()
            return hints

    fenced = ChangedAfterSearch(
        state.db_path,
        retrieval.episode_memory,
        canonical_memory=state,
    )
    hints = await fenced.retrieve(
        "Dois-je envoyer automatiquement ?",
        goal_run_id=goal,
        model_executor=RecordingExecutor(),
    )
    assert not hints.memory
    assert not {item["id"] for item in allowed}.intersection(hints.provenance_ids)
