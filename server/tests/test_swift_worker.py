import importlib.util
import io
import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[2] / "workers/swift-worker/swift_worker.py"
_SPEC = importlib.util.spec_from_file_location("swift_worker_under_test", _PATH)
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)


@pytest.fixture
def package(tmp_path):
    (tmp_path / "Package.swift").write_text(
        '// swift-tools-version: 6.0\nimport PackageDescription\nlet package = Package(name: "Sample", targets: [.testTarget(name: "SampleTests")])\n'
    )
    tests = tmp_path / "Tests/SampleTests"
    tests.mkdir(parents=True)
    (tests / "SampleTests.swift").write_text(
        "import XCTest\nfinal class SampleTests: XCTestCase { func testSum() { XCTAssertEqual(2+2, 4) } }\n"
    )
    return tmp_path


def payload(package, **kwargs):
    return {"source_sha256": worker.source_digest(package), **kwargs}


def approved_workspace(package, **kwargs):
    """Only these handwritten test fixtures are approved by this test harness."""
    return worker.SwiftWorkspace(
        package, approved_source_sha256=worker.source_digest(package), **kwargs
    )


def test_remote_request_cannot_approve_its_own_changed_source(package):
    workspace = approved_workspace(package, runner=lambda *args: pytest.fail("must not compile"))
    (package / "new.swift").write_text('print("changed after operator review")')
    with pytest.raises(worker.SwiftWorkerError, match="approved source"):
        workspace.execute("build", payload(package))


def test_missing_operator_pin_never_compiles(package):
    with pytest.raises((TypeError, worker.SwiftWorkerError)):
        worker.SwiftWorkspace(package, runner=lambda *args: pytest.fail("must not compile"))


def fake_runner(argv, cwd, log, timeout, ensure_active):
    ensure_active()
    log.write_text("success")
    if "--xunit-output" in argv:
        Path(argv[argv.index("--xunit-output") + 1]).write_text(
            '<testsuites><testsuite><testcase name="testSum"/></testsuite></testsuites>'
        )
    return 0


def test_test_requires_executed_case(package):
    workspace = approved_workspace(package, runner=fake_runner)
    receipt = workspace.execute("test", payload(package))
    assert receipt["status"] == "passed"
    assert receipt["tests_executed"] == 1
    assert receipt["source_unchanged"]
    assert (package / receipt["artifact_directory"] / "receipt.json").is_file()


def test_zero_tests_never_passes(package):
    def run(argv, cwd, log, timeout, ensure_active):
        result = fake_runner(argv, cwd, log, timeout, ensure_active)
        Path(argv[argv.index("--xunit-output") + 1]).write_text(
            '<testsuites><testsuite tests="100"/></testsuites>'
        )
        return result

    assert (
        approved_workspace(package, runner=run).execute("test", payload(package))["status"]
        == "failed"
    )


def test_skipped_only_never_passes(package):
    def run(argv, cwd, log, timeout, ensure_active):
        result = fake_runner(argv, cwd, log, timeout, ensure_active)
        Path(argv[argv.index("--xunit-output") + 1]).write_text(
            '<testsuites><testcase name="skip"><skipped/></testcase></testsuites>'
        )
        return result

    receipt = approved_workspace(package, runner=run).execute("test", payload(package))
    assert receipt["status"] == "failed"
    assert receipt["tests_executed"] == 0


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_xunit_rejects_entity_declarations_in_every_encoding(tmp_path, encoding):
    report = tmp_path / "report.xml"
    report.write_bytes(
        '<!DOCTYPE testsuites [<!ENTITY x "expanded">]><testsuites>&x;</testsuites>'.encode(
            encoding
        )
    )
    with pytest.raises(worker.SwiftWorkerError):
        worker.xunit_counts(report)


def test_xunit_accepts_utf8_bom_and_counts_actual_cases(tmp_path):
    report = tmp_path / "report.xml"
    report.write_bytes('<testsuites><testcase name="valid"/></testsuites>'.encode("utf-8-sig"))
    assert worker.xunit_counts(report) == (1, 0)


def test_wrong_revision_does_not_execute(package):
    def never(*args):
        pytest.fail("must not run stale source")

    with pytest.raises(worker.SwiftWorkerError, match="revision"):
        approved_workspace(package, runner=never).execute("build", {"source_sha256": "wrong"})


def test_changed_source_does_not_pass(package):
    def run(argv, cwd, log, timeout, ensure_active):
        (cwd / "Package.swift").write_text("changed")
        return 0

    receipt = approved_workspace(package, runner=run).execute("build", payload(package))
    assert receipt["status"] == "failed"
    assert not receipt["source_unchanged"]


def test_xcode_uses_operator_destination_and_result_summary(package):
    (package / "App.xcodeproj").mkdir()
    commands = []

    def run(argv, cwd, log, timeout, ensure_active):
        commands.append(argv)
        if "xcresulttool" in argv:
            log.write_text(
                json.dumps(
                    {
                        "result": "Passed",
                        "passedTests": 2,
                        "failedTests": 0,
                        "expectedFailures": 0,
                        "skippedTests": 1,
                        "totalTestCount": 3,
                    }
                )
            )
        else:
            log.write_text("build succeeded")
        return 0

    workspace = approved_workspace(
        package, runner=run, destinations={"sim": "platform=iOS Simulator,id=12345678-ABCD"}
    )
    result = workspace.execute(
        "test",
        payload(package, kind="xcode", project="App.xcodeproj", scheme="App", destination="sim"),
    )
    assert result["status"] == "passed"
    assert result["tests_executed"] == 2
    assert "CODE_SIGNING_ALLOWED=NO" in commands[0]
    assert commands[1][1] == "xcresulttool"


@pytest.mark.parametrize(
    "field,value",
    [
        ("project", "../App.xcodeproj"),
        ("scheme", "-allowProvisioningUpdates"),
        ("destination", "other"),
    ],
)
def test_xcode_invalid_arguments_never_execute(package, field, value):
    (package / "App.xcodeproj").mkdir()

    def never(*args):
        pytest.fail("invalid argv executed")

    workspace = approved_workspace(
        package, runner=never, destinations={"sim": "platform=iOS Simulator,id=12345678-ABCD"}
    )
    values = payload(
        package, kind="xcode", project="App.xcodeproj", scheme="App", destination="sim"
    )
    values[field] = value
    with pytest.raises(worker.SwiftWorkerError):
        workspace.execute("test", values)


def test_process_timeout_and_output_limit(tmp_path):
    with pytest.raises(worker.SwiftWorkerError, match="wall-time"):
        worker.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
            tmp_path / "timeout.log",
            0.1,
            lambda: None,
        )
    with pytest.raises(worker.SwiftWorkerError, match="byte limit"):
        worker.run_command(
            [sys.executable, "-c", "print('x'*1100000)"],
            tmp_path,
            tmp_path / "output.log",
            3,
            lambda: None,
        )


class StoppingProcess:
    pid = 4321

    def __init__(self, returncode):
        self.returncode = returncode
        self.polls = 0
        self.waits = 0
        self.stdout = io.BytesIO()

    def poll(self):
        self.polls += 1
        return self.returncode

    def wait(self, *, timeout):
        assert timeout == 2
        self.waits += 1
        return self.returncode


def test_stop_reaps_zombie_then_accepts_disappeared_group(monkeypatch):
    process = StoppingProcess(0)
    signals = []

    def killpg(pid, sig):
        assert pid == process.pid
        signals.append(sig)
        if len(signals) == 1:
            raise PermissionError("Darwin group contains only a zombie")
        raise ProcessLookupError("group disappeared after reaping")

    monkeypatch.setattr(worker.os, "killpg", killpg)
    worker._stop(process)

    assert signals == [signal.SIGTERM, signal.SIGTERM, signal.SIGKILL]
    assert process.polls == 1 and process.waits == 1


@pytest.mark.parametrize("returncode", [None, 0])
def test_stop_never_ignores_persistent_group_permission_error(monkeypatch, returncode):
    process = StoppingProcess(returncode)
    signals = []

    def killpg(pid, sig):
        assert pid == process.pid
        signals.append(sig)
        raise PermissionError("group cannot be controlled")

    monkeypatch.setattr(worker.os, "killpg", killpg)
    with pytest.raises(PermissionError, match="cannot be controlled"):
        worker._stop(process)

    expected = (
        [signal.SIGTERM, signal.SIGKILL]
        if returncode is None
        else [signal.SIGTERM, signal.SIGTERM, signal.SIGKILL, signal.SIGKILL]
    )
    assert signals == expected
    assert process.polls == 2 and process.waits == 0


def test_stop_still_signals_descendants_after_reaping_leader(monkeypatch):
    process = StoppingProcess(0)
    signals = []

    def killpg(pid, sig):
        assert pid == process.pid
        signals.append(sig)
        if len(signals) == 1:
            raise PermissionError("zombie leader during group traversal")
        # A surviving descendant still occupies the same group after poll().

    monkeypatch.setattr(worker.os, "killpg", killpg)
    worker._stop(process)

    assert signals == [signal.SIGTERM, signal.SIGTERM, signal.SIGKILL]
    assert process.polls == 1 and process.waits == 1


def test_stop_accepts_zombie_disappearance_during_kill_escalation(monkeypatch):
    class ExitsAfterTermDeadline(StoppingProcess):
        def wait(self, *, timeout):
            self.waits += 1
            if self.waits == 1:
                raise worker.subprocess.TimeoutExpired("fixture", timeout)
            return 0

    process = ExitsAfterTermDeadline(0)
    signals = []

    def killpg(pid, sig):
        assert pid == process.pid
        signals.append(sig)
        if len(signals) == 2:
            raise PermissionError("leader became a zombie before escalation")
        if len(signals) > 2:
            raise ProcessLookupError("group disappeared after reaping")

    monkeypatch.setattr(worker.os, "killpg", killpg)
    worker._stop(process)

    assert signals == [signal.SIGTERM, signal.SIGKILL, signal.SIGKILL, signal.SIGKILL]
    assert process.polls == 1 and process.waits == 2


@pytest.mark.parametrize("group_refuses", [False, True])
def test_output_limit_survives_zombie_cleanup_and_pipe_always_closes(
    tmp_path, monkeypatch, group_refuses
):
    process = StoppingProcess(0)
    signals = []

    class ReadyOutput:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def register(self, stream, events):
            assert stream is process.stdout

        def get_map(self):
            return {1: process.stdout}

        def select(self, *, timeout):
            return [(SimpleNamespace(fd=1, fileobj=process.stdout), None)]

    def killpg(pid, sig):
        signals.append(sig)
        if len(signals) == 1 or group_refuses:
            raise PermissionError("group cannot be controlled")
        raise ProcessLookupError("group disappeared after reaping")

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(worker.selectors, "DefaultSelector", ReadyOutput)
    monkeypatch.setattr(worker.os, "read", lambda *args: b"x" * (worker.MAX_LOG_BYTES + 1))
    monkeypatch.setattr(worker.os, "killpg", killpg)
    expected = PermissionError if group_refuses else worker.SwiftWorkerError
    message = "cannot be controlled" if group_refuses else "byte limit"
    with pytest.raises(expected, match=message):
        worker.run_command(["fixture"], tmp_path, tmp_path / "output.log", 3, lambda: None)
    assert process.stdout.closed


def test_process_cancelled_and_no_credentials(tmp_path):
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        worker.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
            tmp_path / "cancel.log",
            5,
            cancel,
        )


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/xcrun").exists(),
    reason="requires local Xcode Swift compiler",
)
def test_real_swift_package_build_and_test(package):
    workspace = approved_workspace(package, timeout=180)
    result = workspace.execute("test", payload(package))
    assert result["status"] == "passed", (
        result,
        (package / result["artifact_directory"] / "command.log").read_text(),
    )
    assert result["tests_executed"] == 1
