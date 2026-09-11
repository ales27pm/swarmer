#!/usr/bin/env python3
"""Minimal read-only remote worker for the monGARS worker protocol."""

from __future__ import annotations

import argparse
import functools
import ipaddress
import json
import logging
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

PROTECTED_PARTS = frozenset({".env", ".git", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"})
CAPABILITY_TERMINAL_STATUSES = frozenset({"completed", "denied", "failed", "expired", "cancelled"})
LOGGER = logging.getLogger("mongars.file_worker")
MAX_CONTROL_RESPONSE_BYTES = 1_000_000
HEARTBEAT_JOIN_TIMEOUT_SECONDS = 1.0
SUPPORTS_DESCRIPTOR_TRAVERSAL = os.open in os.supports_dir_fd


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


def _read_control_response(
    response: Any, max_response_bytes: int = MAX_CONTROL_RESPONSE_BYTES
) -> Any:
    raw = response.read(max_response_bytes + 1)
    if len(raw) > max_response_bytes:
        raise WorkerProtocolError("control-plane response exceeded its size limit")
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError as exc:
        raise WorkerProtocolError("control plane returned invalid JSON") from exc


def protected_name(name: str) -> bool:
    folded = name.casefold()
    return bool(
        folded in PROTECTED_PARTS
        or folded.startswith(".")
        or "secret" in folded
        or "token" in folded
        or "credential" in folded
        or "password" in folded
        or "passwd" in folded
        or folded.endswith((".pem", ".key"))
    )


def request(
    base_url: str,
    path: str,
    token: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    *,
    max_response_bytes: int = MAX_CONTROL_RESPONSE_BYTES,
) -> Any:
    base_url = validate_control_plane_origin(base_url)
    encoded = (
        json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        if body is not None
        else None
    )
    headers = {"Authorization": f"Bearer {token}"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=encoded, method=method, headers=headers
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    # This is the strictly validated operator-configured origin, never job input.
    with opener.open(call, timeout=30) as response:  # nosec B310
        return _read_control_response(response, max_response_bytes)


class WorkerProtocolError(RuntimeError):
    """The control plane returned a response that does not match the worker contract."""


class LeaseLost(RuntimeError):
    """The control plane rejected the proof for an active job lease."""


class LeaseUnavailable(RuntimeError):
    """The worker can no longer prove that its job lease is current."""


class ControlPlaneUnavailable(RuntimeError):
    """A control-plane request failed before its outcome could be established."""


class CapabilityRequestFailed(ValueError):
    """A requested iPhone capability reached a non-success terminal state."""


class LeaseProof:
    """Opaque proof copied from a claim and echoed on every job-scoped request."""

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


class ControlPlaneClient:
    """Small, replaceable adapter for the authenticated worker control-plane API."""

    def __init__(
        self,
        base_url: str,
        agent_id: str,
        credential: str,
        request_fn: Any | None = None,
        *,
        max_response_bytes: int = MAX_CONTROL_RESPONSE_BYTES,
    ) -> None:
        self.base_url = validate_control_plane_origin(base_url)
        self.agent_id = agent_id
        self.credential = credential
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= 4_000_000
        ):
            raise ValueError("control-plane response limit must be between 1 and 4000000 bytes")
        self._request = request if request_fn is None else request_fn
        if request_fn is None and max_response_bytes != MAX_CONTROL_RESPONSE_BYTES:
            self._request = functools.partial(request, max_response_bytes=max_response_bytes)

    @staticmethod
    def _segment(value: str) -> str:
        return quote(value, safe="")

    def agent_heartbeat_path(self) -> str:
        return f"/agents/{self._segment(self.agent_id)}/heartbeat"

    def claim_path(self) -> str:
        return f"/agents/{self._segment(self.agent_id)}/claim"

    def job_heartbeat_path(self, job_id: str) -> str:
        return f"/agents/{self._segment(self.agent_id)}/jobs/{self._segment(job_id)}/heartbeat"

    def job_result_path(self, job_id: str) -> str:
        return f"/agents/{self._segment(self.agent_id)}/jobs/{self._segment(job_id)}/result"

    def capability_requests_path(self, job_id: str) -> str:
        return (
            f"/agents/{self._segment(self.agent_id)}/jobs/"
            f"{self._segment(job_id)}/capability-requests"
        )

    def capability_poll_path(self, job_id: str, request_id: str) -> str:
        return f"{self.capability_requests_path(job_id)}/{self._segment(request_id)}/poll"

    def _call(
        self,
        path: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            return self._request(self.base_url, path, self.credential, method, body)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ControlPlaneUnavailable("control-plane request failed") from exc
        except json.JSONDecodeError as exc:
            raise WorkerProtocolError("control plane returned invalid JSON") from exc

    def _leased_call(self, path: str, method: str, body: dict[str, Any]) -> Any:
        try:
            return self._request(self.base_url, path, self.credential, method, body)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise LeaseLost("job lease is stale or no longer owned by this worker") from exc
            raise ControlPlaneUnavailable("control-plane request failed") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ControlPlaneUnavailable("control-plane request failed") from exc
        except json.JSONDecodeError as exc:
            raise WorkerProtocolError("control plane returned invalid JSON") from exc

    @staticmethod
    def _object(response: Any, operation: str) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise WorkerProtocolError(f"{operation} returned a non-object response")
        return response

    def heartbeat_agent(self, status: str) -> None:
        self._call(self.agent_heartbeat_path(), "POST", {"status": status})

    def claim(self) -> dict[str, Any] | None:
        response = self._call(self.claim_path(), "POST", {"wait_seconds": 0})
        if response is None:
            return None
        return self._object(response, "job claim")

    def heartbeat_job(self, job_id: str, lease: LeaseProof) -> dict[str, Any]:
        response = self._leased_call(self.job_heartbeat_path(job_id), "POST", lease.body())
        return self._object(response, "job heartbeat")

    def submit_result(
        self, job_id: str, lease: LeaseProof, result_body: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._leased_call(
            self.job_result_path(job_id),
            "POST",
            {**result_body, **lease.body()},
        )
        return self._object(response, "job result")

    def create_capability_request(
        self,
        job_id: str,
        lease: LeaseProof,
        capability_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._leased_call(
            self.capability_requests_path(job_id),
            "POST",
            {
                **lease.body(),
                "capability_name": capability_name,
                "arguments": arguments,
            },
        )
        return self._object(response, "capability request creation")

    def poll_capability_request(
        self, job_id: str, lease: LeaseProof, request_id: str
    ) -> dict[str, Any]:
        response = self._leased_call(
            self.capability_poll_path(job_id, request_id), "POST", lease.body()
        )
        return self._object(response, "capability request poll")


class LeaseHeartbeat:
    """Renews one lease in the background and exposes any loss to foreground work."""

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
        self._failure_lock = threading.Lock()
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
            name=f"lease-heartbeat-{self.job_id}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.client.heartbeat_job(self.job_id, self.lease)
            except (LeaseLost, ControlPlaneUnavailable, WorkerProtocolError) as exc:
                with self._failure_lock:
                    self._failure = exc
                self._stop.set()
                return

    def ensure_active(self) -> None:
        with self._failure_lock:
            failure = self._failure
        if failure is None:
            return
        if isinstance(failure, LeaseLost):
            raise failure
        raise LeaseUnavailable("job lease renewal failed") from failure

    def wait(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("capability poll interval must be positive")
        self._stop.wait(seconds)
        self.ensure_active()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=HEARTBEAT_JOIN_TIMEOUT_SECONDS)


def capability_request_from_job(
    job: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    payload = job.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise TypeError("job payload must be an object")
    raw_request = payload.get("capability_request")
    if raw_request is None:
        return None
    if not isinstance(raw_request, dict):
        raise TypeError("capability_request must be an object")
    capability_name = raw_request.get("capability_name")
    arguments = raw_request.get("arguments", {})
    if not isinstance(capability_name, str):
        raise TypeError("capability_name must be a string")
    if not capability_name.startswith("iphone."):
        raise ValueError("capability_name must be an explicit iphone.* capability")
    if not isinstance(arguments, dict):
        raise TypeError("capability arguments must be an object")
    return capability_name, arguments


def await_capability_result(
    client: ControlPlaneClient,
    job_id: str,
    lease: LeaseProof,
    heartbeat: LeaseHeartbeat,
    capability_name: str,
    arguments: dict[str, Any],
    poll_interval_seconds: float,
) -> dict[str, Any]:
    state = client.create_capability_request(job_id, lease, capability_name, arguments)
    request_id = state.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise WorkerProtocolError("capability creation response is missing request_id")
    while True:
        status = state.get("status")
        if not isinstance(status, str) or not status:
            raise WorkerProtocolError("capability response is missing status")
        if status == "completed":
            return {
                "request_id": request_id,
                "status": status,
                "result": state.get("result"),
            }
        if status in CAPABILITY_TERMINAL_STATUSES:
            raise CapabilityRequestFailed(f"iPhone capability ended with status {status}")
        heartbeat.wait(poll_interval_seconds)
        state = client.poll_capability_request(job_id, lease, request_id)


def _validated_relative_parts(relative: str) -> tuple[str, ...]:
    candidate = Path(relative)
    if candidate.is_absolute() or "\0" in relative:
        raise ValueError("protected or invalid path")
    if ".." in candidate.parts:
        raise ValueError("path escapes worker root")
    if any(protected_name(part) for part in candidate.parts):
        raise ValueError("protected or invalid path")
    return tuple(part for part in candidate.parts if part != ".")


def _open_beneath(root: Path, relative: str, *, directory: bool) -> int:
    """Open a path component-by-component without ever following a symlink."""

    if not SUPPORTS_DESCRIPTOR_TRAVERSAL:
        raise RuntimeError("worker platform lacks descriptor-relative path safety")
    parts = _validated_relative_parts(relative)
    base_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(root, base_flags | os.O_DIRECTORY)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("worker root is not a directory")
        for index, part in enumerate(parts):
            flags = base_flags
            if index < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ValueError(
            "symbolic links or unavailable paths are not available to the worker"
        ) from exc
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def safe_path(root: Path, relative: str) -> Path:
    """Validate a path for callers; execution itself remains descriptor-relative."""

    parts = _validated_relative_parts(relative)
    descriptor = _open_beneath(root, relative, directory=False)
    os.close(descriptor)
    return root.absolute().joinpath(*parts)


def _read_safe_text(root: Path, relative: str) -> str:
    descriptor = _open_beneath(root, relative, directory=False)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1_000_000:
            raise ValueError("file is unavailable or too large")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _list_safe_directory(root: Path, relative: str) -> list[str]:
    descriptor = _open_beneath(root, relative, directory=True)
    try:
        entries: list[str] = []
        for name in os.listdir(descriptor):
            if protected_name(name):
                continue
            try:
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISLNK(metadata.st_mode):
                entries.append(name)
        return sorted(entries)
    finally:
        os.close(descriptor)


def execute(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    skill = job["required_skill"]
    payload = job.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise TypeError("job payload must be an object")
    relative = str(payload.get("path", "."))
    _validated_relative_parts(relative)
    if skill == "workspace.list_dir":
        return {"entries": _list_safe_directory(root, relative)}
    if skill == "workspace.read_text":
        return {"content": _read_safe_text(root, relative)}
    raise ValueError("unsupported worker skill")


def run_once(
    base_url: str,
    agent_id: str,
    credential: str,
    root: Path,
    *,
    heartbeat_interval_seconds: float = 10,
    capability_poll_seconds: float = 2,
) -> bool:
    if heartbeat_interval_seconds <= 0:
        raise ValueError("job heartbeat interval must be positive")
    if capability_poll_seconds <= 0:
        raise ValueError("capability poll interval must be positive")
    client = ControlPlaneClient(base_url, agent_id, credential)
    client.heartbeat_agent("online")
    job = client.claim()
    if not job:
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
            capability = capability_request_from_job(job)
            result = execute(root, job)
            capability_result = None
            if capability is not None:
                capability_result = await_capability_result(
                    client,
                    job_id,
                    lease,
                    heartbeat,
                    capability[0],
                    capability[1],
                    capability_poll_seconds,
                )
            if capability_result is not None:
                result["capability_result"] = capability_result
            result_body = {"status": "completed", "result": result}
        except (
            CapabilityRequestFailed,
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as exc:
            result_body = {"status": "failed", "error": str(exc)[:500]}
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
        except (ControlPlaneUnavailable, WorkerProtocolError):
            LOGGER.warning("could not return agent status to online", exc_info=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    base_url = os.environ["MONGARS_SERVER_URL"]
    agent_id = os.environ["MONGARS_AGENT_ID"]
    credential = os.environ["MONGARS_AGENT_CREDENTIAL"]
    root = Path(os.environ["MONGARS_WORKER_ROOT"])
    heartbeat_interval_seconds = float(os.environ.get("MONGARS_JOB_HEARTBEAT_SECONDS", "10"))
    capability_poll_seconds = float(os.environ.get("MONGARS_CAPABILITY_POLL_SECONDS", "2"))
    while True:
        worked = run_once(
            base_url,
            agent_id,
            credential,
            root,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            capability_poll_seconds=capability_poll_seconds,
        )
        if args.once:
            return
        time.sleep(1 if worked else 5)


if __name__ == "__main__":
    main()
