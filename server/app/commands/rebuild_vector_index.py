from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path

from app.services.vector_index import FaissVectorIndex, VectorIndexError, rebuild_vector_index


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild a secondary FAISS projection from authoritative SQLite embeddings."
    )
    parser.add_argument("--db", required=True, type=Path, help="Authoritative SQLite database")
    parser.add_argument("--provider", required=True, help="Embedding provider name")
    parser.add_argument("--index-path", required=True, type=Path, help="FAISS projection directory")
    parser.add_argument(
        "--generations-to-keep",
        type=int,
        default=int(os.environ.get("MONGARS_VECTOR_INDEX_GENERATIONS_TO_KEEP", "2")),
        help="Number of private FAISS generations retained after a successful rebuild",
    )
    return parser


async def _run(arguments: argparse.Namespace) -> dict[str, object]:
    index = FaissVectorIndex(
        arguments.index_path,
        generations_to_keep=arguments.generations_to_keep,
    )
    try:
        report = await rebuild_vector_index(
            db_path=arguments.db,
            provider=arguments.provider,
            index=index,
        )
    finally:
        await index.close()
    return {
        "backend": report.backend,
        "provider": report.provider,
        "indexed_count": report.indexed_count,
        "dimensions": report.dimensions,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = asyncio.run(_run(arguments))
    except VectorIndexError:
        # Keep paths, provider responses, and index contents out of operator logs.
        print(json.dumps({"status": "failed", "error": "vector index rebuild failed"}))
        return 1
    print(json.dumps({"status": "completed", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
