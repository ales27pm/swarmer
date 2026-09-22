#!/usr/bin/env python3
"""Deterministic context projection checks, NOT an LLM quality benchmark.

Uses the actual ProjectContextService against disposable SQLite fixtures.
Optional model plugin requires BOTH --model-plugin and --allow-model-calls.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "server") not in sys.path:
    sys.path.insert(0, str(ROOT / "server"))
from app.services.project_context import (
    ProjectContextConflict,
    ProjectContextService,
)

MODES = (
    "current_reduction",
    "observation_masking",
    "structured_summary",
    "summary_hybrid",
)
DEFAULT_FIXTURES = ROOT / "evals/context/scenarios.json"


def serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def token_estimate(value: Any) -> int:
    """UTF-8 bytes / 4 rounded up, explicitly NOT a model tokenizer."""
    return math.ceil(len(serialize(value).encode("utf8")) / 4)


def load_scenarios(path: Path = DEFAULT_FIXTURES) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    scenarios = data["scenarios"]
    if len(scenarios) != 50 or len({s["id"] for s in scenarios}) != 50:
        raise ValueError("expected exactly 50 unique scenarios")
    if any(s["language"] not in {"fr", "en"} for s in scenarios):
        raise ValueError("unknown fixture language")
    return scenarios


def _database(path: Path, case: dict[str, Any]) -> None:
    """Minimal fixture schema for the actual service; no production database is opened."""
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE goal_runs(id TEXT PRIMARY KEY,objective TEXT,conversation_revision INTEGER);
            CREATE TABLE goal_project_links(goal_run_id TEXT,project_id TEXT);
            CREATE TABLE goal_conversation_links(goal_run_id TEXT,conversation_id TEXT);
            CREATE TABLE goal_messages(id TEXT PRIMARY KEY,conversation_id TEXT,goal_run_id TEXT,role TEXT,content TEXT,created_at TEXT);
            CREATE TABLE project_revisions(id TEXT,project_id TEXT,revision INTEGER,sha256 TEXT,snapshot_json TEXT);
            CREATE TABLE project_context_snapshots(project_id TEXT,version INTEGER,goal_run_id TEXT,fingerprint TEXT,state_json TEXT,created_at TEXT);
        """)
        for project in ("own", "foreign"):
            db.execute("INSERT INTO goal_runs VALUES (?,?,?)", (project, case["objective"], 1))
            db.execute("INSERT INTO goal_project_links VALUES (?,?)", (project, project))
            db.execute("INSERT INTO goal_conversation_links VALUES (?,?)", (project, project))
        for i, message in enumerate(case["messages"]):
            project = message.get("project", "own")
            db.execute(
                "INSERT INTO goal_messages VALUES (?,?,?,?,?,?)",
                (
                    message["id"],
                    project,
                    project,
                    message["role"],
                    message["content"],
                    f"2026-09-22T00:{i % 60:02d}:00Z",
                ),
            )
        checks = case.get("verified_checks", [])
        if checks:
            db.execute(
                "INSERT INTO project_revisions VALUES (?,?,?,?,?)",
                (
                    "accepted-revision",
                    "own",
                    1,
                    "fixture-source-hash",
                    serialize({"checks": checks, "plan": []}),
                ),
            )


def _lexical_sources(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Transparent fallback only: no semantic embeddings are simulated or claimed."""
    terms = set(re.findall(r"[^\W_]{3,}", case["query"].casefold()))
    ranked = []
    for index, m in enumerate(case["messages"]):
        if m.get("project", "own") != "own":
            continue
        words = set(re.findall(r"[^\W_]{3,}", m["content"].casefold()))
        score = len(terms & words)
        if score:
            ranked.append(
                (
                    score,
                    index,
                    {"source_id": m["id"], "role": m["role"], "text": m["content"]},
                )
            )
    return [item for _, _, item in sorted(ranked, key=lambda row: (-row[0], -row[1]))[:4]]


def project(case: dict[str, Any], state: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError("invalid mode")
    messages = [dict(m) for m in case["messages"] if m.get("project", "own") == "own"]
    budget = case["budget_tokens"]
    protected = {
        "objective": state["objective"],
        "verified_results": state["verified_results"],
    }
    retrieval_mode = "not_requested"
    if mode in {"structured_summary", "summary_hybrid"}:
        protected["project_state"] = ProjectContextService.prompt_state(state)
    if mode == "observation_masking":
        tool_indices = [i for i, m in enumerate(messages) if m["role"] == "tool"]
        for i in tool_indices[:-1]:
            messages[i]["content"] = (
                f"[Observation available from original source {messages[i]['id']}]"
            )
    if mode == "summary_hybrid":
        retrieval_mode = "lexical_fallback_no_embedding_provider"
        protected["retrieved_sources"] = _lexical_sources(case)
    # Critical structured state is never silently dropped to make a dispatch fit.
    if token_estimate(protected) > budget:
        return {
            "payload": protected,
            "dispatchable": False,
            "overflow": True,
            "retrieval_mode": retrieval_mode,
            "selected_source_ids": [],
            "reason": "protected_state_exceeds_budget",
        }
    selected = []
    for message in reversed(messages):
        candidate = {
            "source_id": message["id"],
            "role": message["role"],
            "content": message["content"],
        }
        # Reference baseline's per-message/history reduction, not an exact production replay.
        if mode == "current_reduction":
            candidate["content"] = candidate["content"][-8000:]
        if token_estimate({**protected, "messages": [candidate, *selected]}) <= budget:
            selected.insert(0, candidate)
        elif mode == "current_reduction":
            # Baseline is a contiguous recent window, not relevance selection.
            break
    payload = {**protected, "messages": selected}
    return {
        "payload": payload,
        "dispatchable": True,
        "overflow": False,
        "retrieval_mode": retrieval_mode,
        "selected_source_ids": [m["source_id"] for m in selected],
        "reason": "bounded_projection",
    }


async def evaluate_case(
    case: dict[str, Any],
    directory: Path,
    model_plugin: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    path = directory / f"{case['id']}.sqlite"
    _database(path, case)
    service = ProjectContextService(path)
    started = time.perf_counter()
    state = await service.refresh("own")
    first_ms = (time.perf_counter() - started) * 1000
    restarted = await ProjectContextService(path).refresh("own")
    original_user = [
        m for m in case["messages"] if m.get("project", "own") == "own" and m["role"] == "user"
    ]
    recovered = [await service.source("own", m["id"]) for m in original_user]
    isolation = True
    for m in case["messages"]:
        if m.get("project") == "foreign":
            try:
                await service.source("own", m["id"])
            except ProjectContextConflict:
                continue
            isolation = False
    with sqlite3.connect(path) as db:
        snapshot_count = db.execute("SELECT count(*) FROM project_context_snapshots").fetchone()[0]
    with sqlite3.connect(path) as db:
        originals_unchanged = all(
            db.execute("SELECT content FROM goal_messages WHERE id=?", (message["id"],)).fetchone()[
                0
            ]
            == message["content"]
            for message in case["messages"]
        )
    modes = {}
    for mode in MODES:
        before = time.perf_counter()
        projection = project(case, state, mode)
        elapsed = (time.perf_counter() - before) * 1000
        payload_text = serialize(projection["payload"])
        requirements = case["expected"]["critical_fragments"]
        preserved = [fragment for fragment in requirements if fragment in payload_text]
        forbidden = case["expected"].get("foreign_fragments", [])
        expected_checks = case.get("verified_checks", [])
        checked = [
            {k: v for k, v in item.items() if k != "source_id"}
            for item in projection["payload"]["verified_results"]
        ]
        sources = list(
            dict.fromkeys(
                [
                    *projection["selected_source_ids"],
                    *[r["source_id"] for r in projection["payload"].get("retrieved_sources", [])],
                ]
            )
        )
        result = {
            "dispatchable": projection["dispatchable"],
            "reason": projection["reason"],
            "estimated_tokens": token_estimate(projection["payload"]),
            "budget_tokens": case["budget_tokens"],
            "projection_latency_ms": round(elapsed, 3),
            "critical_total": len(requirements),
            "critical_preserved": len(preserved),
            "missing_critical_fragments": [x for x in requirements if x not in preserved],
            "cross_project_leak": any(x in payload_text for x in forbidden),
            "verified_receipts_unchanged": checked == expected_checks,
            "selected_source_ids": sources,
            "retrieval_mode": projection["retrieval_mode"],
            "model_called": False,
        }
        if model_plugin is not None and projection["dispatchable"]:
            model_started = time.perf_counter()
            output = model_plugin(
                {
                    "scenario_id": case["id"],
                    "language": case["language"],
                    "query": case["query"],
                    "mode": mode,
                    "context": projection["payload"],
                }
            )
            if not isinstance(output, dict) or not isinstance(output.get("answer"), str):
                raise ValueError("model plugin must return an object with answer:string")
            allowed = {x.get("source_id") for x in state["verified_results"]}
            claims = output.get("claimed_verified_source_ids", [])
            result["model_called"] = True
            result["model"] = {
                "latency_ms": round((time.perf_counter() - model_started) * 1000, 3),
                "answer": output["answer"],
                "unbacked_receipt_claims": [x for x in claims if x not in allowed],
                "quality_grade": None,
                "quality_grade_reason": "Requires independent human or calibrated judge review",
            }
        modes[mode] = result
    return {
        "id": case["id"],
        "language": case["language"],
        "category": case["category"],
        "snapshot_refresh_ms": round(first_ms, 3),
        "restart_identical": restarted == state,
        "snapshot_count_after_replay": snapshot_count,
        "cross_project_source_denied": isolation,
        "original_user_sources_recoverable": len(recovered) == len(original_user)
        and all(
            found["content"] == " ".join(original["content"].split())
            for found, original in zip(recovered, original_user, strict=True)
        ),
        "original_source_count": len(case["messages"]),
        "persisted_originals_unchanged": originals_unchanged,
        "modes": modes,
    }


async def evaluate(
    scenarios: list[dict[str, Any]], *, model_plugin: Callable | None = None
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mongars-context-eval-") as temp:
        cases = [await evaluate_case(case, Path(temp), model_plugin) for case in scenarios]
    totals = {}
    for mode in MODES:
        rows = [case["modes"][mode] for case in cases]
        latencies = sorted(row["projection_latency_ms"] for row in rows)
        totals[mode] = {
            "cases": len(rows),
            "dispatchable": sum(r["dispatchable"] for r in rows),
            "critical_preserved": sum(r["critical_preserved"] for r in rows),
            "critical_total": sum(r["critical_total"] for r in rows),
            "dispatchable_critical_preserved": sum(
                r["critical_preserved"] for r in rows if r["dispatchable"]
            ),
            "dispatchable_critical_total": sum(
                r["critical_total"] for r in rows if r["dispatchable"]
            ),
            "cross_project_leaks": sum(r["cross_project_leak"] for r in rows),
            "receipt_projection_failures": sum(not r["verified_receipts_unchanged"] for r in rows),
            "estimated_tokens_total": sum(r["estimated_tokens"] for r in rows),
            "projection_latency_p50_ms": statistics.median(latencies),
            "projection_latency_p95_ms": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)],
            "model_calls": sum(r["model_called"] for r in rows),
        }
    return {
        "schema_version": "1.0",
        "kind": "deterministic_projection_checks",
        "methodology": {
            "token_count": "ceil(serialized UTF-8 bytes / 4); no model tokenizer",
            "summary": "Actual ProjectContextService source-backed extract; not generative compaction",
            "baseline": "Bounded chronological projection with tail8000 per message; reference only, not exact runtime replay",
            "hybrid": "Lexical fallback only; real embedding quality not measured",
            "latency": "Local SQLite snapshot/projection latency; excludes any uncalled model",
            "duplicate_effects": "No external effects executed; snapshot replay only. CRM atomic replay tested separately in CRM integration suite.",
            "quality": "No inferred model quality gain, semantic accuracy, energy or device-memory measurement",
        },
        "model_calls_enabled": model_plugin is not None,
        "scenario_count": len(cases),
        "summary": totals,
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--model-plugin",
        help="Trusted installed Python module:function; may call configured models",
    )
    parser.add_argument("--allow-model-calls", action="store_true")
    args = parser.parse_args()
    if bool(args.model_plugin) != args.allow_model_calls:
        parser.error("Model calls require both --model-plugin and --allow-model-calls")
    plugin = None
    if args.model_plugin:
        module, name = args.model_plugin.rsplit(":", 1)
        plugin = getattr(importlib.import_module(module), name)
        if not callable(plugin):
            parser.error("Model plugin is not callable")
    result = asyncio.run(evaluate(load_scenarios(args.fixtures), model_plugin=plugin))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
