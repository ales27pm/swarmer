#!/usr/bin/env python3
"""Lease-aware, read-only Git and Ruff review worker."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess  # nosec B404
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

LOGGER = logging.getLogger("mongars.code_review_worker")

REVIEW_SKILLS = frozenset(
    {
        "code_review.git_status",
        "code_review.git_diff",
        "code_review.git_show",
        "code_review.static_analysis",
    }
)
PROTECTED_NAMES = frozenset({".git", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials"})
MAX_PATHS = 50
MAX_SELECTED_FILES = 100
MAX_STATUS_ENTRIES = 200
MAX_PATH_CHARACTERS = 500
MAX_CONTROL_RESPONSE_BYTES = 1_000_000
MAX_COMMAND_OUTPUT_BYTES = 500_000
MAX_JOB_RESULT_BYTES = 524_288
FULL_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")

ControlRequest = Callable[[str, str, str, str, dict[str, Any] | None], Any]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Keep the worker bearer credential on the configured origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


class WorkerProtocolError(RuntimeError):
    """The control plane returned data outside the worker protocol."""


class LeaseLost(RuntimeError):
    """The control plane rejected the current lease proof."""


class LeaseUnavailable(RuntimeError):
    """The worker cannot establish that its lease remains current."""


class ControlPlaneUnavailable(RuntimeError):
    """A control-plane request failed without a known terminal outcome."""


class WorkerExecutionError(RuntimeError):
    """A fixed read-only inspection could not complete safely."""


class CommandResult:
    __slots__ = ("exit_code", "output", "truncated")

    def __init__(self, exit_code: int, output: str, truncated: bool) -> None:
        self.exit_code = exit_code
        self.output = output
        self.truncated = truncated


class CommandRunner(Protocol):
    def __call__(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
        env: dict[str, str],
    ) -> CommandResult: ...


class ReviewExecutor(Protocol):
    def execute(self, job: dict[str, Any]) -> dict[str, Any]: ...


class ReviewRequest:
    __slots__ = ("context_lines", "paths", "revision", "skill", "staged")

    def __init__(
        self,
        skill: str,
        *,
        paths: tuple[str, ...] = (),
        staged: bool = False,
        context_lines: int = 3,
        revision: str | None = None,
    ) -> None:
        self.skill = skill
        self.paths = paths
        self.staged = staged
        self.context_lines = context_lines
        self.revision = revision


class LeaseProof:
    __slots__ = ("claim_token", "lease_generation", "lease_id")

    def __init__(self, claim_token: str, lease_id: str, lease_generation: int) -> None:
        self.claim_token = claim_token
        self.lease_id = lease_id
        self.lease_generation = lease_generation

    @classmethod
    def from_job(cls, job: dict[str, Any]) -> LeaseProof:
        claim_token = job.get("claim_token") or job.get("lease_token")
        lease_id = job.get("lease_id")
        lease_generation = job.get("lease_generation")
        if not isinstance(claim_token, str) or not claim_token:
            raise WorkerProtocolError("claim response is missing its claim token")
        if not isinstance(lease_id, str) or not lease_id:
            raise WorkerProtocolError("claim response is missing its lease id")
        if (
            not isinstance(lease_generation, int)
            or isinstance(lease_generation, bool)
            or lease_generation < 1
        ):
            raise WorkerProtocolError("claim response has an invalid lease generation")
        return cls(claim_token, lease_id, lease_generation)

    def body(self) -> dict[str, Any]:
        return {
            "claim_token": self.claim_token,
            "lease_id": self.lease_id,
            "lease_generation": self.lease_generation,
        }


def _read_control_response(response: Any) -> Any:
    raw = response.read(MAX_CONTROL_RESPONSE_BYTES + 1)
    if len(raw) > MAX_CONTROL_RESPONSE_BYTES:
        raise WorkerProtocolError("control-plane response exceeded its size limit")
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError as exc:
        raise WorkerProtocolError("control plane returned invalid JSON") from exc


def _is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_control_plane_origin(origin: str) -> str:
    """Validate one credential-free bare origin before attaching a bearer token."""

    if origin != origin.strip() or any(character.isspace() for character in origin):
        raise ValueError("control-plane URL must be a bare HTTPS origin")
    parsed = urlsplit(origin)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("control-plane URL has an invalid port") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
        or "?" in origin
        or "#" in origin
        or "\\" in origin
    ):
        raise ValueError("control-plane URL must be a credential-free bare origin")
    if parsed.scheme == "https":
        return origin
    if parsed.scheme == "http" and _is_loopback_host(parsed.hostname):
        return origin
    raise ValueError("control-plane URL requires HTTPS outside loopback")


def control_plane_request(
    base_url: str,
    path: str,
    token: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> Any:
    base_url = validate_control_plane_origin(base_url)
    encoded = json.dumps(body, allow_nan=False, separators=(",", ":")).encode() if body else None
    headers = {"Authorization": f"Bearer {token}"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=encoded, method=method, headers=headers
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    # This destination is operator-configured control-plane policy, never job input.
    with opener.open(request, timeout=30) as response:  # nosec B310
        return _read_control_response(response)


class ControlPlaneClient:
    def __init__(
        self,
        base_url: str,
        agent_id: str,
        credential: str,
        request_fn: ControlRequest | None = None,
    ) -> None:
        self.base_url = validate_control_plane_origin(base_url)
        self.agent_id = agent_id
        self.credential = credential
        self._request = control_plane_request if request_fn is None else request_fn

    @staticmethod
    def _segment(value: str) -> str:
        return quote(value, safe="")

    def _path(self, suffix: str) -> str:
        return f"/agents/{self._segment(self.agent_id)}{suffix}"

    def _call(self, path: str, method: str, body: dict[str, Any]) -> Any:
        try:
            return self._request(self.base_url, path, self.credential, method, body)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise LeaseLost("job lease is stale or no longer owned by this worker") from exc
            raise ControlPlaneUnavailable("control-plane request failed") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ControlPlaneUnavailable("control-plane request failed") from exc
        except json.JSONDecodeError as exc:
            raise WorkerProtocolError("control plane returned invalid JSON") from exc

    @staticmethod
    def _object(response: Any, operation: str) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise WorkerProtocolError(f"{operation} returned a non-object response")
        return response

    def heartbeat_agent(self, status: str) -> None:
        self._call(self._path("/heartbeat"), "POST", {"status": status})

    def claim(self) -> dict[str, Any] | None:
        response = self._call(self._path("/claim"), "POST", {"wait_seconds": 0})
        if response is None:
            return None
        return self._object(response, "job claim")

    def heartbeat_job(self, job_id: str, lease: LeaseProof) -> dict[str, Any]:
        response = self._call(
            self._path(f"/jobs/{self._segment(job_id)}/heartbeat"),
            "POST",
            lease.body(),
        )
        return self._object(response, "job heartbeat")

    def submit_result(
        self, job_id: str, lease: LeaseProof, result_body: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._call(
            self._path(f"/jobs/{self._segment(job_id)}/result"),
            "POST",
            {**result_body, **lease.body()},
        )
        return self._object(response, "job result")


class LeaseHeartbeat:
    def __init__(
        self,
        client: ControlPlaneClient,
        job_id: str,
        lease: LeaseProof,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("job heartbeat interval must be positive")
        self.client = client
        self.job_id = job_id
        self.lease = lease
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._failure: Exception | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self.client.heartbeat_job(self.job_id, self.lease)
        except LeaseLost:
            raise
        except (ControlPlaneUnavailable, WorkerProtocolError) as exc:
            raise LeaseUnavailable("initial job lease renewal failed") from exc
        self._thread = threading.Thread(
            target=self._run,
            name=f"review-lease-heartbeat-{self.job_id}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.client.heartbeat_job(self.job_id, self.lease)
            except (LeaseLost, ControlPlaneUnavailable, WorkerProtocolError) as exc:
                with self._lock:
                    self._failure = exc
                self._stop.set()
                return

    def ensure_active(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is None:
            return
        if isinstance(failure, LeaseLost):
            raise failure
        raise LeaseUnavailable("job lease renewal failed") from failure

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


def protected_name(name: str) -> bool:
    folded = name.casefold()
    return bool(
        folded in PROTECTED_NAMES
        or folded.startswith(".env")
        or "secret" in folded
        or "token" in folded
        or "credential" in folded
        or "password" in folded
        or "passwd" in folded
        or folded.endswith((".pem", ".key"))
    )


def _validate_relative_path(value: str) -> str:
    if not value or len(value) > MAX_PATH_CHARACTERS or "\0" in value:
        raise ValueError("review path is invalid")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("review path escapes repository root")
    if any(protected_name(part) for part in candidate.parts):
        raise ValueError("review path is protected")
    return candidate.as_posix()


def _paths(payload: dict[str, Any], *, required: bool = False) -> tuple[str, ...]:
    raw = payload.get("paths", [])
    if not isinstance(raw, list) or len(raw) > MAX_PATHS:
        raise TypeError("paths must be a bounded array")
    if required and not raw:
        raise ValueError("static analysis requires at least one Python file")
    normalized: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise TypeError("review paths must be strings")
        path = _validate_relative_path(value)
        if path not in normalized:
            normalized.append(path)
    return tuple(normalized)


def _context_lines(payload: dict[str, Any]) -> int:
    value = payload.get("context_lines", 3)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 20:
        raise ValueError("context_lines must be an integer between 0 and 20")
    return value


def _validate_static_file(root: Path, relative: str) -> None:
    if Path(relative).suffix.casefold() != ".py":
        raise ValueError("static analysis accepts only Python files")
    cursor = root
    for part in Path(relative).parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("static analysis does not follow symbolic links")
    target = (root / relative).resolve(strict=True)
    if root != target and root not in target.parents:
        raise ValueError("review path escapes repository root")
    if not target.is_file():
        raise ValueError("static analysis accepts only regular files")


def parse_review_job(root: Path, job: dict[str, Any]) -> ReviewRequest:
    root = root.resolve(strict=True)
    skill = job.get("required_skill")
    if not isinstance(skill, str) or skill not in REVIEW_SKILLS:
        raise ValueError("unsupported worker skill")
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise TypeError("job payload must be an object")
    allowed_fields = {
        "code_review.git_status": frozenset(),
        "code_review.git_diff": frozenset({"paths", "staged", "context_lines"}),
        "code_review.git_show": frozenset({"paths", "revision", "context_lines"}),
        "code_review.static_analysis": frozenset({"paths"}),
    }[skill]
    if set(payload) - allowed_fields:
        raise ValueError("review payload contains unsupported fields")
    if skill == "code_review.git_status":
        return ReviewRequest(skill)
    if skill == "code_review.git_diff":
        staged = payload.get("staged", False)
        if not isinstance(staged, bool):
            raise TypeError("staged must be a boolean")
        return ReviewRequest(
            skill,
            paths=_paths(payload),
            staged=staged,
            context_lines=_context_lines(payload),
        )
    if skill == "code_review.git_show":
        revision = payload.get("revision")
        if not isinstance(revision, str) or not (
            revision == "HEAD" or FULL_COMMIT_RE.fullmatch(revision)
        ):
            raise ValueError("revision must be HEAD or a full commit object id")
        return ReviewRequest(
            skill,
            paths=_paths(payload),
            context_lines=_context_lines(payload),
            revision=revision,
        )
    paths = _paths(payload, required=True)
    for path in paths:
        _validate_static_file(root, path)
    return ReviewRequest(skill, paths=paths)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()
    process.wait()


def run_bounded_command(
    arguments: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int,
    env: dict[str, str],
) -> CommandResult:
    if not arguments or timeout_seconds <= 0 or max_output_bytes <= 0:
        raise ValueError("bounded command configuration is invalid")
    # Callers construct fixed argument arrays; job payloads never supply argv.
    process = subprocess.Popen(  # nosec B603
        arguments,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )
    if process.stdout is None:
        _stop_process(process)
        raise WorkerExecutionError("inspection output pipe was unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    output = bytearray()
    truncated = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                raise WorkerExecutionError("read-only inspection timed out")
            events = selector.select(timeout=min(remaining, 0.05))
            if not events:
                continue
            for key, _ in events:
                chunk = os.read(key.fd, 8_192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                available = max_output_bytes - len(output)
                output.extend(chunk[:available])
                if len(chunk) > available:
                    truncated = True
                    _stop_process(process)
                    selector.close()
                    return CommandResult(
                        process.returncode,
                        bytes(output).decode("utf-8", errors="ignore"),
                        True,
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process(process)
            raise WorkerExecutionError("read-only inspection timed out")
        exit_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        _stop_process(process)
        raise WorkerExecutionError("read-only inspection timed out") from exc
    finally:
        selector.close()
    return CommandResult(exit_code, bytes(output).decode("utf-8", errors="ignore"), truncated)


def _safe_subprocess_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }


def _validate_git_metadata(root: Path) -> Path:
    git_dir = root / ".git"
    try:
        metadata = git_dir.lstat()
    except OSError as exc:
        raise ValueError("review root Git metadata is unavailable") from exc
    if git_dir.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("review root Git metadata must be a contained directory")
    for forbidden in (
        git_dir / "commondir",
        git_dir / "objects" / "info" / "alternates",
        git_dir / "objects" / "info" / "http-alternates",
    ):
        if forbidden.exists() or forbidden.is_symlink():
            raise ValueError("review root uses alternate object or Git metadata storage")
    for directory, names, files in os.walk(git_dir, followlinks=False):
        base = Path(directory)
        for name in (*names, *files):
            if (base / name).is_symlink():
                raise ValueError("review root Git metadata contains a symbolic link")
    return git_dir


def _nul_records(output: str) -> list[str]:
    records = output.split("\0")
    if not output.endswith("\0"):
        records = records[:-1]
    return [record for record in records if record]


def _safe_discovered_paths(output: str) -> tuple[list[str], int, bool]:
    safe: list[str] = []
    omitted = 0
    truncated = False
    for raw in _nul_records(output):
        try:
            path = _validate_relative_path(raw)
        except ValueError:
            omitted += 1
            continue
        if path in safe:
            continue
        if len(safe) >= MAX_SELECTED_FILES:
            truncated = True
            continue
        safe.append(path)
    return safe, omitted, truncated


class CodeReviewExecutor:
    """Build and execute only fixed, read-only Git and Ruff commands."""

    def __init__(
        self,
        root: Path,
        *,
        git_binary: str,
        ruff_binary: str,
        timeout_seconds: float = 20,
        runner: CommandRunner = run_bounded_command,
    ) -> None:
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("review root must be a Git working tree")
        git_dir = _validate_git_metadata(root)
        if not Path(git_binary).is_absolute() or Path(git_binary).name != "git":
            raise ValueError("git executable must be an absolute git path")
        if not Path(ruff_binary).is_absolute() or Path(ruff_binary).name != "ruff":
            raise ValueError("Ruff executable must be an absolute ruff path")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("review timeout must be between 0 and 60 seconds")
        self.root = root
        self.git_dir = git_dir
        self.git_binary = git_binary
        self.ruff_binary = ruff_binary
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.environment = _safe_subprocess_environment()

    def _run(self, arguments: list[str]) -> CommandResult:
        return self.runner(
            arguments,
            cwd=self.root,
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
            env=self.environment,
        )

    def _git_prefix(self) -> list[str]:
        return [
            self.git_binary,
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.root}",
            "-c",
            f"core.worktree={self.root}",
            "-c",
            "core.attributesfile=/dev/null",
            "-c",
            "core.excludesfile=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.quotepath=false",
            "--no-pager",
        ]

    @staticmethod
    def _require_git_success(result: CommandResult) -> None:
        if result.exit_code != 0 and not result.truncated:
            raise WorkerExecutionError("read-only Git inspection failed")

    def _status(self) -> dict[str, Any]:
        command = self._git_prefix() + [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
            "--ignore-submodules=all",
        ]
        command_result = self._run(command)
        self._require_git_success(command_result)
        raw_records = _nul_records(command_result.output)
        entries: list[dict[str, str]] = []
        omitted = 0
        index = 0
        entry_limit_hit = False
        while index < len(raw_records):
            record = raw_records[index]
            index += 1
            if len(record) < 4 or record[2] != " ":
                omitted += 1
                continue
            status = record[:2]
            paths = [record[3:]]
            if ("R" in status or "C" in status) and index < len(raw_records):
                paths.append(raw_records[index])
                index += 1
            try:
                safe_paths = [_validate_relative_path(path) for path in paths]
            except ValueError:
                omitted += 1
                continue
            if len(entries) >= MAX_STATUS_ENTRIES:
                entry_limit_hit = True
                continue
            entry: dict[str, str] = {"status": status, "path": safe_paths[0]}
            if len(safe_paths) == 2:
                entry["original_path"] = safe_paths[1]
            entries.append(entry)
        result: dict[str, Any] = {
            "content_trust": "untrusted",
            "entries": entries,
            "protected_entries_omitted": omitted,
            "truncated": command_result.truncated or entry_limit_hit,
        }
        ensure_result_size(result)
        return result

    def _discover(self, command: list[str]) -> tuple[list[str], int, bool]:
        result = self._run(command)
        self._require_git_success(result)
        paths, omitted, selection_truncated = _safe_discovered_paths(result.output)
        return paths, omitted, result.truncated or selection_truncated

    def _diff(self, request: ReviewRequest) -> dict[str, Any]:
        options = [
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--ignore-submodules=all",
        ]
        if request.staged:
            options.append("--cached")
        discovery = self._git_prefix() + ["diff", *options, "--name-only", "-z", "--"]
        discovery.extend(request.paths)
        paths, omitted, selection_truncated = self._discover(discovery)
        if not paths:
            result: dict[str, Any] = {
                "content_trust": "untrusted",
                "output": "",
                "exit_code": 0,
                "protected_entries_omitted": omitted,
                "truncated": selection_truncated,
            }
            return result
        command = self._git_prefix() + [
            "diff",
            *options,
            f"--unified={request.context_lines}",
            "--",
            *paths,
        ]
        command_result = self._run(command)
        self._require_git_success(command_result)
        result = {
            "content_trust": "untrusted",
            "output": command_result.output,
            "exit_code": command_result.exit_code,
            "protected_entries_omitted": omitted,
            "truncated": command_result.truncated or selection_truncated,
        }
        ensure_result_size(result)
        return result

    def _show(self, request: ReviewRequest) -> dict[str, Any]:
        if request.revision is None:
            raise ValueError("show revision is required")
        options = [
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--ignore-submodules=all",
        ]
        discovery = self._git_prefix() + [
            "show",
            *options,
            "--format=",
            "--name-only",
            "-z",
            request.revision,
            "--",
            *request.paths,
        ]
        paths, omitted, selection_truncated = self._discover(discovery)
        if not paths:
            result: dict[str, Any] = {
                "content_trust": "untrusted",
                "output": "",
                "exit_code": 0,
                "protected_entries_omitted": omitted,
                "truncated": selection_truncated,
            }
            return result
        command = self._git_prefix() + [
            "show",
            *options,
            "--format=fuller",
            f"--unified={request.context_lines}",
            request.revision,
            "--",
            *paths,
        ]
        command_result = self._run(command)
        self._require_git_success(command_result)
        result = {
            "content_trust": "untrusted",
            "output": command_result.output,
            "exit_code": command_result.exit_code,
            "protected_entries_omitted": omitted,
            "truncated": command_result.truncated or selection_truncated,
        }
        ensure_result_size(result)
        return result

    def _static_analysis(self, request: ReviewRequest) -> dict[str, Any]:
        command = [
            self.ruff_binary,
            "check",
            "--isolated",
            "--no-cache",
            "--no-fix",
            "--output-format",
            "concise",
            "--",
            *request.paths,
        ]
        command_result = self._run(command)
        if command_result.exit_code not in {0, 1} and not command_result.truncated:
            raise WorkerExecutionError("fixed Ruff analysis failed")
        result: dict[str, Any] = {
            "content_trust": "untrusted",
            "output": command_result.output,
            "exit_code": command_result.exit_code,
            "protected_entries_omitted": 0,
            "truncated": command_result.truncated,
        }
        ensure_result_size(result)
        return result

    def execute(self, job: dict[str, Any]) -> dict[str, Any]:
        request = parse_review_job(self.root, job)
        if request.skill == "code_review.git_status":
            return self._status()
        if request.skill == "code_review.git_diff":
            return self._diff(request)
        if request.skill == "code_review.git_show":
            return self._show(request)
        return self._static_analysis(request)


def ensure_result_size(result: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("worker result is not canonical JSON") from exc
    if len(encoded) > MAX_JOB_RESULT_BYTES:
        raise ValueError("worker result exceeded its size limit")


def ensure_result_contract(result: dict[str, Any]) -> None:
    if result.get("content_trust") != "untrusted":
        raise ValueError("worker result must be marked as untrusted")
    ensure_result_size(result)


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, OSError):
        return "read-only inspection failed"
    return str(exc)[:500]


def run_once(
    base_url: str,
    agent_id: str,
    credential: str,
    executor: ReviewExecutor,
    *,
    heartbeat_interval_seconds: float = 10,
) -> bool:
    if heartbeat_interval_seconds <= 0:
        raise ValueError("job heartbeat interval must be positive")
    client = ControlPlaneClient(base_url, agent_id, credential)
    client.heartbeat_agent("online")
    job = client.claim()
    if job is None:
        return False
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise WorkerProtocolError("claim response is missing its job id")
    lease = LeaseProof.from_job(job)
    heartbeat = LeaseHeartbeat(client, job_id, lease, heartbeat_interval_seconds)
    try:
        client.heartbeat_agent("busy")
        heartbeat.start()
        try:
            result = executor.execute(job)
            if not isinstance(result, dict):
                raise TypeError("review executor returned a non-object result")
            ensure_result_contract(result)
            result_body: dict[str, Any] = {"status": "completed", "result": result}
        except (OSError, TypeError, ValueError, WorkerExecutionError) as exc:
            result_body = {"status": "failed", "error": _failure_message(exc)}
        heartbeat.ensure_active()
        client.submit_result(job_id, lease, result_body)
    except (LeaseLost, LeaseUnavailable) as exc:
        LOGGER.warning("discarding local result because the job lease is unavailable: %s", exc)
    except (ControlPlaneUnavailable, WorkerProtocolError) as exc:
        LOGGER.warning("control-plane operation failed; leaving job for lease recovery: %s", exc)
    finally:
        heartbeat.stop()
        try:
            client.heartbeat_agent("online")
        except (ControlPlaneUnavailable, WorkerProtocolError, LeaseLost):
            LOGGER.warning("could not return agent status to online", exc_info=True)
    return True


def _resolve_executable(value: str, expected_name: str) -> str:
    resolved = shutil.which(value)
    if resolved is None or Path(resolved).name != expected_name:
        raise ValueError(f"{expected_name} executable is unavailable")
    return str(Path(resolved).resolve(strict=True))


def _bounded_float(value: str, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} is outside its allowed range")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    timeout_seconds = _bounded_float(
        os.environ.get("MONGARS_REVIEW_TIMEOUT_SECONDS", "20"),
        "review timeout",
        1,
        60,
    )
    heartbeat_seconds = _bounded_float(
        os.environ.get("MONGARS_JOB_HEARTBEAT_SECONDS", "10"),
        "job heartbeat interval",
        0.1,
        60,
    )
    executor = CodeReviewExecutor(
        Path(os.environ["MONGARS_REVIEW_ROOT"]),
        git_binary=_resolve_executable(os.environ.get("MONGARS_GIT_BIN", "git"), "git"),
        ruff_binary=_resolve_executable(os.environ.get("MONGARS_RUFF_BIN", "ruff"), "ruff"),
        timeout_seconds=timeout_seconds,
    )
    while True:
        worked = run_once(
            os.environ["MONGARS_SERVER_URL"],
            os.environ["MONGARS_AGENT_ID"],
            os.environ["MONGARS_AGENT_CREDENTIAL"],
            executor,
            heartbeat_interval_seconds=heartbeat_seconds,
        )
        if args.once:
            return
        time.sleep(1 if worked else 5)


if __name__ == "__main__":
    main()
