import importlib.util
import json
import sys
from pathlib import Path

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
