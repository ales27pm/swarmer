"""Goal-scoped memory calls account for actual, strictly parsed HTTP requests."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import httpx
import pytest
from test_memory_normalization import Model, provider, source
from test_memory_presentation import Models, batch
from test_memory_presentation import provider as presentation_provider

from app.services.embedding_service import (
    DeterministicEmbeddingService,
    EmbeddingServiceError,
    HttpEmbeddingService,
)
from app.services.memory_normalization import MemoryNormalizationError
from app.services.model_request_execution import (
    MemoryModelRole,
    ModelExecutionControlError,
    ModelRequestBudgetUnavailable,
)

Result = TypeVar("Result")


class RecordingExecutor:
    def __init__(self, failure: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failure = failure

    async def execute(
        self,
        *,
        role: MemoryModelRole,
        model_id: str,
        endpoint: str,
        request_body: dict[str, Any],
        operation: Callable[[], Awaitable[Result]],
    ) -> Result:
        call = {
            "role": role,
            "model_id": model_id,
            "endpoint": endpoint,
            "request_body": copy.deepcopy(request_body),
            "outcome": "running",
        }
        self.calls.append(call)
        try:
            if self.failure is not None:
                raise self.failure
            result = await operation()
        except BaseException as exc:
            call["outcome"] = type(exc).__name__
            raise
        call["outcome"] = "completed"
        return result


@asynccontextmanager
async def forbidden_direct_admission() -> AsyncIterator[None]:
    raise AssertionError("goal executor must replace direct admission")
    yield


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", [False, True])
async def test_each_memory_http_request_uses_exact_goal_scope_without_nested_admission(
    presentation: bool,
) -> None:
    executor = RecordingExecutor()
    model = Models() if presentation else Model()
    factory = presentation_provider if presentation else provider
    service = factory(model, model_admission=forbidden_direct_admission)
    before = service._configuration_fingerprint()
    if presentation:
        await service.present(batch(), model_executor=executor)
        roles = ["memory_presenter", "memory_presentation_reviewer"]
    else:
        await service.normalize(source(), model_executor=executor)
        roles = ["memory_normalizer", "memory_reviewer"]
    assert service._configuration_fingerprint() == before
    assert len(executor.calls) == len(model.calls) == 2
    assert [call["role"] for call in executor.calls] == roles
    for receipt, body in zip(executor.calls, model.calls, strict=True):
        assert receipt["model_id"] == body["model"]
        assert receipt["request_body"] == body
        assert receipt["endpoint"] == "http://localhost:8711/v1/chat/completions"
        assert receipt["outcome"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", [False, True])
async def test_direct_memory_requests_keep_admission_when_scope_omitted(presentation: bool) -> None:
    admissions: list[str] = []

    @asynccontextmanager
    async def admitted() -> AsyncIterator[None]:
        admissions.append("enter")
        yield
        admissions.append("exit")

    if presentation:
        await presentation_provider(Models(), model_admission=admitted).present(batch())
    else:
        await provider(Model(), model_admission=admitted).normalize(source())
    assert admissions == ["enter", "exit", "enter", "exit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", [False, True])
@pytest.mark.parametrize("failure", [ModelRequestBudgetUnavailable, ModelExecutionControlError])
async def test_goal_control_error_is_not_provider_error_and_sends_no_http(
    presentation: bool, failure: type[ModelExecutionControlError]
) -> None:
    error = failure("goal fence or budget denied")
    executor = RecordingExecutor(error)
    model = Models() if presentation else Model()
    with pytest.raises(failure) as caught:
        if presentation:
            await presentation_provider(model).present(batch(), model_executor=executor)
        else:
            await provider(model).normalize(source(), model_executor=executor)
    assert caught.value is error
    assert not model.calls
    assert len(executor.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", [False, True])
async def test_source_recheck_preserves_execution_control_errors(presentation: bool) -> None:
    error = ModelExecutionControlError("source fence")

    async def recheck(_: Any) -> bool:
        raise error

    with pytest.raises(ModelExecutionControlError) as caught:
        if presentation:
            await presentation_provider(Models()).present(batch(), recheck_sources=recheck)
        else:
            await provider(Model()).normalize(source(), recheck_source=recheck)
    assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", [False, True])
@pytest.mark.parametrize("response", ["envelope", "schema", "http"])
async def test_invalid_memory_response_is_rejected_inside_call_receipt(
    presentation: bool,
    response: str,
) -> None:
    executor = RecordingExecutor()

    def invalid(_: httpx.Request) -> httpx.Response:
        if response == "http":
            return httpx.Response(503)
        if response == "envelope":
            return httpx.Response(200, content=b'{"choices":[],"choices":[]}')
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"unexpected": true}'},
                    }
                ]
            },
        )

    with pytest.raises(MemoryNormalizationError):
        if presentation:
            await presentation_provider(invalid).present(batch(), model_executor=executor)
        else:
            await provider(invalid).normalize(source(), model_executor=executor)
    assert len(executor.calls) == 1
    assert executor.calls[0]["outcome"] == "MemoryNormalizationError"


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", [False, True])
async def test_cancelled_memory_http_does_not_become_provider_failure(presentation: bool) -> None:
    entered = asyncio.Event()
    executor = RecordingExecutor()

    async def waiting(_: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled HTTP should not finish")

    service = presentation_provider(waiting) if presentation else provider(waiting)
    operation = (
        service.present(batch(), model_executor=executor)
        if presentation
        else (service.normalize(source(), model_executor=executor))
    )
    task = asyncio.create_task(operation)
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(executor.calls) == 1
    assert executor.calls[0]["outcome"] == "CancelledError"


def embedding_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.embedding_service.httpx.AsyncClient",
        lambda **options: client_type(transport=httpx.MockTransport(handler), **options),
    )


@pytest.mark.asyncio
async def test_http_embedding_one_call_for_actual_batch_and_strict_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = RecordingExecutor()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0, 0.0]}, {"index": 1, "embedding": [0, 1]}]
            },
        )

    embedding_transport(monkeypatch, respond)
    result = await HttpEmbeddingService("http://localhost:8711/v1", "embed-local").embed(
        ["first", "second"],
        model_executor=executor,
    )
    assert result == [[1.0, 0.0], [0.0, 1.0]]
    assert len(requests) == len(executor.calls) == 1
    assert executor.calls[0] == {
        "role": "memory_embedder",
        "model_id": "embed-local",
        "endpoint": str(requests[0].url),
        "request_body": json.loads(requests[0].content),
        "outcome": "completed",
    }


@pytest.mark.asyncio
async def test_invalid_embedding_response_is_failed_inside_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = RecordingExecutor()
    embedding_transport(monkeypatch, lambda _: httpx.Response(200, json={"data": []}))
    with pytest.raises(EmbeddingServiceError):
        await HttpEmbeddingService("http://localhost:8711/v1", "embed-local").embed(
            ["one"],
            model_executor=executor,
        )
    assert executor.calls[0]["outcome"] == "EmbeddingServiceError"


@pytest.mark.asyncio
async def test_embedding_direct_request_keeps_prior_response_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [1, 0]}]})

    embedding_transport(monkeypatch, respond)
    assert await HttpEmbeddingService("http://localhost:8711/v1", "embed-local").embed(["one"]) == [
        [1.0, 0.0]
    ]
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {"model": "embed-local", "input": ["one"]}


@pytest.mark.asyncio
async def test_cancelled_embedding_http_remains_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = RecordingExecutor()
    entered = asyncio.Event()

    async def waiting(_: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled HTTP should not finish")

    embedding_transport(monkeypatch, waiting)
    task = asyncio.create_task(
        HttpEmbeddingService("http://localhost:8711/v1", "embed-local").embed(
            ["one"],
            model_executor=executor,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert executor.calls[0]["outcome"] == "CancelledError"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ModelRequestBudgetUnavailable, ModelExecutionControlError, asyncio.CancelledError]
)
async def test_embedding_control_and_cancellation_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    error = failure("control flow")
    executor = RecordingExecutor(error)

    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP without admission")

    embedding_transport(monkeypatch, forbidden)
    with pytest.raises(failure) as caught:
        await HttpEmbeddingService("http://localhost:8711/v1", "embed-local").embed(
            ["one"],
            model_executor=executor,
        )
    assert caught.value is error


@pytest.mark.asyncio
async def test_deterministic_embeddings_do_not_spend_http_credit() -> None:
    executor = RecordingExecutor(AssertionError("no model call"))
    service = DeterministicEmbeddingService()
    assert await service.embed(["one"], model_executor=executor) == await service.embed(["one"])
    assert not executor.calls
