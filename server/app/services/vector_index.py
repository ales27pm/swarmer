from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import shutil
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast
from uuid import uuid4

import aiosqlite

MAX_VECTOR_DIMENSIONS = 65_536
MAX_VECTOR_RESULTS = 1_000
MAX_VECTOR_DOCUMENTS = 1_000_000
MAX_POINTER_BYTES = 4_096
MAX_METADATA_BYTES = 64 * 1_024 * 1_024
MAX_INDEX_BYTES = 8 * 1_024 * 1_024 * 1_024
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
    if len(documents) > MAX_VECTOR_DOCUMENTS:
        raise VectorIndexError("vector document count is invalid")
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


def _current_uid() -> int:
    return os.geteuid()


def _require_private_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VectorIndexError(f"vector index {label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise VectorIndexError(f"vector index {label} is invalid")
    if metadata.st_uid != _current_uid():
        raise VectorIndexError(f"vector index {label} has an invalid owner")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise VectorIndexError(f"vector index {label} has unsafe permissions")
    return metadata


def _require_private_file(path: Path, *, max_bytes: int = MAX_INDEX_BYTES) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VectorIndexError("vector index file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise VectorIndexError("vector index file is invalid")
    if metadata.st_uid != _current_uid():
        raise VectorIndexError("vector index file has an invalid owner")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise VectorIndexError("vector index file has unsafe permissions")
    if metadata.st_nlink != 1:
        raise VectorIndexError("vector index file has an invalid link count")
    if metadata.st_size < 0 or metadata.st_size > max_bytes:
        raise VectorIndexError("vector index file is too large")
    return metadata


def _require_owned_private_cleanup_file(path: Path) -> os.stat_result:
    """Validate deletion confinement without treating file size as unsafe.

    Size limits protect reads and native loads. They must not make a private,
    owned crash artifact impossible to remove after a partial oversized write.
    """
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VectorIndexError("vector index cleanup file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise VectorIndexError("vector index cleanup file is invalid")
    if metadata.st_uid != _current_uid():
        raise VectorIndexError("vector index cleanup file has an invalid owner")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise VectorIndexError("vector index cleanup file has unsafe permissions")
    if metadata.st_nlink != 1:
        raise VectorIndexError("vector index cleanup file has an invalid link count")
    return metadata


def _read_private_bytes(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise VectorIndexError("vector index file is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        expected = _require_private_file(path, max_bytes=max_bytes)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise VectorIndexError("vector index file changed during validation")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1_048_576, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise VectorIndexError("vector index file is too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _copy_private_file(
    source_path: Path,
    destination_path: Path,
    *,
    max_bytes: int,
) -> tuple[int, str]:
    source_flags = os.O_RDONLY | os.O_CLOEXEC
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
        destination_flags |= os.O_NOFOLLOW
    try:
        source = os.open(source_path, source_flags)
    except OSError as exc:
        raise VectorIndexError("vector index file is unavailable") from exc
    destination: int | None = None
    try:
        opened = os.fstat(source)
        expected = _require_private_file(source_path, max_bytes=max_bytes)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise VectorIndexError("vector index file changed during validation")
        destination = os.open(destination_path, destination_flags, 0o600)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source, 1_048_576)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise VectorIndexError("vector index file is too large")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination, view)
                if written <= 0:
                    raise OSError("vector index copy did not make progress")
                view = view[written:]
        os.fchmod(destination, 0o600)
        os.fsync(destination)
        return total, digest.hexdigest()
    finally:
        os.close(source)
        if destination is not None:
            os.close(destination)


def _private_file_digest(path: Path, *, max_bytes: int) -> tuple[int, str]:
    temporary = path.parent / f".digest-{uuid4().hex}"
    try:
        return _copy_private_file(path, temporary, max_bytes=max_bytes)
    finally:
        if temporary.exists():
            _require_private_file(temporary, max_bytes=max_bytes)
            temporary.unlink()


def _write_private_bytes(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("vector index write did not make progress")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _projection_lock(root_path: Path) -> Iterator[None]:
    lock_path = root_path / ".LOCK"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != _current_uid()
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
        ):
            raise VectorIndexError("vector index lock has an invalid owner, mode, or type")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class FaissVectorIndex:
    """Optional local FAISS projection with crash-safe generation switching."""

    backend_name = "faiss"

    def __init__(self, root_path: Path, *, generations_to_keep: int = 2) -> None:
        if not 1 <= generations_to_keep <= 50:
            raise ValueError("vector index generation retention is invalid")
        self.root_path = _private_projection_root(root_path)
        self.generations_to_keep = generations_to_keep

    @property
    def _current_path(self) -> Path:
        return self.root_path / "CURRENT.json"

    def _ensure_private_root(self) -> None:
        if self.root_path.exists():
            _require_private_directory(self.root_path, label="root")
            return
        self.root_path.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.root_path.chmod(0o700)
        _require_private_directory(self.root_path, label="root")

    @staticmethod
    def _remove_owned_generation(path: Path) -> None:
        _require_private_directory(path, label="generation")
        for child in path.iterdir():
            _require_owned_private_cleanup_file(child)
        shutil.rmtree(path)

    def _cleanup_obsolete_generations(self, current_name: str) -> None:
        candidates: list[tuple[int, str, Path]] = []
        for path in self.root_path.iterdir():
            if not path.name.startswith("generation-"):
                continue
            metadata = _require_private_directory(path, label="generation")
            candidates.append((metadata.st_mtime_ns, path.name, path))
        ordered = sorted(candidates, key=lambda item: (-item[0], item[1]))
        keep = {current_name}
        for _, name, _ in ordered:
            if len(keep) >= self.generations_to_keep:
                break
            keep.add(name)
        for _, name, path in ordered:
            if name not in keep:
                self._remove_owned_generation(path)

    def _cleanup_orphaned_temporaries(self) -> None:
        """Remove only private, owned artifacts from interrupted rebuilds.

        The projection lock serializes rebuilds, so a matching temporary path
        cannot belong to a live rebuild while this method runs. Unknown paths
        and unsafe matching paths fail closed instead of being removed.
        """
        for path in self.root_path.iterdir():
            if path.name.startswith(".tmp-"):
                self._remove_owned_generation(path)
            elif path.name.startswith(".CURRENT-"):
                _require_owned_private_cleanup_file(path)
                path.unlink()
            elif path.name.startswith(".digest-") or (
                path.name.startswith(".read-") and path.name.endswith(".faiss")
            ):
                # Digest and native-load temporaries are complete copies of
                # index.faiss and can legitimately be far larger than the JSON
                # pointer.
                _require_owned_private_cleanup_file(path)
                path.unlink()

    @staticmethod
    def _metadata_bytes(
        *, memory_ids: list[str], dimensions: int | None, index_path: Path | None
    ) -> bytes:
        index_bytes = 0
        index_digest: str | None = None
        if index_path is not None:
            index_bytes, index_digest = _private_file_digest(
                index_path,
                max_bytes=MAX_INDEX_BYTES,
            )
        metadata = {
            "schema_version": VECTOR_INDEX_SCHEMA_VERSION,
            "dimensions": dimensions,
            "memory_ids": memory_ids,
            "index_bytes": index_bytes,
            "index_sha256": index_digest,
        }
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_METADATA_BYTES:
            raise VectorIndexError("vector index metadata is too large")
        return encoded

    async def rebuild(self, documents: Sequence[VectorDocument]) -> None:
        memory_ids, vectors, dimensions = _validated_documents(documents)
        faiss: _FaissModule | None = None
        numpy: _NumpyModule | None = None
        if dimensions is not None:
            faiss, numpy = _load_faiss_runtime()

        self._ensure_private_root()
        with _projection_lock(self.root_path):
            _require_private_directory(self.root_path, label="root")
            self._cleanup_orphaned_temporaries()
            nonce = uuid4().hex
            temporary = self.root_path / f".tmp-{nonce}"
            generation_name = f"generation-{nonce}"
            generation = self.root_path / generation_name
            pointer_temporary = self.root_path / f".CURRENT-{nonce}.json"
            generation_published = False
            temporary.mkdir(mode=0o700)
            temporary.chmod(0o700)
            try:
                index_path: Path | None = None
                if dimensions is not None and faiss is not None and numpy is not None:
                    matrix = numpy.asarray(vectors, dtype=numpy.float32)
                    faiss.normalize_L2(matrix)
                    index = faiss.IndexFlatIP(dimensions)
                    index.add(matrix)
                    index_path = temporary / "index.faiss"
                    faiss.write_index(index, str(index_path))
                    index_path.chmod(0o600)
                    _require_private_file(index_path)
                    with index_path.open("rb") as index_file:
                        os.fsync(index_file.fileno())
                _write_private_bytes(
                    temporary / "metadata.json",
                    self._metadata_bytes(
                        memory_ids=memory_ids,
                        dimensions=dimensions,
                        index_path=index_path,
                    ),
                )
                _fsync_directory(temporary)
                temporary.rename(generation)
                generation_published = True
                generation.chmod(0o700)
                _fsync_directory(self.root_path)
                _write_private_bytes(
                    pointer_temporary,
                    json.dumps(
                        {
                            "schema_version": VECTOR_INDEX_SCHEMA_VERSION,
                            "generation": generation_name,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
                os.replace(pointer_temporary, self._current_path)
                _fsync_directory(self.root_path)
                self._cleanup_obsolete_generations(generation_name)
                _fsync_directory(self.root_path)
            except Exception:
                if pointer_temporary.exists():
                    _require_private_file(pointer_temporary, max_bytes=MAX_POINTER_BYTES)
                    pointer_temporary.unlink()
                if temporary.exists():
                    self._remove_owned_generation(temporary)
                if generation_published and generation.exists():
                    current_name: str | None = None
                    if self._current_path.exists():
                        try:
                            pointer = json.loads(
                                _read_private_bytes(self._current_path, max_bytes=MAX_POINTER_BYTES)
                            )
                            if isinstance(pointer, dict):
                                current_name = pointer.get("generation")
                        except (TypeError, ValueError, VectorIndexError, json.JSONDecodeError):
                            current_name = None
                    if current_name != generation_name:
                        self._remove_owned_generation(generation)
                raise

    def _current_metadata(self) -> tuple[Path, list[str], int | None]:
        try:
            _require_private_directory(self.root_path, label="root")
            pointer = json.loads(
                _read_private_bytes(self._current_path, max_bytes=MAX_POINTER_BYTES)
            )
            if not isinstance(pointer, dict) or set(pointer) != {"schema_version", "generation"}:
                raise ValueError
            generation_name = pointer["generation"]
            if (
                pointer.get("schema_version") != VECTOR_INDEX_SCHEMA_VERSION
                or not isinstance(generation_name, str)
                or not generation_name.startswith("generation-")
                or Path(generation_name).name != generation_name
            ):
                raise ValueError
            generation = self.root_path / generation_name
            _require_private_directory(generation, label="generation")
            metadata_path = generation / "metadata.json"
            metadata = json.loads(_read_private_bytes(metadata_path, max_bytes=MAX_METADATA_BYTES))
            if not isinstance(metadata, dict) or set(metadata) != {
                "schema_version",
                "dimensions",
                "memory_ids",
                "index_bytes",
                "index_sha256",
            }:
                raise ValueError
            memory_ids = metadata["memory_ids"]
            dimensions = metadata["dimensions"]
            index_bytes = metadata["index_bytes"]
            index_digest = metadata["index_sha256"]
            if (
                metadata.get("schema_version") != VECTOR_INDEX_SCHEMA_VERSION
                or not isinstance(memory_ids, list)
                or len(memory_ids) > MAX_VECTOR_DOCUMENTS
                or len(set(memory_ids)) != len(memory_ids)
                or not all(
                    isinstance(value, str) and value and len(value) <= 300 and "\x00" not in value
                    for value in memory_ids
                )
                or dimensions is not None
                and (
                    not isinstance(dimensions, int)
                    or isinstance(dimensions, bool)
                    or not 1 <= dimensions <= MAX_VECTOR_DIMENSIONS
                )
                or not isinstance(index_bytes, int)
                or isinstance(index_bytes, bool)
                or not 0 <= index_bytes <= MAX_INDEX_BYTES
                or index_digest is not None
                and (
                    not isinstance(index_digest, str)
                    or len(index_digest) != 64
                    or any(character not in "0123456789abcdef" for character in index_digest)
                )
                or (dimensions is None) != (index_digest is None)
                or (dimensions is None) != (index_bytes == 0)
            ):
                raise ValueError
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise VectorIndexError("vector index metadata is invalid") from exc
        return generation, memory_ids, dimensions

    async def search(self, query: Sequence[float], limit: int = 20) -> list[VectorHit]:
        if limit < 1 or limit > MAX_VECTOR_RESULTS:
            raise VectorIndexError("search limit is invalid")
        self._ensure_private_root()
        with _projection_lock(self.root_path):
            generation, memory_ids, dimensions = self._current_metadata()
            if not memory_ids or dimensions is None:
                return []
            query_vector = _validated_vector(query, expected_dimensions=dimensions)
            faiss, numpy = _load_faiss_runtime()
            verified_copy = self.root_path / f".read-{uuid4().hex}.faiss"
            try:
                metadata = json.loads(
                    _read_private_bytes(generation / "metadata.json", max_bytes=MAX_METADATA_BYTES)
                )
                source_bytes, source_digest = _copy_private_file(
                    generation / "index.faiss",
                    verified_copy,
                    max_bytes=MAX_INDEX_BYTES,
                )
                if (
                    source_bytes != int(metadata["index_bytes"])
                    or source_digest != metadata["index_sha256"]
                ):
                    raise VectorIndexError("vector index contents failed integrity validation")
                index = faiss.read_index(str(verified_copy))
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
            finally:
                if verified_copy.exists():
                    _require_private_file(verified_copy)
                    verified_copy.unlink()
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
            if not self.root_path.exists():
                return VectorIndexHealth(
                    backend=self.backend_name,
                    available=True,
                    ready=False,
                    indexed_count=0,
                    dimensions=None,
                    detail="not_built_or_invalid",
                )
            self._ensure_private_root()
            with _projection_lock(self.root_path):
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

    def generation_age_seconds(self) -> float | None:
        """Return only a payload-free age for diagnostics."""

        if not self._current_path.exists():
            return None
        self._ensure_private_root()
        with _projection_lock(self.root_path):
            generation, _, _ = self._current_metadata()
            age = datetime.now(UTC).timestamp() - generation.stat().st_mtime
        return max(0.0, age)

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
