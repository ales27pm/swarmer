import httpx
import pytest

from app.services.embedding_service import EmbeddingServiceError, HttpEmbeddingService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "known"),
    [
        ("read_timeout", False),
        ("connection_reset", False),
        ("http_error", True),
        ("invalid_json", True),
        ("invalid_vector", True),
    ],
)
async def test_http_outcome_distinguishes_received_response_from_transport_loss(
    monkeypatch, outcome, known
):
    calls = []

    def respond(request):
        calls.append(request)
        if outcome == "read_timeout":
            raise httpx.ReadTimeout("fixture", request=request)
        if outcome == "connection_reset":
            raise httpx.RemoteProtocolError("fixture", request=request)
        if outcome == "http_error":
            return httpx.Response(503)
        if outcome == "invalid_json":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(200, json={"data": []})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(EmbeddingServiceError) as caught:
        await HttpEmbeddingService("http://fixture.invalid/v1", "test-model").embed(["one"])
    assert caught.value.request_outcome_known is known
    assert len(calls) == 1
