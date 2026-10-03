"""A provider response must retain the association with each query input."""

import json

import httpx
import pytest

from app.services.embedding_service import EmbeddingServiceError, HttpEmbeddingService


def transport(monkeypatch, data):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"data": data})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    return requests


async def test_reversed_embedding_response_preserves_input_identity(monkeypatch):
    requests = transport(
        monkeypatch,
        [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}],
    )
    provider = HttpEmbeddingService("http://fixture.invalid/v1", "fixture")
    assert await provider.embed(["requête originale", "English pivot"]) == [[1, 0], [0, 1]]
    assert requests == [{"model": "fixture", "input": ["requête originale", "English pivot"]}]


@pytest.mark.parametrize(
    "data",
    [
        [{"embedding": [1, 0]}, {"embedding": [0, 1]}],
        [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}],
        [{"index": True, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": "0", "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": -1, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [1, 0]}, {"index": 2, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [1, 0]}],
        [{"index": 0, "embedding": [True, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [0, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [1] * 8193}, {"index": 1, "embedding": [1] * 8193}],
    ],
)
async def test_invalid_batch_fails_inside_the_receipted_operation(monkeypatch, data):
    transport(monkeypatch, data)
    outcomes = []

    class Executor:
        async def execute(self, *, operation, **kwargs):
            try:
                result = await operation()
            except EmbeddingServiceError:
                outcomes.append("failed")
                raise
            outcomes.append("completed")
            return result

    provider = HttpEmbeddingService("http://fixture.invalid/v1", "fixture")
    with pytest.raises(EmbeddingServiceError) as caught:
        await provider.embed(["fr", "en"], model_executor=Executor())
    assert caught.value.request_outcome_known
    assert outcomes == ["failed"]


async def test_unindexed_singleton_is_unambiguous(monkeypatch):
    transport(monkeypatch, [{"embedding": [1, 0]}])
    assert await HttpEmbeddingService("http://fixture.invalid/v1", "fixture").embed(["one"]) == [
        [1, 0]
    ]
