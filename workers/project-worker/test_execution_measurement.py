from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import check_harness
import pytest
import runtime
from project_execution import (
    bind_execution_receipt,
    execution_receipt_value,
    observation_value,
)


@pytest.mark.parametrize("mutate,fail", [(False, False), (True, False), (False, True)])
def test_real_offline_node_harness_measures_actual_copied_workspace(
    tmp_path, mutate, fail
):
    source, dependencies, workspace = (
        tmp_path / name for name in ("source", "deps", "copy")
    )
    source.mkdir()
    dependencies.mkdir()
    text = "const t=require('node:test');const a=require('node:assert/strict');t('sum',()=>a.equal(2+2,4));\n"
    if fail:
        text = text.replace("a.equal(2+2,4)", "a.equal(2+2,5)")
    if mutate:
        text += "require('node:fs').appendFileSync(__filename,'\\n//changed\\n');\n"
    (source / "sum.test.js").write_text(text)
    code = """
import sys, pathlib, shutil, os
sys.path.insert(0, sys.argv[1])
import check_harness as h
h.SOURCE_ROOT, h.DEPENDENCY_ROOT, h.PROJECT_ROOT = map(pathlib.Path, sys.argv[2:5])
def prepare():
    shutil.copytree(h.SOURCE_ROOT, h.PROJECT_ROOT)
    os.chdir(h.PROJECT_ROOT)
h.prepare = prepare
sys.argv = ['check_harness.py', 'node_test']
h.main()
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            "-c",
            code,
            str(Path(runtime.__file__).parent),
            str(source),
            str(dependencies),
            str(workspace),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
    )
    assert completed.returncode == int(fail), completed.stderr
    lines = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith(runtime.RECEIPT_PREFIX)
    ]
    result = json.loads(lines[-1][len(runtime.RECEIPT_PREFIX) :])
    assert result["tests_executed"] == 1 and result["test_failures"] == int(fail)
    expected = hashlib.sha256(
        json.dumps(
            [{"path": "sum.test.js", "content": text}],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    observation = result["observation"]
    assert observation["source_before_sha256"] == expected
    assert (observation["source_after_sha256"] == expected) is not mutate
    assert (
        observation["workspace_before_sha256"] == observation["workspace_after_sha256"]
    ) is not mutate
    assert observation["source_unchanged"] is not mutate
    assert observation["environment_unchanged"] is True
    assert observation["errors"] == []
    assert observation_value(observation) == observation


def test_real_runner_marks_legacy_harness_measurement_incomplete(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(runtime.os, "getuid", lambda: 1000)
    runner = runtime.DockerRunner("sha256:" + "a" * 64)

    def process(command, directory, timeout, active):
        active()
        return (
            0,
            runtime.RECEIPT_PREFIX
            + json.dumps(
                {
                    "exit_code": 0,
                    "tests_executed": 1 if command[-1].endswith("test") else 0,
                    "test_failures": 0,
                }
            ),
            1,
        )

    monkeypatch.setattr(runner, "_process", process)
    monkeypatch.setattr(runner, "_cleanup", lambda *args: None)
    result = runner.run(
        [{"path": "a.py", "content": "x=1\n"}], "python", [], lambda: None
    )
    assert result["execution_receipt"]["observation_status"] == "incomplete"
    assert "legacy_harness" in result["execution_receipt"]["incomplete_reasons"]
    assert result["execution_receipt"]["origin"] == "worker_reported_measurement"


def measured_result(monkeypatch, files=None):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(runtime.os, "getuid", lambda: 1000)
    files = files or [{"path": "a.py", "content": "x=1\n"}]
    source_sha = hashlib.sha256(
        json.dumps(
            sorted(files, key=lambda f: f["path"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    observation = {
        "schema_version": "project-check-observation-v1",
        "source_before_sha256": source_sha,
        "source_after_sha256": source_sha,
        "workspace_before_sha256": "b" * 64,
        "workspace_after_sha256": "b" * 64,
        "dependency_before_sha256": "c" * 64,
        "dependency_after_sha256": "c" * 64,
        "harness_sha256": "d" * 64,
        "source_unchanged": True,
        "environment_unchanged": True,
        "errors": [],
    }
    runner = runtime.DockerRunner("sha256:" + "a" * 64)

    def process(command, directory, timeout, active):
        active()
        return (
            0,
            runtime.RECEIPT_PREFIX
            + json.dumps(
                {
                    "exit_code": 0,
                    "tests_executed": int(command[-1].endswith("test")),
                    "test_failures": 0,
                    "observation": observation,
                }
            ),
            3,
        )

    monkeypatch.setattr(runner, "_process", process)
    monkeypatch.setattr(runner, "_cleanup", lambda *args: None)
    return runner.run(files, "python", [], lambda: None)


def test_runner_binds_all_profiles_counts_and_source_without_raw_logs(monkeypatch):
    result = measured_result(monkeypatch)
    value = result["execution_receipt"]
    assert value["observation_status"] == "complete"
    assert value["incomplete_reasons"] == []
    assert [p["tests_executed"] for p in value["profiles"]] == [0, 1]
    assert value["origin"] == "worker_reported_measurement"
    assert len(json.dumps(value).encode()) < 16_384
    assert "output" not in json.dumps(value)
    assert (
        bind_execution_receipt(
            value,
            source_sha256=value["source_sha256"],
            runtime="python",
            checks=result["checks"],
        )
        == value
    )
    assert (
        measured_result(monkeypatch)["execution_receipt"]["run_id"] != value["run_id"]
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(origin="trusted"),
        lambda r: r.update(run_id="model-supplied"),
        lambda r: r.update(extra="not allowed"),
        lambda r: r.update(profiles_expected=["python_test", "python_build"]),
        lambda r: r["profiles"][1].update(tests_executed=True),
        lambda r: r["profiles"][1].update(tests_executed=0),
        lambda r: r["profiles"][1].update(test_failures=1),
        lambda r: r["profiles"][1].update(check_index=0),
        lambda r: r["profiles"][0]["observation"].update(source_unchanged=False),
        lambda r: r["profiles"][0]["observation"].update(dependency_before_sha256=None),
        lambda r: r["profiles"][0]["observation"].update(
            errors=["environment_unbound"]
        ),
        lambda r: r.update(profiles=r["profiles"][:1]),
        lambda r: r.update(incomplete_reasons=["environment_unbound"]),
    ],
)
def test_measurement_rejects_false_completeness_and_unbound_fields(monkeypatch, change):
    value = measured_result(monkeypatch)["execution_receipt"]
    change(value)
    with pytest.raises(ValueError):
        execution_receipt_value(value)


def test_receipt_cannot_bind_to_other_snapshot_or_checks(monkeypatch):
    result = measured_result(monkeypatch)
    receipt = result["execution_receipt"]
    with pytest.raises(ValueError):
        bind_execution_receipt(
            receipt, source_sha256="f" * 64, runtime="python", checks=result["checks"]
        )
    result["checks"][1]["exit_code"] = 1
    with pytest.raises(ValueError):
        bind_execution_receipt(
            receipt,
            source_sha256=receipt["source_sha256"],
            runtime="python",
            checks=result["checks"],
        )


def test_environment_measures_bytes_and_refuses_escaping_links(tmp_path, monkeypatch):
    dependencies = tmp_path / "deps"
    dependencies.mkdir()
    package = dependencies / "package.py"
    package.write_text("version='1'\nvalue=1\n")
    first = check_harness._measurement_tree(dependencies, dependencies=True)
    package.write_text("version='1'\nvalue=2\n")
    assert check_harness._measurement_tree(dependencies, dependencies=True) != first
    (dependencies / "foreign").symlink_to(tmp_path / "never-read-secret")
    with pytest.raises(ValueError, match="contained"):
        check_harness._measurement_tree(dependencies, dependencies=True)
    monkeypatch.setattr(check_harness, "DEPENDENCY_ROOT", dependencies)
    monkeypatch.setattr(check_harness, "PROJECT_ROOT", tmp_path / "absent")
    before = check_harness._measurement_capture(None)
    observation = check_harness._measurement_finish(before, before, "a" * 64)
    assert observation["dependency_before_sha256"] is None
    assert "environment_unbound" in observation["errors"]
    assert observation_value(observation) == observation


@pytest.mark.parametrize(
    "limit", ["MEASUREMENT_MAX_FILES", "MEASUREMENT_MAX_BYTES", "MEASUREMENT_SECONDS"]
)
def test_measurement_is_bounded_and_does_not_change_files(tmp_path, monkeypatch, limit):
    file = tmp_path / "data"
    file.write_bytes(b"abc")
    monkeypatch.setattr(check_harness, limit, 0)
    with pytest.raises(ValueError, match="bound"):
        check_harness._measurement_tree(tmp_path)
    assert file.read_bytes() == b"abc"


def test_copied_source_identity_refuses_symlink_ancestors(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "a.py").write_text("secret-not-read")
    source = tmp_path / "source"
    source.mkdir()
    (source / "linked").symlink_to(private, target_is_directory=True)
    with pytest.raises(OSError):
        check_harness._measurement_source(source, ("linked/a.py",))


def test_worker_transports_only_fresh_runner_receipt_never_model_fields(monkeypatch):
    import project_worker as worker
    from project_contract import ProjectError
    from test_project_worker import Generator, payload, step

    class MeasuredRunner:
        def run(self, files, *args):
            return measured_result(monkeypatch, files)

    result = worker.run_iteration(
        payload(), Generator(step()), MeasuredRunner(), lambda: None
    )
    assert result["execution_receipt"]["observation_status"] == "complete"
    assert result["execution_receipt"]["origin"] == "worker_reported_measurement"
    assert "execution_receipt" not in json.dumps(worker.STEP_SCHEMA)
    with pytest.raises(ProjectError):
        worker.run_iteration(
            payload(),
            Generator(step(execution_receipt=result["execution_receipt"])),
            MeasuredRunner(),
            lambda: None,
        )


def test_read_only_iteration_does_not_relabel_historical_checks_as_fresh(monkeypatch):
    import project_worker as worker
    from project_contract import snapshot_sha
    from test_project_worker import Generator, payload, step

    files = [{"path": "app.py", "content": "x=1\n"}]
    old = measured_result(monkeypatch, files)
    data = {
        **payload(),
        "files": files,
        "checks": old["checks"],
        "base_revision_id": "revision_one",
        "base_sha256": snapshot_sha(files),
    }

    class NoExecution:
        def run(self, *args):
            raise AssertionError("historical read must not execute")

    result = worker.run_iteration(
        data,
        Generator(step(action="continue", edits=[], focus_paths=["app.py"])),
        NoExecution(),
        lambda: None,
    )
    assert result["checks"] == old["checks"]
    assert "execution_receipt" not in result


def test_interrupted_profile_keeps_unknown_counts_instead_of_inventing_zero(
    monkeypatch,
):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(runtime.os, "getuid", lambda: 1000)
    runner = runtime.DockerRunner("sha256:" + "a" * 64)

    def interrupted(*args):
        raise runtime.RuntimeError("synthetic timeout")

    monkeypatch.setattr(runner, "_process", interrupted)
    monkeypatch.setattr(runner, "_cleanup", lambda *args: None)
    result = runner.run(
        [{"path": "a.py", "content": "x=1\n"}], "python", [], lambda: None
    )
    receipt = result["execution_receipt"]
    assert receipt["observation_status"] == "incomplete"
    assert receipt["incomplete_reasons"] == ["profile_interrupted"]
    for profile in receipt["profiles"]:
        assert (
            profile["exit_code"]
            is profile["tests_executed"]
            is profile["test_failures"]
            is None
        )
    bind_execution_receipt(
        receipt,
        source_sha256=receipt["source_sha256"],
        runtime="python",
        checks=result["checks"],
    )


def test_harness_receipt_decoding_is_bounded_and_rejects_nested_invalid_json():
    with pytest.raises(runtime.RuntimeError, match="bound"):
        runtime.parse_receipt(runtime.RECEIPT_PREFIX + "x" * 16_385, "python_test", 0)
    with pytest.raises(runtime.RuntimeError, match="invalid"):
        runtime.parse_receipt(
            runtime.RECEIPT_PREFIX + "[" * 1500 + "]" * 1500, "python_test", 0
        )


def test_special_files_are_not_opened_as_measurement_content(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ValueError, match="special"):
        check_harness._measurement_tree(tmp_path)


def test_environment_bound_failure_does_not_manufacture_matching_hashes(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("x=1\n")
    monkeypatch.setattr(check_harness, "PROJECT_ROOT", source)
    monkeypatch.setattr(check_harness, "DEPENDENCY_ROOT", tmp_path / "missing-deps")
    before = check_harness._measurement_capture(("a.py",))
    observation = check_harness._measurement_finish(before, before, "a" * 64)
    assert observation["source_unchanged"] is True
    assert observation["environment_unchanged"] is None
    assert observation["errors"] == ["environment_unbound"]
