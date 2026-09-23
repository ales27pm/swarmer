#!/usr/bin/env python3
"""Run the fixed benign CRM workflow with local inference and isolated checks.

This is an operator-guided qualification, not an autonomous planner benchmark.
No generated source is executed on the host and no production API is mutated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_new(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def report_interruption(
    work: Path,
    index: int,
    last_result: str | None,
    stage: str,
    started: float,
    error: Exception,
    model_metrics: dict,
    transport_metrics: dict,
) -> int:
    interrupted = {
        "passed": False,
        "interrupted_iteration": index,
        "last_result": last_result,
        "stage": stage,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "stop_reason": str(error),
        "error_type": type(error).__name__,
        "model_metrics": model_metrics,
        "transport_metrics": transport_metrics,
        "guided": True,
        "production_mutations": False,
    }
    write_new(work / "summary.json", interrupted)
    print(json.dumps(interrupted), flush=True)
    return 2


def ensure_idle(database: Path | None) -> None:
    if database is None:
        return
    with sqlite3.connect(
        database.resolve(strict=True).as_uri() + "?mode=ro", uri=True
    ) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        active = sum(
            db.execute(query).fetchone()[0]
            for query in (
                "SELECT count(*) FROM agent_jobs WHERE status NOT IN ('completed','failed','cancelled')",
                "SELECT count(*) FROM goal_model_calls WHERE status='started'",
                "SELECT count(*) FROM goal_memory_queries WHERE status='started'",
                "SELECT count(*) FROM project_memory_queries WHERE status='started'",
            )
        )
    if active:
        raise RuntimeError(
            "Production work is active; qualification yields before further work"
        )


def next_instruction(state: dict, workflow: dict) -> str:
    paths = {item["path"] for item in state["files"]}
    missing = [item for item in workflow["steps"] if item["path"] not in paths]
    failed = [check for check in state["checks"] if check["status"] != "passed"]
    no_tests = failed and all(
        check["command"] == ["python", "-m", "pytest", "-q"]
        and check["exit_code"] == 5
        and "no tests ran" in check["output"]
        for check in failed
    )
    if failed and not no_tests:
        return (
            "Repair one actual failure from the current check receipts. Keep all CRM "
            "requirements, existing working behavior and the remaining module plan. "
            "If the generated test has a lifecycle or import error, correct that test; "
            "do not weaken its assertions or change a correct implementation to suit it. "
            "Return one small complete corrected file or one precise patch. Documentation "
            "and remaining features follow only after the failing checks pass."
        )
    if missing:
        return missing[0]["instruction"]
    return (
        "All planned modules exist. Check the full public CRM contract and the independent "
        "acceptance diagnostics. Correct one actual failing requirement while preserving "
        "all other behavior. No sending or network access. Return complete only when the "
        "requested features and all checks pass."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work", required=True, type=Path, help="New private evidence directory"
    )
    parser.add_argument("--worker-root", type=Path, default=ROOT)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--image", required=True)
    parser.add_argument("--max-iterations", type=int, default=16)
    parser.add_argument("--admission-db", type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_iterations <= 30:
        parser.error("max-iterations must be between 1 and 30")
    if args.work.exists():
        parser.error("work already exists; use a new path to preserve evidence")
    os.umask(0o077)
    ensure_idle(args.admission_db)
    worker_root = args.worker_root.resolve(strict=True)
    sys.path.insert(0, str(worker_root / "workers/project-worker"))
    import project_worker as worker
    from project_contract import snapshot_sha

    contract = json.loads((ROOT / "evals/projects/crm/contract.json").read_text())
    workflow = json.loads((ROOT / "evals/projects/crm/workflow.json").read_text())
    acceptance = (ROOT / "evals/projects/crm/acceptance.py").read_text()
    # Validate the configured endpoint before creating experiment evidence.
    generator = worker.ProjectGenerator(args.model_url, args.model, timeout_seconds=240)
    runner = worker.DockerRunner(args.image)
    args.work.mkdir(mode=0o700, parents=True)
    source_hashes = {
        str(path.relative_to(worker_root)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted((worker_root / "workers").rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    write_new(
        args.work / "manifest.json",
        {
            "contract": contract,
            "workflow": workflow,
            "model": args.model,
            "runtime_image": args.image,
            "max_iterations": args.max_iterations,
            "worker_sources": source_hashes,
            "acceptance_sha256": hashlib.sha256(acceptance.encode()).hexdigest(),
            "guided": True,
        },
    )
    state = {
        "objective": contract["objective"],
        "conversation": [{"role": "user", "content": contract["objective"]}],
        "files": [],
        "plan": [step["path"] for step in workflow["steps"]],
        "checks": [],
        "iteration": 1,
        "base_revision_id": None,
        "base_sha256": None,
        "focus_paths": [],
    }
    no_progress = 0
    accepted = False
    stop_reason = "iteration_budget_exhausted"
    for index in range(1, args.max_iterations + 1):
        started = time.monotonic()
        try:
            ensure_idle(args.admission_db)
            state["conversation"].append(
                {"role": "user", "content": next_instruction(state, workflow)}
            )
            write_new(args.work / f"input-{index}.json", state)
            result = worker.run_iteration(
                state, generator, runner, lambda: ensure_idle(args.admission_db)
            )
        except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
            return report_interruption(
                args.work,
                index,
                f"result-{index - 1}.json" if index > 1 else None,
                "generation",
                started,
                exc,
                generator.last_metrics,
                generator.last_transport_metrics,
            )
        changed = snapshot_sha(state["files"]) != snapshot_sha(result["files"])
        write_new(args.work / f"result-{index}.json", result)
        receipt = {
            "iteration": index,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "changed": changed,
            "action": result["action"],
            "message": result["message"],
            "files": [item["path"] for item in result["files"]],
            "checks": result["checks"],
            "model_metrics": generator.last_metrics,
            "transport_metrics": generator.last_transport_metrics,
        }
        write_new(args.work / f"receipt-{index}.json", receipt)
        print(
            json.dumps(
                {
                    k: receipt[k]
                    for k in (
                        "iteration",
                        "elapsed_seconds",
                        "changed",
                        "action",
                        "files",
                    )
                }
            ),
            flush=True,
        )
        # Reading must not hide a repeated no-edit loop.
        no_progress = 0 if changed else no_progress + 1
        state.update(
            files=result["files"],
            plan=result["plan"],
            checks=result["checks"],
            focus_paths=result["focus_paths"],
            iteration=index + 1,
            base_revision_id=f"crm_qualification_{index}",
            base_sha256=snapshot_sha(result["files"]),
        )
        state["conversation"].append(
            {"role": "assistant", "content": result["message"]}
        )
        paths = {item["path"] for item in result["files"]}
        if all(step["path"] in paths for step in workflow["steps"]) and all(
            check["status"] == "passed" for check in result["checks"]
        ):
            tested = [
                item
                for item in result["files"]
                if re.fullmatch(contract["allowed_module_pattern"], item["path"])
            ]
            tested.append(
                {"path": "tests/test_crm_acceptance.py", "content": acceptance}
            )
            try:
                evidence = runner.run(
                    tested, "python", [], lambda: ensure_idle(args.admission_db)
                )
            except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
                return report_interruption(
                    args.work,
                    index,
                    f"result-{index}.json",
                    "independent_acceptance",
                    started,
                    exc,
                    generator.last_metrics,
                    generator.last_transport_metrics,
                )
            accepted = (
                evidence["build_passed"] is True
                and evidence["tests_executed"] == contract["expected_acceptance_tests"]
                and evidence["test_failures"] == 0
                and bool(evidence["checks"])
                and all(
                    c["status"] == "passed" and c["exit_code"] == 0
                    for c in evidence["checks"]
                )
            )
            write_new(
                args.work / f"acceptance-{index}.json",
                {
                    "passed": accepted,
                    "snapshot_sha256": snapshot_sha(result["files"]),
                    "receipt": evidence,
                },
            )
            if accepted:
                stop_reason = "independent_acceptance_passed"
                break
            state["checks"] = evidence["checks"]
        if no_progress >= 3:
            stop_reason = "three_iterations_without_a_source_change"
            break
    summary = {
        "passed": accepted,
        "iterations": index,
        "last_result": f"result-{index}.json",
        "guided": True,
        "production_mutations": False,
        "stop_reason": stop_reason,
    }
    write_new(args.work / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
