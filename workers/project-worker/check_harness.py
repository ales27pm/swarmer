"""Trusted runtime entry point; installed in the immutable runtime image."""

from __future__ import annotations

import json
import os
import py_compile
import re
import shutil

# Fixed commands execute only inside the isolated runtime.
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

PREFIX = "SWARMER_RUNNER_RECEIPT="


def prepare() -> None:
    # Only a private, read-only source snapshot enters this tmpfs workspace.
    shutil.copytree("/source", "/workspace/project")
    if Path("/dependencies/node_modules").is_dir():
        Path("/workspace/project/node_modules").symlink_to(
            "/dependencies/node_modules", target_is_directory=True
        )
    os.chdir("/workspace/project")
    sys.path.insert(0, "/dependencies/python")
    sys.path.insert(0, "/workspace/project")


def python_build() -> dict[str, int]:
    files = sorted(Path("/workspace/project").rglob("*.py"))
    with tempfile.TemporaryDirectory(prefix="compiled-") as compiled:
        for index, path in enumerate(files):
            py_compile.compile(str(path), cfile=str(Path(compiled) / f"{index}.pyc"), doraise=True)
    return {"exit_code": 0 if files else 1, "tests_executed": 0, "test_failures": 0}


def python_test() -> dict[str, int]:
    class Counter:
        executed = 0
        failures = 0

        def pytest_runtest_logreport(self, report: Any) -> None:
            if report.when == "call" and not report.skipped:
                self.executed += 1
            if report.failed:
                self.failures += 1

    counter = Counter()
    code = int(
        pytest.main(
            [
                "-q",
                "-p",
                "no:cacheprovider",
                "-c",
                "/opt/swarmer/pytest.ini",
                "--override-ini=addopts=",
                "/workspace/project",
            ],
            plugins=[counter],
        )
    )
    return {
        "exit_code": code if counter.executed else 5,
        "tests_executed": counter.executed,
        "test_failures": counter.failures,
    }


def node_test() -> dict[str, int]:
    process = subprocess.Popen(  # nosec B603
        ["/usr/local/bin/node", "--test", "--test-reporter=tap"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    tail = bytearray()
    if process.stdout is None:
        raise RuntimeError("Node runner did not create its output pipe")
    while chunk := process.stdout.read(4_096):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        tail.extend(chunk)
        if len(tail) > 16_000:
            del tail[:-16_000]
    code = process.wait()
    raw = tail.decode("utf-8", errors="replace")
    passed = re.findall(r"^# pass ([0-9]+)$", raw, re.MULTILINE)
    failed = re.findall(r"^# fail ([0-9]+)$", raw, re.MULTILINE)
    if not passed or not failed:
        return {"exit_code": code or 5, "tests_executed": 0, "test_failures": 0}
    failures = int(failed[-1])
    count = int(passed[-1]) + failures
    return {"exit_code": code if count else 5, "tests_executed": count, "test_failures": failures}


def main() -> None:
    prepare()
    mode = sys.argv[1]
    try:
        if mode == "python_build":
            result = python_build()
        elif mode == "python_test":
            result = python_test()
        elif mode == "node_build":
            package = json.loads(Path("package.json").read_text())
            if "build" not in package.get("scripts", {}):
                print("No npm build script is declared.")
                result = {"exit_code": 1, "tests_executed": 0, "test_failures": 0}
            else:
                code = subprocess.call(  # nosec B603
                    ["/usr/local/bin/npm", "run", "build", "--ignore-scripts"]
                )
                result = {"exit_code": code, "tests_executed": 0, "test_failures": 0}
        elif mode == "node_test":
            # The Node test runner owns the TAP summary. Project npm scripts are
            # never accepted as proof that tests were collected and executed.
            result = node_test()
        else:
            raise ValueError("unknown fixed check profile")
    except (OSError, ValueError, py_compile.PyCompileError, RuntimeError) as exc:
        print(f"Check failed: {type(exc).__name__}: {str(exc)[:1_000]}")
        result = {"exit_code": 1, "tests_executed": 0, "test_failures": 1}
    print("\n" + PREFIX + json.dumps(result, separators=(",", ":")), flush=True)
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()
