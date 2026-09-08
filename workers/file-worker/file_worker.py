#!/usr/bin/env python3
"""Minimal read-only remote worker for the monGARS worker protocol."""

from __future__ import annotations

import argparse
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
from urllib.parse import quote

PROTECTED_PARTS = frozenset({".env", ".git", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"})
CAPABILITY_TERMINAL_STATUSES = frozenset({"completed", "denied", "failed", "expired", "cancelled"})
LOGGER = logging.getLogger("mongars.file_worker")


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
) -> Any:
    encoded = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=encoded, method=method, headers=headers
    )
    with urllib.request.urlopen(call, timeout=30) as response:  # nosec B310 - operator-configured control plane
        raw = response.read()
    return json.loads(raw) if raw else None


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
    ) -> None:
        self.base_url = base_url
        self.agent_id = agent_id
        self.credential = credential
        self._request = request if request_fn is None else request_fn

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
            self._thread.join()


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


def safe_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or "\0" in relative:
        raise ValueError("protected or invalid path")
    if ".." in candidate.parts:
        raise ValueError("path escapes worker root")
    if any(protected_name(part) for part in candidate.parts):
        raise ValueError("protected or invalid path")
    root = root.resolve(strict=True)
    unresolved = root / candidate
    cursor = root
    for part in candidate.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("symbolic links are not available to the worker")
    target = unresolved.resolve(strict=True)
    if target != root and root not in target.parents:
        raise ValueError("path escapes worker root")
    relative_target = target.relative_to(root)
    if any(protected_name(part) for part in relative_target.parts):
        raise ValueError("protected or invalid path")
    return target


def read_safe_text(path: Path) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
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


def execute(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    skill = job["required_skill"]
    payload = job.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise TypeError("job payload must be an object")
    path = safe_path(root, str(payload.get("path", ".")))
    if skill == "workspace.list_dir":
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return {
            "entries": sorted(
                entry.name
                for entry in path.iterdir()
                if not entry.is_symlink() and not protected_name(entry.name)
            )
        }
    if skill == "workspace.read_text":
        return {"content": read_safe_text(path)}
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
