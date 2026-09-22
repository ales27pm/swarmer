#!/usr/bin/env python3
"""Execute the fixed offline CRM contract against a snapshot in isolated Docker."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--image", required=True, help="Immutable local Docker image SHA256 ID")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--worker-root", type=Path, default=ROOT)
    args = parser.parse_args()
    # Only operator-selected trusted runtime source is imported on the host.
    # The model's snapshot is never imported or executed outside the container.
    sys.path.insert(0, str(args.worker_root.resolve(strict=True) / "workers/project-worker"))
    from project_contract import files_value, snapshot_sha
    from runtime import DockerRunner

    if args.output.exists():
        parser.error("output already exists; select a new evidence path")
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    files = files_value(snapshot["files"])
    contract = json.loads((ROOT / "evals/projects/crm/contract.json").read_text())
    source = (ROOT / "evals/projects/crm/acceptance.py").read_text(encoding="utf-8")
    # This reference contract permits only root CRM modules and the standard
    # library. Exclude model-written test/configuration/plugin files so they
    # cannot replace the acceptance suite or change its collection settings.
    selected = [file for file in files if re.fullmatch(r"crm(?:_[A-Za-z0-9_]+)?\.py", file["path"])]
    selected.append({"path": "tests/test_crm_acceptance.py", "content": source})
    receipt = DockerRunner(args.image).run(selected, "python", [], lambda: None)
    passed = (
        receipt["build_passed"] is True
        and receipt["tests_executed"] == contract["expected_acceptance_tests"]
        and receipt["test_failures"] == 0
        and bool(receipt["checks"])
        and all(
            check["status"] == "passed" and check["exit_code"] == 0 for check in receipt["checks"]
        )
    )
    report = {
        "contract": contract["id"],
        "passed": passed,
        "snapshot_sha256": snapshot_sha(files),
        "tested_sha256": snapshot_sha(selected),
        "acceptance_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "runtime_image": args.image,
        "selected_modules": [file["path"] for file in selected[:-1]],
        "expected_tests": contract["expected_acceptance_tests"],
        "receipt": receipt,
        "scope": contract["scope"],
    }
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("contract", "passed", "expected_tests", "selected_modules")
            }
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
