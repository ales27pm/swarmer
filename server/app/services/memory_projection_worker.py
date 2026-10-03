"""Bounded consumer for durable, revision-fenced memory indexing intents.

Each current native/canonical view is projected independently. The index view
also maintains the canonical/legacy compatibility cache; there is no external sink.
Calls are explicit (write path or operator drain), never a startup model load.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import aiosqlite

from app.services.direct_model_admission import LocalGPUUnavailable
from app.services.embedding_service import EmbeddingService, EmbeddingServiceError
from app.services.memory_text_views import (
    ProjectionClaim,
    claim_projection_batch,
    fail_projection_claim,
    finish_projection_locked,
    mark_projection_dispatched,
    mark_projection_response_received,
    read_projection_source,
)
from app.services.memory_vectors import embedding_identity, memory_vector
from app.services.model_request_execution import ModelExecutionControlError


@dataclass
class ProjectionDrainReport:
    claimed: int = 0
    projected: int = 0
    deleted: int = 0
    deferred: int = 0
    stale: int = 0
    failed: int = 0
    unknown: int = 0


@asynccontextmanager
async def _uncoordinated_test_slot() -> AsyncIterator[None]:
    yield


class MemoryProjectionWorker:
    """One lease per view; never reserve a batch while its first model call runs."""

    def __init__(
        self,
        db_path: Path,
        get_provider: Callable[[], EmbeddingService | None],
        get_model_revision: Callable[[], str | None],
        *,
        model_admission: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        self.db_path = db_path
        self.get_provider = get_provider
        self.get_model_revision = get_model_revision
        self.model_admission = model_admission or _uncoordinated_test_slot

    def _current(self, provider: EmbeddingService, identity: str) -> bool:
        return provider is self.get_provider() and identity == embedding_identity(
            self.get_provider(), self.get_model_revision()
        )

    async def _finish(
        self,
        claim: ProjectionClaim,
        vector: list[float] | None,
        provider: EmbeddingService | None,
        identity: str | None,
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if provider is not None and not self._current(provider, str(identity)):
                return False
            applied = await finish_projection_locked(db, claim, vector=vector)
            if provider is not None and not self._current(provider, str(identity)):
                await db.rollback()
                return False
            await db.commit()
            return applied

    async def drain(
        self, *, limit: int = 1, memory_id: str | None = None, revision: int | None = None
    ) -> ProjectionDrainReport:
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("projection batch must contain 1 to 16 items")
        report = ProjectionDrainReport()
        owner = "memory-projector-" + uuid4().hex
        for _ in range(limit):
            provider = self.get_provider()
            identity = (
                embedding_identity(provider, self.get_model_revision())
                if provider is not None
                else None
            )
            claims = await claim_projection_batch(
                self.db_path,
                owner,
                limit=1,
                lease_seconds=60,
                memory_id=memory_id,
                revision=revision,
                provider=identity,
            )
            if not claims:
                break
            claim = claims[0]
            report.claimed += 1
            if claim.operation == "delete":
                if await self._finish(claim, None, None, None):
                    report.deleted += 1
                else:
                    report.stale += 1
                continue
            if provider is None:
                await fail_projection_claim(
                    self.db_path, claim, error_category="provider_unavailable"
                )
                report.deferred += 1
                continue
            try:
                async with self.model_admission():
                    source = await read_projection_source(self.db_path, claim)
                    if source is None:
                        report.stale += 1
                        continue
                    if not self._current(provider, str(identity)):
                        await fail_projection_claim(
                            self.db_path, claim, error_category="provider_changed"
                        )
                        report.deferred += 1
                        continue
                    if not await mark_projection_dispatched(self.db_path, claim):
                        report.deferred += 1
                        continue
                    if not self._current(provider, str(identity)):
                        # Configuration changed while the marker was committed;
                        # no request was sent, so this outcome is known.
                        await mark_projection_response_received(self.db_path, claim)
                        await fail_projection_claim(
                            self.db_path, claim, error_category="provider_changed"
                        )
                        report.deferred += 1
                        continue
                    vectors = await provider.embed([f"{source.content} {source.summary or ''}"])
                    if not await mark_projection_response_received(self.db_path, claim):
                        report.stale += 1
                        continue
                    vector = (
                        memory_vector(vectors[0], getattr(provider, "dimensions", None))
                        if len(vectors) == 1
                        else None
                    )
                    if vector is None:
                        await fail_projection_claim(
                            self.db_path, claim, error_category="invalid_vector"
                        )
                        report.failed += 1
                    elif not self._current(provider, str(identity)):
                        await fail_projection_claim(
                            self.db_path, claim, error_category="provider_changed"
                        )
                        report.deferred += 1
                    elif await self._finish(claim, vector, provider, identity):
                        report.projected += 1
                    else:
                        report.stale += 1
            except LocalGPUUnavailable:
                await fail_projection_claim(
                    self.db_path, claim, error_category="provider_unavailable"
                )
                report.deferred += 1
            except ModelExecutionControlError:
                # A budget/revision/cancellation boundary must not turn into a
                # successful empty index operation or another provider request.
                raise
            except EmbeddingServiceError as exc:
                if exc.request_outcome_known:
                    await mark_projection_response_received(self.db_path, claim)
                    await fail_projection_claim(
                        self.db_path, claim, error_category="provider_unavailable"
                    )
                else:
                    report.unknown += 1
                report.failed += 1
            # Cancellation/process death after dispatch retains durable in_flight.
            # Lease expiry is not proof of remote completion and cannot retry it.
        return report
