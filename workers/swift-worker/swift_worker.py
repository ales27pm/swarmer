#!/usr/bin/env python3
"""Bounded SwiftPM/Xcode tools for an operator-approved iMac workspace.

Build scripts and package manifests are executable code: use a dedicated account
and only approved source. Fixed argv is not a sandbox. No shell or arbitrary flags.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.util
import json
import os
import re
import selectors
import signal
import stat
import subprocess  # nosec B404 - fixed compiler argv in an operator-approved workspace
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET  # nosec B405 - bounded UTF-8 without DTD or entities
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

MAX_LOG_BYTES = 1_000_000
MAX_REMOTE_SECONDS = 120
SKILLS = {"code.swift.build": "build", "code.swift.test": "test"}
_EXCLUDED = frozenset({".git", ".build", ".swarmer-swift-runs"})


class SwiftWorkerError(ValueError):
    """No successful Swift execution receipt can be issued."""


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    size = 0
    files = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in _EXCLUDED)
        for name in dirs + names:
            if (Path(current) / name).is_symlink():
                raise SwiftWorkerError("source workspace contains a symlink")
        for name in sorted(names):
            path = Path(current) / name
            if not path.is_file():
                raise SwiftWorkerError("source contains a non-file entry")
            files += 1
            size += path.stat().st_size
            if files > 50_000 or size > 512_000_000:
                raise SwiftWorkerError("source exceeds snapshot bounds")
            relative = str(path.relative_to(root)).encode()
            digest.update(len(relative).to_bytes(4, "big") + relative)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _stop(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
    except ProcessLookupError:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_command(
    argv: list[str],
    cwd: Path,
    log: Path,
    timeout: float,
    ensure_active: Callable[[], None],
) -> int:
    """Run a fixed tool argv with bounded output and process-group cancellation."""
    if not 0 < timeout <= 1800:
        raise SwiftWorkerError("invalid command wall budget")
    ensure_active()
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "DEVELOPER_DIR", "SDKROOT", "LANG", "LC_ALL"}
    }
    env["NSUnbufferedIO"] = "YES"
    started = time.monotonic()
    process = subprocess.Popen(  # nosec B603 - executable and options are fixed by this worker
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        if process.stdout is None:
            raise SwiftWorkerError("Swift command output pipe unavailable")
        with selectors.DefaultSelector() as selector, log.open("xb") as output:
            selector.register(process.stdout, selectors.EVENT_READ)
            total = 0
            while selector.get_map():
                ensure_active()
                if time.monotonic() - started > timeout:
                    raise SwiftWorkerError("Swift command exceeded wall-time limit")
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_LOG_BYTES:
                        raise SwiftWorkerError("Swift command output exceeded byte limit")
                    output.write(chunk)
            ensure_active()
            return process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
    finally:
        _stop(process)
        if process.stdout is not None:
            process.stdout.close()


def xunit_counts(path: Path) -> tuple[int, int]:
    if not path.is_file() or path.stat().st_size > MAX_LOG_BYTES:
        raise SwiftWorkerError("missing or oversized Swift test report")
    try:
        raw = path.read_bytes().decode("utf-8-sig")
    except UnicodeError as exc:
        raise SwiftWorkerError("test report must use UTF-8") from exc
    if "\0" in raw or "<!DOCTYPE" in raw or "<!ENTITY" in raw:
        raise SwiftWorkerError("unsupported XML declaration")
    try:
        root = ET.fromstring(raw)  # nosec B314 - bounded text; DTD/entity declarations rejected above
    except ET.ParseError as exc:
        raise SwiftWorkerError("invalid test report") from exc
    cases = list(root.iter("testcase"))
    executed = sum(case.find("skipped") is None for case in cases)
    failures = sum(
        case.find("failure") is not None or case.find("error") is not None for case in cases
    )
    return executed, failures


def xcresult_counts(path: Path) -> tuple[int, int]:
    if not path.is_file() or path.stat().st_size > MAX_LOG_BYTES:
        raise SwiftWorkerError("missing or oversized Xcode summary")
    try:
        data = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise SwiftWorkerError("invalid Xcode summary") from exc
    if not isinstance(data, dict):
        raise SwiftWorkerError("invalid Xcode summary object")
    counts = [
        data.get(key)
        for key in (
            "passedTests",
            "failedTests",
            "expectedFailures",
            "skippedTests",
            "totalTestCount",
        )
    ]
    if any(type(value) is not int or value < 0 for value in counts):
        raise SwiftWorkerError("invalid Xcode test counts")
    passed, failed, expected, skipped, total = cast(list[int], counts)
    if total != passed + failed + expected + skipped:
        raise SwiftWorkerError("inconsistent Xcode test counts")
    if data.get("result") not in {"Passed", "Failed", "Expected Failure", "Skipped"}:
        raise SwiftWorkerError("unknown Xcode test result")
    return passed + failed + expected, failed if data["result"] != "Failed" else max(1, failed)


class SwiftWorkspace:
    def __init__(
        self,
        root: Path,
        *,
        approved_source_sha256: str,
        destinations: dict[str, str] | None = None,
        runner: Callable[..., int] = run_command,
        timeout: float = MAX_REMOTE_SECONDS,
    ) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir() or not 0 < timeout <= 1800:
            raise SwiftWorkerError("invalid approved workspace or timeout")
        if not re.fullmatch(r"[a-f0-9]{64}", approved_source_sha256):
            raise SwiftWorkerError("an independently approved source digest is required")
        self.approved_source_sha256 = approved_source_sha256
        if destinations is not None and not isinstance(destinations, dict):
            raise SwiftWorkerError("operator destinations must be an object")
        self.destinations = destinations or {}
        for key, value in self.destinations.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) or not re.fullmatch(
                r"platform=(?:iOS Simulator|iOS|macOS)(?:,id=[A-Fa-f0-9-]{8,64})?",
                value,
            ):
                raise SwiftWorkerError(
                    "destination must be an operator-approved Apple platform/device"
                )
        self.runner = runner
        self.timeout = timeout

    def execute(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        ensure_active: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        if operation not in {"build", "test"} or not isinstance(payload, dict):
            raise SwiftWorkerError("unsupported Swift operation")
        ensure_active()
        initial = source_digest(self.root)
        expected = payload.get("source_sha256")
        if initial != self.approved_source_sha256 or expected != self.approved_source_sha256:
            raise SwiftWorkerError("source_sha256 does not match the approved source revision")
        kind = payload.get("kind", "swiftpm")
        canonical_payload = {**payload, "kind": kind}
        allowed = {"kind", "source_sha256"}
        if kind == "xcode":
            allowed |= {"project", "scheme", "destination"}
        if payload.keys() - allowed:
            raise SwiftWorkerError("unsupported Swift operation fields")
        artifact_root = self.root / ".swarmer-swift-runs"
        if artifact_root.is_symlink():
            raise SwiftWorkerError("artifact root cannot be a symlink")
        artifact_root.mkdir(exist_ok=True, mode=0o700)
        run_dir = artifact_root / uuid.uuid4().hex
        run_dir.mkdir(mode=0o700)
        report = run_dir / "tests.xml"
        result_bundle = run_dir / "tests.xcresult"
        if kind == "swiftpm":
            if not (self.root / "Package.swift").is_file():
                raise SwiftWorkerError("Package.swift missing")
            argv = [
                "/usr/bin/xcrun",
                "swift",
                operation,
                "--package-path",
                str(self.root),
                "--scratch-path",
                str(run_dir / "build"),
                "--disable-automatic-resolution",
                "--disable-netrc",
                "--disable-keychain",
                "--jobs",
                "2",
            ]
            if operation == "test":
                argv += [
                    "--parallel",
                    "--num-workers",
                    "1",
                    "--xunit-output",
                    str(report),
                ]
        elif kind == "xcode":
            project = payload.get("project")
            scheme = payload.get("scheme")
            destination = self.destinations.get(payload.get("destination", ""))
            if not isinstance(project, str) or not re.fullmatch(
                r"[A-Za-z0-9_ .-]+\.(?:xcodeproj|xcworkspace)", project
            ):
                raise SwiftWorkerError("one root-level Xcode project/workspace is required")
            if not (self.root / project).is_dir():
                raise SwiftWorkerError("Xcode project/workspace not found")
            if not isinstance(scheme, str) or not re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_ .-]{0,127}", scheme
            ):
                raise SwiftWorkerError("invalid Xcode scheme")
            if destination is None:
                raise SwiftWorkerError("destination is not configured by the operator")
            argv = [
                "/usr/bin/xcrun",
                "xcodebuild",
                "-workspace" if project.endswith(".xcworkspace") else "-project",
                project,
                "-scheme",
                scheme,
                "-configuration",
                "Debug",
                "-destination",
                destination,
                "-derivedDataPath",
                str(run_dir / "DerivedData"),
                "-disableAutomaticPackageResolution",
                "-resultBundlePath",
                str(result_bundle),
                operation,
            ]
            argv += ["CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO"]
        else:
            raise SwiftWorkerError("kind must be swiftpm or xcode")
        started = time.monotonic()
        exit_code = self.runner(
            argv, self.root, run_dir / "command.log", self.timeout, ensure_active
        )
        ensure_active()
        executed = failures = 0
        report_error = None
        if operation == "test":
            try:
                if kind == "swiftpm":
                    executed, failures = xunit_counts(report)
                else:
                    summary = run_dir / "summary.json"
                    remaining = self.timeout - (time.monotonic() - started)
                    code = self.runner(
                        [
                            "/usr/bin/xcrun",
                            "xcresulttool",
                            "get",
                            "test-results",
                            "summary",
                            "--path",
                            str(result_bundle),
                            "--compact",
                        ],
                        self.root,
                        summary,
                        remaining,
                        ensure_active,
                    )
                    if code != 0:
                        raise SwiftWorkerError("xcresulttool could not decode test result")
                    executed, failures = xcresult_counts(summary)
            except (SwiftWorkerError, OSError) as exc:
                report_error = str(exc)
        final = source_digest(self.root)
        ensure_active()
        duration_ms = round((time.monotonic() - started) * 1000)
        accepted = (
            exit_code == 0
            and final == initial
            and duration_ms <= self.timeout * 1000
            and (operation == "build" or (executed > 0 and failures == 0 and report_error is None))
        )
        receipt = {
            "operation": operation,
            "kind": kind,
            "status": "passed" if accepted else "failed",
            "exit_code": exit_code,
            "source_sha256": initial,
            "request_sha256": hashlib.sha256(
                json.dumps(canonical_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "source_unchanged": final == initial,
            "tests_executed": executed,
            "test_evidence_format": "swiftpm_xunit" if kind == "swiftpm" else "xcresult_summary",
            "test_failures": failures,
            "duration_ms": duration_ms,
            "artifact_directory": str(run_dir.relative_to(self.root)),
            "report_error": report_error,
        }
        (run_dir / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt


@functools.lru_cache(maxsize=1)
def _protocol() -> Any:
    path = Path(__file__).resolve().parents[1] / "file-worker/file_worker.py"
    spec = importlib.util.spec_from_file_location("swift_protocol", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("file-worker protocol missing from release")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=1)
def _project_snapshots() -> Any:
    path = Path(__file__).with_name("project_snapshot.py")
    spec = importlib.util.spec_from_file_location("swift_project_snapshots", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("project snapshot staging missing from release")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProjectSnapshotWorkspace:
    """Explicit operator opt-in to currently consented, lease-bound project snapshots."""

    def __init__(
        self,
        root: Path,
        *,
        destinations: dict[str, str] | None = None,
        runner: Callable[..., int] = run_command,
        timeout: float = MAX_REMOTE_SECONDS,
    ) -> None:
        if not 0 < timeout <= MAX_REMOTE_SECONDS:
            raise SwiftWorkerError("invalid remote project Swift timeout")
        self.timeout = timeout
        self.snapshots = _project_snapshots().SnapshotWorkspace(
            root,
            workspace_factory=SwiftWorkspace,
            source_digest=source_digest,
            runner=runner,
            destinations=destinations,
            timeout=timeout,
        )

    def execute_job(self, operation, payload, *, source_request, ensure_active):
        return self.snapshots.execute_job(
            operation, payload, source_request=source_request, ensure_active=ensure_active
        )


def _project_source_http_request(base_url, path, token, method, body):
    protocol = _protocol()
    origin = protocol.validate_control_plane_origin(base_url)
    call = urllib.request.Request(
        origin + path,
        data=json.dumps(body, allow_nan=False, separators=(",", ":")).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    opener = urllib.request.build_opener(protocol._RejectRedirects())
    with opener.open(call, timeout=5) as response:  # nosec B310 - pinned credential-free control-plane origin
        return protocol._read_control_response(
            response, _project_snapshots().MAX_SOURCE_RESPONSE_BYTES
        )


def _project_source_request(protocol, client, job_id, lease, request_fn):
    """Only this consent-bound source endpoint permits a larger bounded JSON response."""
    source_client = protocol.ControlPlaneClient(
        client.base_url,
        client.agent_id,
        client.credential,
        request_fn=request_fn if request_fn is not None else _project_source_http_request,
    )
    path = (
        f"/agents/{client._segment(client.agent_id)}/jobs/{client._segment(job_id)}/project-source"
    )
    try:
        return source_client._leased_call(path, "POST", lease.body())
    except protocol.ControlPlaneUnavailable as exc:
        raise protocol.LeaseUnavailable("project source consent could not be revalidated") from exc


def run_once(
    base_url: str,
    agent_id: str,
    credential: str,
    workspace: SwiftWorkspace | ProjectSnapshotWorkspace,
    *,
    request_fn: Any | None = None,
) -> bool:
    protocol = _protocol()
    if workspace.timeout > MAX_REMOTE_SECONDS:
        raise SwiftWorkerError("remote Swift operation exceeds the advertised time limit")
    client = protocol.ControlPlaneClient(base_url, agent_id, credential, request_fn=request_fn)
    client.heartbeat_agent("online")
    job = client.claim()
    if job is None:
        return False
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise protocol.WorkerProtocolError("claim has no job ID")
    lease = protocol.LeaseProof.from_job(job)
    heartbeat = protocol.LeaseHeartbeat(client, job_id, lease, 10)
    try:
        client.heartbeat_agent("busy")
        heartbeat.start()
        try:
            if isinstance(workspace, ProjectSnapshotWorkspace):
                result = workspace.execute_job(
                    SKILLS.get(job.get("required_skill"), ""),
                    job.get("payload"),
                    source_request=lambda: _project_source_request(
                        protocol, client, job_id, lease, request_fn
                    ),
                    ensure_active=heartbeat.ensure_active,
                )
            else:
                result = workspace.execute(
                    SKILLS.get(job.get("required_skill"), ""),
                    job.get("payload"),
                    ensure_active=heartbeat.ensure_active,
                )
            body = {
                "status": "completed" if result["status"] == "passed" else "failed",
                "result": result,
            }
        except (ValueError, TypeError, OSError, subprocess.SubprocessError):
            body = {
                "status": "failed",
                "error": "Swift execution did not produce a verified receipt",
            }
        heartbeat.ensure_active()
        client.heartbeat_job(job_id, lease)
        client.submit_result(job_id, lease, body)
        return True
    except (protocol.LeaseLost, protocol.LeaseUnavailable):
        return True
    finally:
        heartbeat.stop()
        try:
            client.heartbeat_agent("online")
        except (protocol.ControlPlaneUnavailable, protocol.WorkerProtocolError):
            pass


def load_credentials(path: Path, workspace: Path) -> tuple[str, str]:
    """Read enrollment output privately; never pass it to compiler subprocesses."""
    if path.resolve().is_relative_to(workspace.resolve()):
        raise SwiftWorkerError("worker credentials must be outside the approved workspace")
    parent = path.parent.stat()
    if path.parent.is_symlink() or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise SwiftWorkerError("credential directory must be private and operator-owned")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or not 1 <= metadata.st_size <= 4096
        ):
            raise SwiftWorkerError("credential file must be small, private and operator-owned")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise SwiftWorkerError("credential file exceeds its size limit")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise SwiftWorkerError("invalid worker registration file") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"agent_id", "credential"}
            or not isinstance(value["agent_id"], str)
            or not re.fullmatch(r"agt_[A-Za-z0-9_-]{1,124}", value["agent_id"])
            or not isinstance(value["credential"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value["credential"])
        ):
            raise SwiftWorkerError("invalid worker registration fields")
        return value["agent_id"], value["credential"]
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--credential-file", required=True, type=Path)
    source_mode = parser.add_mutually_exclusive_group(required=True)
    source_mode.add_argument("--workspace", type=Path)
    source_mode.add_argument("--project-staging-root", type=Path)
    parser.add_argument("--approved-source-sha256")
    parser.add_argument("--timeout", type=float, default=MAX_REMOTE_SECONDS)
    parser.add_argument(
        "--destinations",
        type=Path,
        help="operator-owned JSON key -> Apple destination map",
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        if not 0 < args.timeout <= MAX_REMOTE_SECONDS:
            raise SwiftWorkerError("remote Swift timeout must be between 0 and 120 seconds")
        if args.project_staging_root is not None:
            if args.approved_source_sha256 is not None:
                raise SwiftWorkerError("project staging cannot use a startup source pin")
            staging = _project_snapshots().private_root(args.project_staging_root)
            credential_parent = args.credential_file.parent.resolve(strict=True)
            if staging.is_relative_to(credential_parent) or credential_parent.is_relative_to(
                staging
            ):
                raise SwiftWorkerError("project staging must be outside the credential directory")
            agent_id, token = load_credentials(args.credential_file, staging)
            workspace = ProjectSnapshotWorkspace(
                staging,
                destinations=json.loads(args.destinations.read_text()) if args.destinations else {},
                timeout=args.timeout,
            )
        else:
            if not args.approved_source_sha256:
                raise SwiftWorkerError("workspace mode requires an approved source digest")
            agent_id, token = load_credentials(args.credential_file, args.workspace)
            workspace = SwiftWorkspace(
                args.workspace,
                approved_source_sha256=args.approved_source_sha256,
                destinations=json.loads(args.destinations.read_text()) if args.destinations else {},
                timeout=args.timeout,
            )
        _protocol().validate_control_plane_origin(args.base_url)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    while True:
        try:
            run_once(args.base_url, agent_id, token, workspace)
        except (_protocol().ControlPlaneUnavailable, _protocol().WorkerProtocolError):
            if args.once:
                raise SystemExit(
                    "Swift worker could not confirm the control-plane operation"
                ) from None
            # Retry transport only; a lost job lease never reruns the compilation.
            time.sleep(8)
        if args.once:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
