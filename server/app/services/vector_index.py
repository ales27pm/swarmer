from __future__ import annotations

import json
import math
import os
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast
from uuid import uuid4

import aiosqlite

MAX_VECTOR_DIMENSIONS = 65_536
MAX_VECTOR_RESULTS = 1_000
VECTOR_INDEX_SCHEMA_VERSION = "1"


class VectorIndexError(RuntimeError):
    """Raised when a rebuildable secondary vector index cannot be used safely."""


class VectorIndexUnavailable(VectorIndexError):
    """Raised when an optional vector-index runtime is not installed."""


@dataclass(frozen=True, slots=True)
class VectorDocument:
    memory_id: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class VectorHit:
    memory_id: str
    score: float


@dataclass(frozen=True, slots=True)
class VectorIndexHealth:
    backend: str
    available: bool
    ready: bool
    indexed_count: int
    dimensions: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class VectorRebuildReport:
    backend: str
    provider: str
    indexed_count: int
    dimensions: int | None


class VectorIndex(Protocol):
    """Rebuildable search projection; never the memory source of truth."""

    backend_name: str

    async def rebuild(self, documents: Sequence[VectorDocument]) -> None: ...

    async def search(self, query: Sequence[float], limit: int = 20) -> list[VectorHit]: ...

    async def health(self) -> VectorIndexHealth: ...

    async def close(self) -> None: ...


def _validated_vector(values: Sequence[float], *, expected_dimensions: int | None) -> list[float]:
    if not values or len(values) > MAX_VECTOR_DIMENSIONS:
        raise VectorIndexError("vector dimensions are invalid")
    if expected_dimensions is not None and len(values) != expected_dimensions:
        raise VectorIndexError("vector dimensions do not match the index")
    normalized: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise VectorIndexError("vector contains an invalid value")
        try:
            item = float(value)
        except (TypeError, ValueError) as exc:
            raise VectorIndexError("vector contains an invalid value") from exc
        if not math.isfinite(item):
            raise VectorIndexError("vector contains an invalid value")
        normalized.append(item)
    return normalized


def _validated_documents(
    documents: Sequence[VectorDocument],
) -> tuple[list[str], list[list[float]], int | None]:
    memory_ids: list[str] = []
    vectors: list[list[float]] = []
    seen: set[str] = set()
    dimensions: int | None = None
    for document in documents:
        memory_id = document.memory_id.strip()
        if not memory_id or len(memory_id) > 300 or "\x00" in memory_id:
            raise VectorIndexError("memory id is invalid")
        if memory_id in seen:
            raise VectorIndexError("memory id is duplicated")
        vector = _validated_vector(document.vector, expected_dimensions=dimensions)
        if dimensions is None:
            dimensions = len(vector)
        seen.add(memory_id)
        memory_ids.append(memory_id)
        vectors.append(vector)
    return memory_ids, vectors, dimensions


class InMemoryVectorIndex:
    """Deterministic adapter for tests and small trusted control-plane projections."""

    backend_name = "memory"

    def __init__(self) -> None:
        self._documents: dict[str, tuple[float, ...]] = {}
        self._dimensions: int | None = None

    async def rebuild(self, documents: Sequence[VectorDocument]) -> None:
        memory_ids, vectors, dimensions = _validated_documents(documents)
        replacement = {
            memory_id: tuple(vector) for memory_id, vector in zip(memory_ids, vectors, strict=True)
        }
        # Swap only after every record has passed validation.
        self._documents = replacement
        self._dimensions = dimensions

    async def search(self, query: Sequence[float], limit: int = 20) -> list[VectorHit]:
        if limit < 1 or limit > MAX_VECTOR_RESULTS:
            raise VectorIndexError("search limit is invalid")
        if not self._documents:
            return []
        query_vector = _validated_vector(query, expected_dimensions=self._dimensions)
        query_norm = math.sqrt(sum(value * value for value in query_vector))
        if query_norm == 0:
            return []
        hits: list[VectorHit] = []
        for memory_id, vector in self._documents.items():
            vector_norm = math.sqrt(sum(value * value for value in vector))
            if vector_norm == 0:
                score = 0.0
            else:
                score = sum(
                    query_value * candidate
                    for query_value, candidate in zip(query_vector, vector, strict=True)
                ) / (query_norm * vector_norm)
            hits.append(VectorHit(memory_id=memory_id, score=score))
        return sorted(hits, key=lambda hit: (-hit.score, hit.memory_id))[:limit]

    async def health(self) -> VectorIndexHealth:
        return VectorIndexHealth(
            backend=self.backend_name,
            available=True,
            ready=True,
            indexed_count=len(self._documents),
            dimensions=self._dimensions,
            detail="ready",
        )

    async def close(self) -> None:
        return None


class _Array(Protocol):
    def __getitem__(self, key: object) -> _Array: ...

    def tolist(self) -> list[Any]: ...


class _FaissIndex(Protocol):
    d: int
    ntotal: int

    def add(self, vectors: _Array) -> None: ...

    def search(self, query: _Array, limit: int) -> tuple[_Array, _Array]: ...


class _FaissModule(Protocol):
    def IndexFlatIP(self, dimensions: int) -> _FaissIndex: ...

    def normalize_L2(self, vectors: _Array) -> None: ...

    def write_index(self, index: _FaissIndex, path: str) -> None: ...

    def read_index(self, path: str) -> _FaissIndex: ...


class _NumpyModule(Protocol):
    float32: object

    def asarray(self, values: object, *, dtype: object) -> _Array: ...


def _load_faiss_runtime() -> tuple[_FaissModule, _NumpyModule]:
    try:
        faiss_module: ModuleType = import_module("faiss")
        numpy_module: ModuleType = import_module("numpy")
    except ImportError as exc:
        raise VectorIndexUnavailable("optional FAISS vector runtime is unavailable") from exc
    return cast(_FaissModule, faiss_module), cast(_NumpyModule, numpy_module)


def _private_projection_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise VectorIndexError("vector index root is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise VectorIndexError("vector index root contains a symbolic link")
    return absolute


def _require_private_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VectorIndexError("vector index file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise VectorIndexError("vector index file is invalid")


class FaissVectorIndex:
    """Optional local FAISS projection with crash-safe generation switching."""

    backend_name = "faiss"

    def __init__(self, root_path: Path) -> None:
        self.root_path = _private_projection_root(root_path)

    @property
    def _current_path(self) -> Path:
        return self.root_path / "CURRENT.json"

    async def rebuild(self, documents: Sequence[VectorDocument]) -> None:
        memory_ids, vectors, dimensions = _validated_documents(documents)
        faiss: _FaissModule | None = None
        numpy: _NumpyModule | None = None
        if dimensions is not None:
            faiss, numpy = _load_faiss_runtime()

        if self.root_path.exists() and not self.root_path.is_dir():
            raise VectorIndexError("vector index root is invalid")
        self.root_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root_path.chmod(0o700)
        nonce = uuid4().hex
        temporary = self.root_path / f".tmp-{nonce}"
        generation_name = f"generation-{nonce}"
        generation = self.root_path / generation_name
        temporary.mkdir(mode=0o700)
        try:
            if dimensions is not None and faiss is not None and numpy is not None:
                matrix = numpy.asarray(vectors, dtype=numpy.float32)
                faiss.normalize_L2(matrix)
                index = faiss.IndexFlatIP(dimensions)
                index.add(matrix)
                faiss.write_index(index, str(temporary / "index.faiss"))
                (temporary / "index.faiss").chmod(0o600)
            metadata = {
                "schema_version": VECTOR_INDEX_SCHEMA_VERSION,
                "dimensions": dimensions,
                "memory_ids": memory_ids,
            }
            (temporary / "metadata.json").write_text(
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            (temporary / "metadata.json").chmod(0o600)
            temporary.rename(generation)
            generation.chmod(0o700)
            pointer_temporary = self.root_path / f".CURRENT-{nonce}.json"
            pointer_temporary.write_text(
                json.dumps(
                    {"schema_version": VECTOR_INDEX_SCHEMA_VERSION, "generation": generation_name},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            pointer_temporary.chmod(0o600)
            os.replace(pointer_temporary, self._current_path)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def _current_metadata(self) -> tuple[Path, list[str], int | None]:
        try:
            _require_private_file(self._current_path)
            pointer = json.loads(self._current_path.read_text(encoding="utf-8"))
            generation_name = pointer["generation"]
            if (
                pointer.get("schema_version") != VECTOR_INDEX_SCHEMA_VERSION
                or not isinstance(generation_name, str)
                or not generation_name.startswith("generation-")
                or Path(generation_name).name != generation_name
            ):
                raise ValueError
            generation = self.root_path / generation_name
            generation_metadata = generation.lstat()
            if stat.S_ISLNK(generation_metadata.st_mode) or not stat.S_ISDIR(
                generation_metadata.st_mode
            ):
                raise ValueError
            metadata_path = generation / "metadata.json"
            _require_private_file(metadata_path)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            memory_ids = metadata["memory_ids"]
            dimensions = metadata["dimensions"]
            if (
                metadata.get("schema_version") != VECTOR_INDEX_SCHEMA_VERSION
                or not isinstance(memory_ids, list)
                or not all(isinstance(value, str) and value for value in memory_ids)
                or dimensions is not None
                and (not isinstance(dimensions, int) or dimensions < 1)
            ):
                raise ValueError
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VectorIndexError("vector index metadata is invalid") from exc
        return generation, memory_ids, dimensions

    async def search(self, query: Sequence[float], limit: int = 20) -> list[VectorHit]:
        if limit < 1 or limit > MAX_VECTOR_RESULTS:
            raise VectorIndexError("search limit is invalid")
        generation, memory_ids, dimensions = self._current_metadata()
        if not memory_ids or dimensions is None:
            return []
        query_vector = _validated_vector(query, expected_dimensions=dimensions)
        faiss, numpy = _load_faiss_runtime()
        try:
            index_path = generation / "index.faiss"
            _require_private_file(index_path)
            index = faiss.read_index(str(index_path))
            if index.d != dimensions or index.ntotal != len(memory_ids):
                raise VectorIndexError("vector index contents are inconsistent")
            matrix = numpy.asarray([query_vector], dtype=numpy.float32)
            faiss.normalize_L2(matrix)
            distances, positions = index.search(matrix, min(limit, len(memory_ids)))
            distance_values = cast(list[float], distances[0].tolist())
            position_values = cast(list[int], positions[0].tolist())
        except VectorIndexError:
            raise
        except Exception as exc:
            raise VectorIndexError("vector index search failed") from exc
        hits: list[VectorHit] = []
        for position, score in zip(position_values, distance_values, strict=True):
            if 0 <= position < len(memory_ids):
                hits.append(VectorHit(memory_id=memory_ids[position], score=float(score)))
        return hits

    async def health(self) -> VectorIndexHealth:
        try:
            _load_faiss_runtime()
        except VectorIndexUnavailable:
            return VectorIndexHealth(
                backend=self.backend_name,
                available=False,
                ready=False,
                indexed_count=0,
                dimensions=None,
                detail="runtime_unavailable",
            )
        try:
            _, memory_ids, dimensions = self._current_metadata()
        except VectorIndexError:
            return VectorIndexHealth(
                backend=self.backend_name,
                available=True,
                ready=False,
                indexed_count=0,
                dimensions=None,
                detail="not_built_or_invalid",
            )
        return VectorIndexHealth(
            backend=self.backend_name,
            available=True,
            ready=True,
            indexed_count=len(memory_ids),
            dimensions=dimensions,
            detail="ready",
        )

    async def close(self) -> None:
        return None


async def load_sqlite_embedding_documents(db_path: Path, provider: str) -> list[VectorDocument]:
    """Load a rebuild snapshot without mutating authoritative SQLite memory."""

    if not provider.strip() or len(provider) > 300:
        raise VectorIndexError("embedding provider is invalid")
    if not db_path.is_file():
        raise VectorIndexError("authoritative memory database is unavailable")
    try:
        async with aiosqlite.connect(db_path) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT embeddings.memory_id, embeddings.dimensions, embeddings.vector_json
                    FROM memory_embeddings AS embeddings
                    INNER JOIN memory_items AS memory ON memory.id = embeddings.memory_id
                    WHERE embeddings.provider = ?
                    ORDER BY embeddings.memory_id ASC
                    """,
                    (provider,),
                )
            ).fetchall()
    except aiosqlite.Error as exc:
        raise VectorIndexError("authoritative embedding snapshot is unavailable") from exc

    documents: list[VectorDocument] = []
    for memory_id, dimensions, encoded in rows:
        try:
            decoded = json.loads(str(encoded))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VectorIndexError("authoritative embedding row is invalid") from exc
        if not isinstance(decoded, list) or not isinstance(dimensions, int):
            raise VectorIndexError("authoritative embedding row is invalid")
        vector = _validated_vector(decoded, expected_dimensions=dimensions)
        documents.append(VectorDocument(memory_id=str(memory_id), vector=tuple(vector)))
    # Validate the entire snapshot before a caller replaces a live projection.
    _validated_documents(documents)
    return documents


async def rebuild_vector_index(
    *, db_path: Path, provider: str, index: VectorIndex
) -> VectorRebuildReport:
    documents = await load_sqlite_embedding_documents(db_path, provider)
    await index.rebuild(documents)
    health = await index.health()
    if not health.ready:
        raise VectorIndexError("rebuilt vector index did not become ready")
    return VectorRebuildReport(
        backend=index.backend_name,
        provider=provider,
        indexed_count=len(documents),
        dimensions=health.dimensions,
    )
