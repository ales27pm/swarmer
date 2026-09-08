from __future__ import annotations

import hashlib
import math
from typing import Protocol

import httpx


class EmbeddingService(Protocol):
    provider_name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingServiceError(RuntimeError):
    pass


class DeterministicEmbeddingService:
    provider_name = "deterministic-test"

    def __init__(self, dimensions: int = 16) -> None:
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
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

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/embeddings", json={"model": self.model, "input": texts}
                )
                response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingServiceError("local embedding provider unavailable") from exc
        if not isinstance(body, dict):
            raise EmbeddingServiceError("invalid embedding response")
        data = body.get("data")
        if not isinstance(data, list):
            raise EmbeddingServiceError("invalid embedding response")
        vectors = [item.get("embedding") for item in data if isinstance(item, dict)]
        if len(vectors) != len(texts) or not all(
            isinstance(vector, list) and all(isinstance(value, (int, float)) for value in vector)
            for vector in vectors
        ):
            raise EmbeddingServiceError("invalid embedding vectors")
        return [
            [float(value) for value in vector] for vector in vectors if isinstance(vector, list)
        ]
