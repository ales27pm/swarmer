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
import tempfile
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
MAX_GIT_CONFIG_BYTES = 262_144
MAX_SNAPSHOT_FILE_BYTES = 16_777_216
MAX_SNAPSHOT_TOTAL_BYTES = 67_108_864
MAX_SNAPSHOT_MANIFEST_ENTRIES = 250_000
MAX_SNAPSHOT_DEPTH = 64
HEARTBEAT_JOIN_TIMEOUT_SECONDS = 1.0
FULL_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
EXECUTABLE_GIT_SECTION_RE = re.compile(
    r"^\s*\[\s*(?:filter|include|includeif)(?:\.|\s|\"|\])",
    flags=re.IGNORECASE | re.MULTILINE,
)

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


class SnapshotFile:
    __slots__ = ("content", "mode")

    def __init__(self, content: bytes, mode: int) -> None:
        self.content = content
        self.mode = mode


class OperationBudget:
    """One monotonic deadline and cumulative source-read budget for a review."""

    __slots__ = ("deadline", "remaining_bytes")

    def __init__(self, timeout_seconds: float, maximum_bytes: int) -> None:
        if timeout_seconds <= 0 or maximum_bytes < 1:
            raise ValueError("review operation budget is invalid")
        self.deadline = time.monotonic() + timeout_seconds
        self.remaining_bytes = maximum_bytes

    def remaining_seconds(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerExecutionError("read-only inspection timed out")
        return remaining

    def checkpoint(self) -> None:
        self.remaining_seconds()

    def consume(self, size: int) -> None:
        self.checkpoint()
        if size < 0 or size > self.remaining_bytes:
            raise WorkerExecutionError(
                "read-only inspection exceeded its cumulative snapshot byte limit"
            )
        self.remaining_bytes -= size


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
            self._thread.join(timeout=HEARTBEAT_JOIN_TIMEOUT_SECONDS)


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


def _stable_file_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_identity(path: Path, label: str) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory")
    return metadata.st_dev, metadata.st_ino


def _tree_manifest(
    root: Path,
    *,
    expected_root_identity: tuple[int, int],
    label: str,
    budget: OperationBudget,
    skip_top_level: frozenset[str] = frozenset(),
) -> tuple[tuple[str, tuple[int, int, int, int, int, int]], ...]:
    """Capture a bounded, descriptor-relative metadata manifest.

    File contents are copied separately through ``O_NOFOLLOW`` descriptors. The
    manifest joins those per-file checks into one repository-wide generation:
    a same-inode edit changes ctime/mtime and a path swap changes dev/inode.
    """

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_descriptor = os.open(root, directory_flags)
    except OSError as exc:
        raise WorkerExecutionError(f"{label} changed during snapshot capture") from exc
    entries: list[tuple[str, tuple[int, int, int, int, int, int]]] = []
    open_descriptors = {root_descriptor}

    def names_in(descriptor: int) -> list[str]:
        names: list[str] = []
        try:
            with os.scandir(descriptor) as iterator:
                for item in iterator:
                    budget.checkpoint()
                    names.append(item.name)
                    if len(entries) + len(names) > MAX_SNAPSHOT_MANIFEST_ENTRIES:
                        raise WorkerExecutionError(f"{label} exceeds its manifest entry limit")
        except OSError as exc:
            raise WorkerExecutionError(f"{label} changed during snapshot capture") from exc
        names.sort()
        return names

    # Each frame retains its descriptor until every descendant has been walked,
    # allowing the directory name to be revalidated relative to its original
    # parent. Traversal is iterative so a hostile tree cannot exhaust Python's
    # recursion stack; the explicit depth cap also bounds retained descriptors.
    frames: list[
        tuple[
            int,
            tuple[str, ...],
            tuple[int, int, int, int, int, int],
            list[str],
            int,
            int | None,
            str | None,
        ]
    ] = []

    try:
        budget.checkpoint()
        opened_root = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or (opened_root.st_dev, opened_root.st_ino) != expected_root_identity
        ):
            raise WorkerExecutionError(f"{label} changed during snapshot capture")
        initial_root_identity = _stable_file_identity(opened_root)
        entries.append((".", initial_root_identity))
        frames.append(
            (
                root_descriptor,
                (),
                initial_root_identity,
                names_in(root_descriptor),
                0,
                None,
                None,
            )
        )

        while frames:
            budget.checkpoint()
            descriptor, prefix, identity, names, index, parent, parent_name = frames[-1]
            if index >= len(names):
                after = os.fstat(descriptor)
                if _stable_file_identity(after) != identity:
                    raise WorkerExecutionError(f"{label} changed during snapshot capture")
                if parent is not None and parent_name is not None:
                    named_after = os.stat(
                        parent_name,
                        dir_fd=parent,
                        follow_symlinks=False,
                    )
                    if _stable_file_identity(named_after) != identity:
                        raise WorkerExecutionError(f"{label} changed during snapshot capture")
                frames.pop()
                if descriptor != root_descriptor:
                    os.close(descriptor)
                    open_descriptors.remove(descriptor)
                continue

            name = names[index]
            frames[-1] = (
                descriptor,
                prefix,
                identity,
                names,
                index + 1,
                parent,
                parent_name,
            )
            if not prefix and name in skip_top_level:
                continue
            if len(entries) >= MAX_SNAPSHOT_MANIFEST_ENTRIES:
                raise WorkerExecutionError(f"{label} exceeds its manifest entry limit")
            relative_parts = (*prefix, name)
            if len(relative_parts) > MAX_SNAPSHOT_DEPTH:
                raise WorkerExecutionError(f"{label} exceeds its directory depth limit")
            relative = "/".join(relative_parts)
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            identity = _stable_file_identity(before)
            entries.append((relative, identity))
            if not stat.S_ISDIR(before.st_mode):
                continue
            child_descriptor = os.open(name, directory_flags, dir_fd=descriptor)
            open_descriptors.add(child_descriptor)
            opened = os.fstat(child_descriptor)
            if _stable_file_identity(opened) != identity:
                raise WorkerExecutionError(f"{label} changed during snapshot capture")
            frames.append(
                (
                    child_descriptor,
                    relative_parts,
                    identity,
                    names_in(child_descriptor),
                    0,
                    descriptor,
                    name,
                )
            )

        budget.checkpoint()
        descriptor_after = os.fstat(root_descriptor)
        named_after = root.lstat()
        if (
            _stable_file_identity(descriptor_after) != initial_root_identity
            or _stable_file_identity(named_after) != initial_root_identity
        ):
            raise WorkerExecutionError(f"{label} changed during snapshot capture")
    except OSError as exc:
        raise WorkerExecutionError(f"{label} changed during snapshot capture") from exc
    finally:
        for descriptor in tuple(open_descriptors):
            os.close(descriptor)
    return tuple(entries)


def _read_bounded_regular_path(
    path: Path,
    maximum_bytes: int,
    label: str,
    *,
    budget: OperationBudget | None = None,
) -> bytes:
    if budget is not None:
        budget.checkpoint()
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"{label} contains a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if before.st_nlink != 1:
        raise ValueError(f"{label} must not be hard-linked")
    if before.st_size > maximum_bytes:
        raise ValueError(f"{label} exceeds its size limit")
    if budget is not None and before.st_size > budget.remaining_bytes:
        raise WorkerExecutionError(
            "read-only inspection exceeded its cumulative snapshot byte limit"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if _stable_file_identity(opened) != _stable_file_identity(before):
            raise ValueError(f"{label} changed while it was opened")
        content = bytearray()
        while len(content) <= maximum_bytes:
            if budget is not None:
                budget.checkpoint()
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - len(content)))
            if not chunk:
                break
            if budget is not None:
                budget.consume(len(chunk))
            content.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        named_after = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} changed while it was read") from exc
    if (
        len(content) > maximum_bytes
        or _stable_file_identity(after) != _stable_file_identity(before)
        or _stable_file_identity(named_after) != _stable_file_identity(before)
    ):
        raise ValueError(f"{label} changed while it was read")
    if budget is not None:
        budget.checkpoint()
    return bytes(content)


def _read_contained_regular_file(
    root: Path,
    relative: str,
    *,
    allow_missing: bool,
    expected_root_identity: tuple[int, int],
    budget: OperationBudget,
    maximum_bytes: int = MAX_SNAPSHOT_FILE_BYTES,
) -> SnapshotFile | None:
    budget.checkpoint()
    parts = Path(relative).parts
    if not parts:
        raise ValueError("review snapshot path is invalid")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        current_descriptor = os.open(root, directory_flags)
    except OSError as exc:
        raise ValueError("review snapshot root changed or contains a symbolic link") from exc
    try:
        opened_root = os.fstat(current_descriptor)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or (opened_root.st_dev, opened_root.st_ino) != expected_root_identity
        ):
            raise ValueError("review snapshot root changed during inspection")
        for part in parts[:-1]:
            try:
                next_descriptor = os.open(part, directory_flags, dir_fd=current_descriptor)
            except OSError as exc:
                raise ValueError("review path changed or contains a symbolic link") from exc
            os.close(current_descriptor)
            current_descriptor = next_descriptor
        name = parts[-1]
        try:
            before = os.stat(name, dir_fd=current_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise ValueError("review file is unavailable") from None
        except OSError as exc:
            raise ValueError("review file is unavailable") from exc
        if stat.S_ISLNK(before.st_mode):
            raise ValueError("review path contains a symbolic link")
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("review snapshot accepts only regular files")
        if before.st_nlink != 1:
            raise ValueError("review snapshot rejects hard-linked files")
        if before.st_size > maximum_bytes:
            raise ValueError("review file exceeds its size limit")
        if before.st_size > budget.remaining_bytes:
            raise WorkerExecutionError(
                "read-only inspection exceeded its cumulative snapshot byte limit"
            )
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            file_descriptor = os.open(name, file_flags, dir_fd=current_descriptor)
        except OSError as exc:
            raise ValueError("review path changed or contains a symbolic link") from exc
        try:
            opened = os.fstat(file_descriptor)
            if _stable_file_identity(opened) != _stable_file_identity(before):
                raise ValueError("review file changed while it was opened")
            content = bytearray()
            while len(content) <= maximum_bytes:
                budget.checkpoint()
                chunk = os.read(
                    file_descriptor,
                    min(65_536, maximum_bytes + 1 - len(content)),
                )
                if not chunk:
                    break
                budget.consume(len(chunk))
                content.extend(chunk)
            after = os.fstat(file_descriptor)
        finally:
            os.close(file_descriptor)
        try:
            named_after = os.stat(name, dir_fd=current_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("review file changed while it was read") from exc
        if (
            len(content) > maximum_bytes
            or _stable_file_identity(after) != _stable_file_identity(before)
            or _stable_file_identity(named_after) != _stable_file_identity(before)
        ):
            raise ValueError("review file changed while it was read")
        budget.checkpoint()
        return SnapshotFile(bytes(content), stat.S_IMODE(before.st_mode))
    finally:
        os.close(current_descriptor)


def _write_private_snapshot_file(
    root: Path,
    relative: str,
    source: SnapshotFile,
    *,
    budget: OperationBudget,
) -> None:
    budget.checkpoint()
    destination = root / relative
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination_mode = 0o700 if source.mode & stat.S_IXUSR else 0o600
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, destination_mode)
    try:
        view = memoryview(source.content)
        written = 0
        while written < len(view):
            budget.checkpoint()
            written += os.write(descriptor, view[written:])
        os.fchmod(descriptor, destination_mode)
    finally:
        os.close(descriptor)


def _validate_static_file(
    root: Path,
    relative: str,
    *,
    budget: OperationBudget | None = None,
) -> None:
    if budget is not None:
        budget.checkpoint()
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
    if target.stat().st_nlink != 1:
        raise ValueError("static analysis rejects hard-linked files")
    if budget is not None:
        budget.checkpoint()


def parse_review_job(
    root: Path,
    job: dict[str, Any],
    *,
    budget: OperationBudget | None = None,
) -> ReviewRequest:
    if budget is not None:
        budget.checkpoint()
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
        _validate_static_file(root, path, budget=budget)
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
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_COUNT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }


def _validate_local_git_configuration(
    git_dir: Path,
    *,
    budget: OperationBudget | None = None,
) -> None:
    for name in ("config", "config.worktree"):
        if budget is not None:
            budget.checkpoint()
        path = git_dir / name
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("review root Git configuration is unavailable") from exc
        try:
            configuration = _read_bounded_regular_path(
                path,
                MAX_GIT_CONFIG_BYTES,
                "review root Git configuration",
                budget=budget,
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("review root Git configuration is invalid") from exc
        if (
            "\0" in configuration
            or "\ufeff" in configuration
            or EXECUTABLE_GIT_SECTION_RE.search(configuration)
        ):
            raise ValueError("review root contains executable Git configuration")


def _validate_git_metadata(
    root: Path,
    *,
    budget: OperationBudget | None = None,
) -> Path:
    if budget is not None:
        budget.checkpoint()
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
        git_dir / "info" / "grafts",
    ):
        if forbidden.exists() or forbidden.is_symlink():
            raise ValueError("review root uses alternate object or Git metadata storage")
    visited = 0
    for directory, names, files in os.walk(git_dir, followlinks=False):
        if budget is not None:
            budget.checkpoint()
        base = Path(directory)
        try:
            depth = len(base.relative_to(git_dir).parts)
        except ValueError as exc:  # pragma: no cover - os.walk is rooted at git_dir
            raise ValueError("review root Git metadata escaped its root") from exc
        if depth > MAX_SNAPSHOT_DEPTH:
            raise ValueError("review root Git metadata exceeds its directory depth limit")
        visited += len(names) + len(files)
        if visited > MAX_SNAPSHOT_MANIFEST_ENTRIES:
            raise ValueError("review root Git metadata exceeds its entry limit")
        for name in (*names, *files):
            if budget is not None:
                budget.checkpoint()
            if (base / name).is_symlink():
                raise ValueError("review root Git metadata contains a symbolic link")
    _validate_local_git_configuration(git_dir, budget=budget)
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
        if not 0 < timeout_seconds <= 60:
            raise ValueError("review timeout must be between 0 and 60 seconds")
        startup_budget = OperationBudget(timeout_seconds, MAX_SNAPSHOT_TOTAL_BYTES)
        git_dir = _validate_git_metadata(root, budget=startup_budget)
        if not Path(git_binary).is_absolute() or Path(git_binary).name != "git":
            raise ValueError("git executable must be an absolute git path")
        if not Path(ruff_binary).is_absolute() or Path(ruff_binary).name != "ruff":
            raise ValueError("Ruff executable must be an absolute ruff path")
        self.root = root
        self.git_dir = git_dir
        self.root_identity = _directory_identity(root, "review root")
        self.git_dir_identity = _directory_identity(git_dir, "review root Git metadata")
        self.git_binary = git_binary
        self.ruff_binary = ruff_binary
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.environment = _safe_subprocess_environment()

    def _source_manifest(
        self,
        *,
        include_worktree: bool,
        budget: OperationBudget,
    ) -> tuple[tuple[str, tuple[int, int, int, int, int, int]], ...]:
        """Return one bounded generation token for every tracked live input."""

        self._require_repository_identity(budget)
        git_entries = _tree_manifest(
            self.git_dir,
            expected_root_identity=self.git_dir_identity,
            label="review Git metadata",
            budget=budget,
        )
        namespaced = [(f"git/{path}", identity) for path, identity in git_entries]
        if include_worktree:
            worktree_entries = _tree_manifest(
                self.root,
                expected_root_identity=self.root_identity,
                label="review worktree",
                budget=budget,
                skip_top_level=frozenset({".git"}),
            )
            namespaced.extend((f"worktree/{path}", identity) for path, identity in worktree_entries)
        self._require_repository_identity(budget)
        return tuple(namespaced)

    def _require_source_manifest(
        self,
        expected: tuple[tuple[str, tuple[int, int, int, int, int, int]], ...],
        *,
        include_worktree: bool,
        budget: OperationBudget,
    ) -> None:
        current = self._source_manifest(
            include_worktree=include_worktree,
            budget=budget,
        )
        if current != expected:
            raise WorkerExecutionError("review inputs changed during snapshot capture")

    def _require_repository_identity(self, budget: OperationBudget) -> None:
        budget.checkpoint()
        try:
            root_identity = _directory_identity(self.root, "review root")
            git_dir = _validate_git_metadata(self.root, budget=budget)
            git_dir_identity = _directory_identity(git_dir, "review root Git metadata")
        except ValueError as exc:
            raise WorkerExecutionError("review repository changed during inspection") from exc
        if root_identity != self.root_identity or git_dir_identity != self.git_dir_identity:
            raise WorkerExecutionError("review repository changed during inspection")

    def _run(
        self,
        arguments: list[str],
        *,
        budget: OperationBudget,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> CommandResult:
        self._require_repository_identity(budget)
        command_environment = dict(self.environment)
        if environment is not None:
            command_environment.update(environment)
        result = self.runner(
            arguments,
            cwd=self.root if cwd is None else cwd,
            timeout_seconds=budget.remaining_seconds(),
            max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
            env=command_environment,
        )
        budget.checkpoint()
        self._require_repository_identity(budget)
        return result

    def _git_prefix(self, *, work_tree: Path | None = None) -> list[str]:
        selected_work_tree = self.root if work_tree is None else work_tree
        return [
            self.git_binary,
            f"--git-dir={self.git_dir}",
            f"--work-tree={selected_work_tree}",
            "-c",
            f"core.worktree={selected_work_tree}",
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

    def _snapshot_index(
        self,
        destination: Path,
        budget: OperationBudget,
    ) -> Path | None:
        self._require_repository_identity(budget)
        source = _read_contained_regular_file(
            self.git_dir,
            "index",
            allow_missing=True,
            expected_root_identity=self.git_dir_identity,
            budget=budget,
            maximum_bytes=MAX_SNAPSHOT_FILE_BYTES,
        )
        if source is None:
            return None
        _write_private_snapshot_file(destination, "index", source, budget=budget)
        self._require_repository_identity(budget)
        return destination / "index"

    def _snapshot_worktree(
        self,
        destination: Path,
        paths: list[str] | tuple[str, ...],
        *,
        budget: OperationBudget,
        allow_missing: bool = True,
    ) -> None:
        budget.checkpoint()
        destination.mkdir(mode=0o700)
        self._require_repository_identity(budget)
        for relative in paths:
            source = _read_contained_regular_file(
                self.root,
                relative,
                allow_missing=allow_missing,
                expected_root_identity=self.root_identity,
                budget=budget,
            )
            if source is not None:
                _write_private_snapshot_file(
                    destination,
                    relative,
                    source,
                    budget=budget,
                )
        self._require_repository_identity(budget)

    def _resolve_head(self, *, required: bool, budget: OperationBudget) -> str | None:
        result = self._run(
            self._git_prefix() + ["rev-parse", "--verify", "HEAD^{commit}"],
            budget=budget,
        )
        if result.exit_code != 0 or result.truncated:
            if required:
                raise WorkerExecutionError("review HEAD could not be pinned")
            return None
        revision = result.output.strip()
        if FULL_COMMIT_RE.fullmatch(revision) is None:
            raise WorkerExecutionError("review HEAD returned an invalid object id")
        return revision.lower()

    @staticmethod
    def _require_git_success(result: CommandResult) -> None:
        if result.exit_code != 0 and not result.truncated:
            raise WorkerExecutionError("read-only Git inspection failed")

    def _status(self, budget: OperationBudget) -> dict[str, Any]:
        source_manifest = self._source_manifest(
            include_worktree=True,
            budget=budget,
        )
        command = self._git_prefix() + [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
            "--ignore-submodules=all",
        ]
        command_result = self._run(command, budget=budget)
        self._require_source_manifest(
            source_manifest,
            include_worktree=True,
            budget=budget,
        )
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

    def _discover(
        self,
        command: list[str],
        *,
        budget: OperationBudget,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> tuple[list[str], int, bool]:
        result = self._run(
            command,
            budget=budget,
            cwd=cwd,
            environment=environment,
        )
        self._require_git_success(result)
        paths, omitted, selection_truncated = _safe_discovered_paths(result.output)
        return paths, omitted, result.truncated or selection_truncated

    def _diff(self, request: ReviewRequest, budget: OperationBudget) -> dict[str, Any]:
        options = [
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--ignore-submodules=all",
        ]
        if request.staged:
            options.append("--cached")
        with tempfile.TemporaryDirectory(prefix="mongars-review-") as temporary_name:
            # Keep discovery, index capture, HEAD resolution, and selected file
            # capture in one checked live-source generation. The command below
            # then reads only the private worktree/index plus content-addressed
            # Git objects.
            source_manifest = self._source_manifest(
                include_worktree=not request.staged,
                budget=budget,
            )
            temporary = Path(temporary_name)
            index_path = self._snapshot_index(temporary, budget)
            environment = {"GIT_INDEX_FILE": str(index_path)} if index_path is not None else None
            pinned_base = (
                self._resolve_head(required=False, budget=budget) if request.staged else None
            )
            base_arguments = [pinned_base] if pinned_base is not None else []
            discovery = self._git_prefix() + [
                "diff",
                *options,
                "--name-only",
                "-z",
                *base_arguments,
                "--",
                *request.paths,
            ]
            paths, omitted, selection_truncated = self._discover(
                discovery,
                budget=budget,
                environment=environment,
            )
            if not paths:
                self._require_source_manifest(
                    source_manifest,
                    include_worktree=not request.staged,
                    budget=budget,
                )
                result: dict[str, Any] = {
                    "content_trust": "untrusted",
                    "output": "",
                    "exit_code": 0,
                    "protected_entries_omitted": omitted,
                    "truncated": selection_truncated,
                }
                return result
            work_tree = self.root
            command_cwd = self.root
            if not request.staged:
                work_tree = temporary / "worktree"
                self._snapshot_worktree(work_tree, paths, budget=budget)
                command_cwd = work_tree
            self._require_source_manifest(
                source_manifest,
                include_worktree=not request.staged,
                budget=budget,
            )
            command = self._git_prefix(work_tree=work_tree) + [
                "diff",
                *options,
                f"--unified={request.context_lines}",
                *base_arguments,
                "--",
                *paths,
            ]
            # Object files remain in the operator-mounted repository. Fence
            # their complete metadata generation around the immutable-OID read
            # so a same-inode pack/loose-object mutation cannot go unnoticed.
            object_manifest = self._source_manifest(
                include_worktree=False,
                budget=budget,
            )
            command_result = self._run(
                command,
                budget=budget,
                cwd=command_cwd,
                environment=environment,
            )
            self._require_source_manifest(
                object_manifest,
                include_worktree=False,
                budget=budget,
            )
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

    def _show(self, request: ReviewRequest, budget: OperationBudget) -> dict[str, Any]:
        if request.revision is None:
            raise ValueError("show revision is required")
        source_manifest = self._source_manifest(
            include_worktree=False,
            budget=budget,
        )
        revision = (
            self._resolve_head(required=True, budget=budget)
            if request.revision == "HEAD"
            else request.revision.lower()
        )
        if revision is None:  # pragma: no cover - required resolution cannot return None
            raise WorkerExecutionError("review HEAD could not be pinned")
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
            revision,
            "--",
            *request.paths,
        ]
        paths, omitted, selection_truncated = self._discover(
            discovery,
            budget=budget,
        )
        if not paths:
            self._require_source_manifest(
                source_manifest,
                include_worktree=False,
                budget=budget,
            )
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
            revision,
            "--",
            *paths,
        ]
        command_result = self._run(command, budget=budget)
        self._require_source_manifest(
            source_manifest,
            include_worktree=False,
            budget=budget,
        )
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

    def _static_analysis(
        self,
        request: ReviewRequest,
        budget: OperationBudget,
    ) -> dict[str, Any]:
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
        with tempfile.TemporaryDirectory(prefix="mongars-review-") as temporary_name:
            source_manifest = self._source_manifest(
                include_worktree=True,
                budget=budget,
            )
            snapshot_root = Path(temporary_name) / "worktree"
            self._snapshot_worktree(
                snapshot_root,
                request.paths,
                budget=budget,
                allow_missing=False,
            )
            self._require_source_manifest(
                source_manifest,
                include_worktree=True,
                budget=budget,
            )
            command_result = self._run(command, budget=budget, cwd=snapshot_root)
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
        budget = OperationBudget(self.timeout_seconds, MAX_SNAPSHOT_TOTAL_BYTES)
        request = parse_review_job(self.root, job, budget=budget)
        if request.skill == "code_review.git_status":
            return self._status(budget)
        if request.skill == "code_review.git_diff":
            return self._diff(request, budget)
        if request.skill == "code_review.git_show":
            return self._show(request, budget)
        return self._static_analysis(request, budget)


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
    if isinstance(exc, (MemoryError, RecursionError)):
        return "read-only inspection exceeded its resource limits"
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
        except (
            MemoryError,
            OSError,
            RecursionError,
            TypeError,
            ValueError,
            WorkerExecutionError,
        ) as exc:
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
