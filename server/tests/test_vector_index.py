from __future__ import annotations

import json
import math
import os
import stat
from errno import ENOSPC
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app.commands import rebuild_vector_index as rebuild_command
from app.services import vector_index as vector_module
from app.services.vector_index import (
    FaissVectorIndex,
    InMemoryVectorIndex,
    VectorDocument,
    VectorIndexError,
    VectorIndexUnavailable,
    load_sqlite_embedding_documents,
    rebuild_vector_index,
)


async def _create_memory_database(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            CREATE TABLE memory_items (id TEXT PRIMARY KEY, content TEXT NOT NULL);
            CREATE TABLE memory_embeddings (
                memory_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                vector_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(memory_id, provider)
            );
            """
        )
        await db.executemany(
            "INSERT INTO memory_items(id, content) VALUES(?, ?)",
            [("mem-a", "canoe lake"), ("mem-b", "ubuntu server")],
        )
        await db.executemany(
            """
            INSERT INTO memory_embeddings(
                memory_id, provider, dimensions, vector_json, updated_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            [
                ("mem-a", "test", 3, "[1.0, 0.0, 0.0]", "2026-01-01T00:00:00Z"),
                ("mem-b", "test", 3, "[0.0, 1.0, 0.0]", "2026-01-01T00:00:00Z"),
                # A secondary index must never manufacture memory for an orphaned vector.
                ("missing", "test", 3, "[0.0, 0.0, 1.0]", "2026-01-01T00:00:00Z"),
            ],
        )
        await db.commit()


@pytest.mark.asyncio
async def test_rebuild_uses_authoritative_memory_ids_and_is_deterministic(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await _create_memory_database(database)
    index = InMemoryVectorIndex()

    report = await rebuild_vector_index(db_path=database, provider="test", index=index)
    hits = await index.search([0.9, 0.1, 0.0])

    assert report.backend == "memory"
    assert report.indexed_count == 2
    assert report.dimensions == 3
    assert [hit.memory_id for hit in hits] == ["mem-a", "mem-b"]
    assert all(hit.memory_id != "missing" for hit in hits)


@pytest.mark.asyncio
async def test_missing_projection_does_not_change_authoritative_memory(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await _create_memory_database(database)
    index = InMemoryVectorIndex()
    await rebuild_vector_index(db_path=database, provider="test", index=index)

    # Losing a rebuildable projection does not delete or rewrite SQLite memory.
    await index.rebuild([])
    assert await index.search([1.0, 0.0, 0.0]) == []
    async with aiosqlite.connect(database) as db:
        count = await (await db.execute("SELECT COUNT(*) FROM memory_items")).fetchone()
    assert count == (2,)


@pytest.mark.asyncio
async def test_invalid_rebuild_is_atomic_and_preserves_previous_index() -> None:
    index = InMemoryVectorIndex()
    await index.rebuild([VectorDocument("valid", (1.0, 0.0))])

    with pytest.raises(VectorIndexError, match="dimensions"):
        await index.rebuild(
            [
                VectorDocument("new-a", (1.0, 0.0)),
                VectorDocument("new-b", (0.0, 1.0, 0.0)),
            ]
        )

    hits = await index.search([1.0, 0.0])
    assert [hit.memory_id for hit in hits] == ["valid"]


@pytest.mark.asyncio
async def test_corrupt_authoritative_embedding_fails_without_replacing_index(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await _create_memory_database(database)
    index = InMemoryVectorIndex()
    await index.rebuild([VectorDocument("existing", (1.0, 0.0, 0.0))])
    async with aiosqlite.connect(database) as db:
        await db.execute(
            "UPDATE memory_embeddings SET vector_json=? WHERE memory_id=?",
            ('[1.0, "secretly-not-a-number", 0.0]', "mem-a"),
        )
        await db.commit()

    with pytest.raises(VectorIndexError, match="invalid"):
        await rebuild_vector_index(db_path=database, provider="test", index=index)

    hits = await index.search([1.0, 0.0, 0.0])
    assert [hit.memory_id for hit in hits] == ["existing"]


@pytest.mark.asyncio
async def test_loader_rejects_missing_database_without_creating_it(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"
    with pytest.raises(VectorIndexError, match="unavailable"):
        await load_sqlite_embedding_documents(database, "test")
    assert not database.exists()


@pytest.mark.asyncio
async def test_vector_contract_rejects_nonfinite_values_and_unsafe_limits() -> None:
    index = InMemoryVectorIndex()
    with pytest.raises(VectorIndexError, match="invalid value"):
        await index.rebuild([VectorDocument("mem", (math.inf, 0.0))])
    await index.rebuild([VectorDocument("mem", (1.0, 0.0))])
    with pytest.raises(VectorIndexError, match="limit"):
        await index.search([1.0, 0.0], limit=0)


class _FakeArray:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def __getitem__(self, key: object) -> _FakeArray:
        assert isinstance(key, int)
        value = self.values[key]
        return _FakeArray(value if isinstance(value, list) else [value])

    def tolist(self) -> list[Any]:
        return self.values


class _FakeNumpy:
    float32 = object()

    def asarray(self, values: object, *, dtype: object) -> _FakeArray:
        assert dtype is self.float32
        assert isinstance(values, list)
        return _FakeArray(values)


class _FakeFaissIndex:
    def __init__(self, dimensions: int, vectors: list[list[float]] | None = None) -> None:
        self.d = dimensions
        self.vectors = vectors or []

    @property
    def ntotal(self) -> int:
        return len(self.vectors)

    def add(self, vectors: _FakeArray) -> None:
        self.vectors = vectors.tolist()

    def search(self, query: _FakeArray, limit: int) -> tuple[_FakeArray, _FakeArray]:
        query_values = query.tolist()[0]
        scores = [
            sum(a * b for a, b in zip(query_values, row, strict=True)) for row in self.vectors
        ]
        positions = sorted(range(len(scores)), key=lambda position: (-scores[position], position))[
            :limit
        ]
        return (
            _FakeArray([[scores[position] for position in positions]]),
            _FakeArray([positions]),
        )


class _FakeFaiss:
    def __init__(self) -> None:
        self.read_calls = 0

    def IndexFlatIP(self, dimensions: int) -> _FakeFaissIndex:
        return _FakeFaissIndex(dimensions)

    def normalize_L2(self, vectors: _FakeArray) -> None:
        normalized: list[list[float]] = []
        for row in vectors.tolist():
            norm = math.sqrt(sum(value * value for value in row)) or 1.0
            normalized.append([value / norm for value in row])
        vectors.values = normalized

    def write_index(self, index: _FakeFaissIndex, path: str) -> None:
        Path(path).write_text(
            json.dumps({"dimensions": index.d, "vectors": index.vectors}), encoding="utf-8"
        )

    def read_index(self, path: str) -> _FakeFaissIndex:
        self.read_calls += 1
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return _FakeFaissIndex(value["dimensions"], value["vectors"])


@pytest.mark.asyncio
async def test_faiss_adapter_is_rebuildable_without_an_external_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    index = FaissVectorIndex(tmp_path / "vector-projection")

    await index.rebuild(
        [
            VectorDocument("mem-a", (1.0, 0.0)),
            VectorDocument("mem-b", (0.0, 1.0)),
        ]
    )
    hits = await index.search([1.0, 0.0])
    health = await index.health()

    assert [hit.memory_id for hit in hits] == ["mem-a", "mem-b"]
    assert health.ready is True
    assert health.indexed_count == 2
    assert health.dimensions == 2
    assert (tmp_path / "vector-projection" / "CURRENT.json").is_file()


@pytest.mark.asyncio
async def test_faiss_optional_runtime_failure_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable() -> tuple[object, object]:
        raise VectorIndexUnavailable("runtime unavailable")

    monkeypatch.setattr(vector_module, "_load_faiss_runtime", unavailable)
    index = FaissVectorIndex(tmp_path / "projection")

    with pytest.raises(VectorIndexUnavailable):
        await index.rebuild([VectorDocument("mem-a", (1.0, 0.0))])
    health = await index.health()

    assert health.available is False
    assert health.ready is False
    assert not (tmp_path / "projection" / "CURRENT.json").exists()


@pytest.mark.asyncio
async def test_faiss_projection_rejects_symlink_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    outside = tmp_path / "outside"
    outside.mkdir()
    projection = tmp_path / "projection"
    projection.symlink_to(outside, target_is_directory=True)

    with pytest.raises(VectorIndexError, match="root"):
        await FaissVectorIndex(projection).rebuild([VectorDocument("mem-a", (1.0, 0.0))])
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_faiss_projection_files_are_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    projection = tmp_path / "projection"

    await FaissVectorIndex(projection).rebuild([VectorDocument("mem-a", (1.0, 0.0))])

    assert stat.S_IMODE(projection.stat().st_mode) == 0o700
    for path in projection.rglob("*"):
        if path.is_dir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        else:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_faiss_rejects_group_or_world_access_before_native_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    projection = tmp_path / "projection"
    index = FaissVectorIndex(projection)
    await index.rebuild([VectorDocument("mem-a", (1.0, 0.0))])
    pointer = json.loads((projection / "CURRENT.json").read_text(encoding="utf-8"))
    index_path = projection / pointer["generation"] / "index.faiss"
    index_path.chmod(0o640)

    with pytest.raises(VectorIndexError, match="permissions"):
        await index.search([1.0, 0.0])
    assert fake_faiss.read_calls == 0


@pytest.mark.asyncio
async def test_faiss_rejects_projection_owned_by_another_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projection = tmp_path / "projection"
    projection.mkdir(mode=0o700)
    monkeypatch.setattr(vector_module, "_current_uid", lambda: os.geteuid() + 1)

    with pytest.raises(VectorIndexError, match="owner"):
        await FaissVectorIndex(projection).rebuild([])


@pytest.mark.asyncio
async def test_faiss_generation_retention_keeps_current_and_one_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    projection = tmp_path / "projection"
    index = FaissVectorIndex(projection, generations_to_keep=2)

    for number in range(4):
        await index.rebuild([VectorDocument(f"mem-{number}", (1.0, 0.0))])

    generations = sorted(path.name for path in projection.glob("generation-*"))
    assert len(generations) == 2
    pointer = json.loads((projection / "CURRENT.json").read_text(encoding="utf-8"))
    assert pointer["generation"] in generations
    assert not list(projection.glob(".tmp-*"))
    assert not list(projection.glob(".CURRENT-*"))


@pytest.mark.asyncio
async def test_faiss_rebuild_removes_private_orphans_from_crashed_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    projection = tmp_path / "projection"
    index = FaissVectorIndex(projection)
    await index.rebuild([VectorDocument("old", (1.0, 0.0))])

    orphan = projection / ".tmp-crashed"
    orphan.mkdir(mode=0o700)
    (orphan / "index.faiss").write_bytes(b"orphan")
    (orphan / "index.faiss").chmod(0o600)
    pointer_orphan = projection / ".CURRENT-crashed.json"
    pointer_orphan.write_text("{}", encoding="utf-8")
    pointer_orphan.chmod(0o600)
    digest_orphan = projection / ".digest-crashed"
    digest_orphan.write_bytes(b"x" * 5_000)
    digest_orphan.chmod(0o600)
    read_orphan = projection / ".read-crashed.faiss"
    read_orphan.write_bytes(b"x" * 5_000)
    read_orphan.chmod(0o600)

    await index.rebuild([VectorDocument("new", (0.0, 1.0))])

    assert not orphan.exists()
    assert not pointer_orphan.exists()
    assert not digest_orphan.exists()
    assert not read_orphan.exists()
    assert [hit.memory_id for hit in await index.search([0.0, 1.0])] == ["new"]


@pytest.mark.asyncio
async def test_faiss_rebuild_refuses_unsafe_crash_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    projection = tmp_path / "projection"
    index = FaissVectorIndex(projection)
    await index.rebuild([VectorDocument("old", (1.0, 0.0))])
    outside = tmp_path / "outside"
    outside.mkdir()
    unsafe = projection / ".tmp-crashed"
    unsafe.symlink_to(outside, target_is_directory=True)

    with pytest.raises(VectorIndexError, match="generation is invalid"):
        await index.rebuild([VectorDocument("new", (0.0, 1.0))])

    assert unsafe.is_symlink()
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_faiss_rebuild_removes_oversized_sparse_crash_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    projection = tmp_path / "projection"
    index = FaissVectorIndex(projection)
    await index.rebuild([VectorDocument("old", (1.0, 0.0))])
    orphan = projection / ".tmp-crashed"
    orphan.mkdir(mode=0o700)
    oversized = orphan / "index.faiss"
    with oversized.open("wb") as output:
        output.truncate(vector_module.MAX_INDEX_BYTES + 1)
    oversized.chmod(0o600)

    await index.rebuild([])

    assert not orphan.exists()
    assert (await index.health()).ready is True


@pytest.mark.asyncio
async def test_faiss_pointer_swap_failure_preserves_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    projection = tmp_path / "projection"
    index = FaissVectorIndex(projection)
    await index.rebuild([VectorDocument("old", (1.0, 0.0))])
    old_pointer = (projection / "CURRENT.json").read_bytes()
    real_replace = vector_module.os.replace

    def fail_pointer_swap(source: object, destination: object) -> None:
        if Path(destination).name == "CURRENT.json":
            raise OSError("simulated disk failure")
        real_replace(source, destination)

    monkeypatch.setattr(vector_module.os, "replace", fail_pointer_swap)
    with pytest.raises(OSError, match="simulated disk failure"):
        await index.rebuild([VectorDocument("new", (0.0, 1.0))])

    assert (projection / "CURRENT.json").read_bytes() == old_pointer
    assert not list(projection.glob(".tmp-*"))
    assert not list(projection.glob(".CURRENT-*"))
    assert len(list(projection.glob("generation-*"))) == 1


@pytest.mark.asyncio
async def test_faiss_disk_space_failure_preserves_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    projection = tmp_path / "projection"
    index = FaissVectorIndex(projection)
    await index.rebuild([VectorDocument("old", (1.0, 0.0))])
    old_pointer = (projection / "CURRENT.json").read_bytes()
    real_write = vector_module._write_private_bytes

    def fail_metadata_write(path: Path, value: bytes) -> None:
        if path.name == "metadata.json":
            raise OSError(ENOSPC, "simulated disk full")
        real_write(path, value)

    monkeypatch.setattr(vector_module, "_write_private_bytes", fail_metadata_write)
    with pytest.raises(OSError, match="simulated disk full"):
        await index.rebuild([VectorDocument("new", (0.0, 1.0))])

    assert (projection / "CURRENT.json").read_bytes() == old_pointer
    assert not list(projection.glob(".tmp-*"))
    assert not list(projection.glob(".CURRENT-*"))
    assert len(list(projection.glob("generation-*"))) == 1


@pytest.mark.asyncio
async def test_faiss_health_does_not_create_an_unbuilt_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_faiss = _FakeFaiss()
    fake_numpy = _FakeNumpy()
    monkeypatch.setattr(vector_module, "_load_faiss_runtime", lambda: (fake_faiss, fake_numpy))
    projection = tmp_path / "missing-projection"

    health = await FaissVectorIndex(projection).health()

    assert health.ready is False
    assert health.detail == "not_built_or_invalid"
    assert not projection.exists()


def test_rebuild_command_reports_safe_success_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def successful(_: object) -> dict[str, object]:
        return {
            "backend": "faiss",
            "provider": "test",
            "indexed_count": 2,
            "dimensions": 3,
        }

    monkeypatch.setattr(rebuild_command, "_run", successful)
    result = rebuild_command.main(
        ["--db", "state.db", "--provider", "test", "--index-path", "projection"]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "backend": "faiss",
        "dimensions": 3,
        "indexed_count": 2,
        "provider": "test",
        "status": "completed",
    }


def test_rebuild_command_redacts_failure_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failed(_: object) -> dict[str, object]:
        raise VectorIndexError("/protected/path and provider response")

    monkeypatch.setattr(rebuild_command, "_run", failed)
    result = rebuild_command.main(
        [
            "--db",
            "/protected/path/state.db",
            "--provider",
            "private-provider",
            "--index-path",
            "/protected/path/index",
        ]
    )

    output = capsys.readouterr().out
    assert result == 1
    assert json.loads(output) == {"status": "failed", "error": "vector index rebuild failed"}
    assert "/protected" not in output
    assert "private-provider" not in output
