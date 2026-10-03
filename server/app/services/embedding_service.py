from __future__ import annotations

import hashlib
import math
from typing import Protocol

import httpx

from app.services.memory_vectors import MAX_MEMORY_DIMENSIONS
from app.services.model_request_execution import ModelExecutionControlError, ModelRequestExecutor


class EmbeddingService(Protocol):
    provider_name: str

    async def embed(
        self, texts: list[str], *, model_executor: ModelRequestExecutor | None = None
    ) -> list[list[float]]: ...


class EmbeddingServiceError(RuntimeError):
    def __init__(self, message: str, *, request_outcome_known: bool = True) -> None:
        super().__init__(message)
        # A transport interruption does not prove that the remote request stopped.
        self.request_outcome_known = request_outcome_known


class DeterministicEmbeddingService:
    provider_name = "deterministic-test"

    def __init__(self, dimensions: int = 16) -> None:
        self.dimensions = dimensions

    async def embed(
        self, texts: list[str], *, model_executor: ModelRequestExecutor | None = None
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            values = [0.0] * self.dimensions
            for token in text.casefold().split():
                digest = hashlib.sha256(token.encode()).digest()
                values[int.from_bytes(digest[:2], "big") % self.dimensions] += 1.0
            norm = math.sqrt(sum(value * value for value in values)) or 1.0
            vectors.append([value / norm for value in values])
        return vectors


class HttpEmbeddingService:
    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider_name = f"http:{model}"

    async def embed(
        self, texts: list[str], *, model_executor: ModelRequestExecutor | None = None
    ) -> list[list[float]]:
        endpoint = f"{self.base_url}/embeddings"
        model = self.model
        request_body = {"model": model, "input": list(texts)}
        input_count = len(request_body["input"])

        async def request() -> list[list[float]]:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(endpoint, json=request_body)
                    response.raise_for_status()
                body = response.json()
            except ModelExecutionControlError:
                raise
            except httpx.RequestError as exc:
                raise EmbeddingServiceError(
                    "local embedding provider outcome unknown", request_outcome_known=False
                ) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise EmbeddingServiceError("local embedding provider unavailable") from exc
            if not isinstance(body, dict):
                raise EmbeddingServiceError("invalid embedding response")
            data = body.get("data")
            if not isinstance(data, list) or len(data) != input_count:
                raise EmbeddingServiceError("invalid embedding response")
            # Batch responses identify their inputs by index, not array order.
            # A singleton needs no positional inference; retain that legacy
            # response shape while refusing ambiguous unindexed batches.
            ordered: dict[int, list[float]] = {}
            for item in data:
                if not isinstance(item, dict):
                    raise EmbeddingServiceError("invalid embedding response")
                index = item.get("index", 0 if input_count == 1 else None)
                if type(index) is not int or not 0 <= index < input_count or index in ordered:
                    raise EmbeddingServiceError("invalid embedding response indices")
                vector = item.get("embedding")
                if (
                    not isinstance(vector, list)
                    or not 1 <= len(vector) <= MAX_MEMORY_DIMENSIONS
                    or not all(type(value) in (int, float) for value in vector)
                ):
                    raise EmbeddingServiceError("invalid embedding vectors")
                try:
                    converted = [float(value) for value in vector]
                except (ValueError, OverflowError) as exc:
                    raise EmbeddingServiceError("invalid embedding vectors") from exc
                if not all(math.isfinite(value) for value in converted) or not any(converted):
                    raise EmbeddingServiceError("invalid embedding vectors")
                ordered[index] = converted
            if len({len(vector) for vector in ordered.values()}) > 1:
                raise EmbeddingServiceError("inconsistent embedding dimensions")
            return [ordered[index] for index in range(input_count)]

        if model_executor is not None:
            return await model_executor.execute(
                role="memory_embedder",
                model_id=model,
                endpoint=endpoint,
                request_body=request_body,
                operation=request,
            )
        return await request()
