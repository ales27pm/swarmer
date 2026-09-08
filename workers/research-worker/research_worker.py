#!/usr/bin/env python3
"""Lease-aware research worker with one fixed, operator-configured adapter."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

LOGGER = logging.getLogger("mongars.research_worker")

RESEARCH_SKILL = "research.query"
MAX_QUERY_CHARACTERS = 2_000
MAX_RESULTS = 10
MAX_TITLE_CHARACTERS = 300
MAX_URL_CHARACTERS = 2_048
MAX_SNIPPET_CHARACTERS = 4_000
MAX_ADAPTER_REQUEST_BYTES = 16_384
MAX_ADAPTER_RESPONSE_BYTES = 262_144
MAX_CONTROL_RESPONSE_BYTES = 1_000_000
MAX_JOB_RESULT_BYTES = 524_288

ControlRequest = Callable[[str, str, str, str, dict[str, Any] | None], Any]
ResearchTransport = Callable[[str, str, dict[str, Any], float], Any]


class WorkerProtocolError(RuntimeError):
    """The control plane returned data outside the worker protocol."""


class LeaseLost(RuntimeError):
    """The control plane rejected the current lease proof."""


class LeaseUnavailable(RuntimeError):
    """The worker cannot establish that its lease remains current."""


class ControlPlaneUnavailable(RuntimeError):
    """A control-plane request failed without a known terminal outcome."""


class ResearchAdapterError(RuntimeError):
    """The dedicated adapter failed or returned an invalid response."""


class ResearchQuery:
    __slots__ = ("max_results", "query")

    def __init__(self, query: str, max_results: int) -> None:
        self.query = query
        self.max_results = max_results


class LeaseProof:
    """Opaque proof copied from a claim and echoed on job-scoped requests."""

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


def _read_bounded_json(response: Any, maximum_bytes: int, source: str) -> Any:
    raw = response.read(maximum_bytes + 1)
    if len(raw) > maximum_bytes:
        raise WorkerProtocolError(f"{source} response exceeded its size limit")
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError as exc:
        raise WorkerProtocolError(f"{source} returned invalid JSON") from exc


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
    call = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=encoded, method=method, headers=headers
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    # This destination is operator-configured control-plane policy, never job input.
    with opener.open(call, timeout=30) as response:  # nosec B310
        return _read_bounded_json(response, MAX_CONTROL_RESPONSE_BYTES, "control plane")


class ControlPlaneClient:
    """Authenticated adapter for the existing monGARS worker endpoints."""

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
    """Renew a lease in the background and surface any loss to foreground work."""

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
            name=f"research-lease-heartbeat-{self.job_id}",
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


def parse_research_job(job: dict[str, Any]) -> ResearchQuery:
    if job.get("required_skill") != RESEARCH_SKILL:
        raise ValueError("unsupported worker skill")
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise TypeError("job payload must be an object")
    if set(payload) - {"query", "max_results"}:
        raise ValueError("research payload contains unsupported fields")
    query = payload.get("query")
    if not isinstance(query, str):
        raise TypeError("research query must be a string")
    query = query.strip()
    if not query or len(query) > MAX_QUERY_CHARACTERS:
        raise ValueError("research query is empty or too long")
    max_results = payload.get("max_results", 5)
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or not 1 <= max_results <= MAX_RESULTS
    ):
        raise ValueError("max_results must be an integer between 1 and 10")
    return ResearchQuery(query, max_results)


def validate_adapter_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path
    ):
        raise ValueError("research adapter endpoint must be a credential-free HTTPS URL")
    return endpoint


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
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


def research_adapter_request(
    endpoint: str,
    token: str,
    body: dict[str, Any],
    timeout_seconds: float,
) -> Any:
    encoded = json.dumps(
        body,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_ADAPTER_REQUEST_BYTES:
        raise ResearchAdapterError("research adapter request exceeded its size limit")
    request = urllib.request.Request(
        endpoint,
        data=encoded,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        # This destination is the validated, operator-configured adapter only.
        with opener.open(request, timeout=timeout_seconds) as response:  # nosec B310
            if response.geturl() != endpoint:
                raise ResearchAdapterError("research adapter redirects are not allowed")
            return _read_bounded_json(response, MAX_ADAPTER_RESPONSE_BYTES, "research adapter")
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:
        raise ResearchAdapterError("research adapter request failed") from exc
    except WorkerProtocolError as exc:
        raise ResearchAdapterError(str(exc)) from exc


def normalize_adapter_response(response: Any, max_results: int) -> dict[str, Any]:
    if not isinstance(response, dict) or set(response) != {"results"}:
        raise TypeError("research adapter response must contain only results")
    results = response["results"]
    if not isinstance(results, list) or len(results) > max_results:
        raise ValueError("research adapter returned too many results")
    normalized: list[dict[str, str]] = []
    for item in results:
        if not isinstance(item, dict) or set(item) != {"title", "url", "snippet"}:
            raise TypeError("research result has an invalid shape")
        title = item["title"]
        url = item["url"]
        snippet = item["snippet"]
        if not isinstance(title, str) or not title or len(title) > MAX_TITLE_CHARACTERS:
            raise ValueError("research result title is invalid")
        if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARACTERS:
            raise ValueError("research result URL is invalid")
        parsed_url = urlsplit(url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise ValueError("research result URL is invalid")
        if not isinstance(snippet, str) or len(snippet) > MAX_SNIPPET_CHARACTERS:
            raise ValueError("research result snippet is invalid")
        normalized.append({"title": title, "url": url, "snippet": snippet})
    result: dict[str, Any] = {"content_trust": "untrusted", "results": normalized}
    ensure_result_size(result)
    return result


class ResearchAdapterClient:
    """The worker's only non-control-plane network capability."""

    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        timeout_seconds: float = 20,
        transport: ResearchTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("research adapter credential is required")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("research adapter timeout must be between 0 and 60 seconds")
        self.endpoint = validate_adapter_endpoint(endpoint)
        self._token = token
        self.timeout_seconds = timeout_seconds
        self._transport = research_adapter_request if transport is None else transport

    def query(self, request: ResearchQuery) -> dict[str, Any]:
        response = self._transport(
            self.endpoint,
            self._token,
            {"query": request.query, "max_results": request.max_results},
            self.timeout_seconds,
        )
        return normalize_adapter_response(response, request.max_results)


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


def execute(adapter: ResearchAdapterClient, job: dict[str, Any]) -> dict[str, Any]:
    result = adapter.query(parse_research_job(job))
    ensure_result_contract(result)
    return result


def run_once(
    base_url: str,
    agent_id: str,
    credential: str,
    adapter: ResearchAdapterClient,
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
            result = execute(adapter, job)
            result_body: dict[str, Any] = {"status": "completed", "result": result}
        except (ResearchAdapterError, TypeError, ValueError) as exc:
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
        except (ControlPlaneUnavailable, WorkerProtocolError, LeaseLost):
            LOGGER.warning("could not return agent status to online", exc_info=True)
    return True


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
    adapter = ResearchAdapterClient(
        os.environ["MONGARS_RESEARCH_ADAPTER_URL"],
        os.environ["MONGARS_RESEARCH_ADAPTER_TOKEN"],
        timeout_seconds=_bounded_float(
            os.environ.get("MONGARS_RESEARCH_TIMEOUT_SECONDS", "20"),
            "research timeout",
            1,
            60,
        ),
    )
    heartbeat_seconds = _bounded_float(
        os.environ.get("MONGARS_JOB_HEARTBEAT_SECONDS", "10"),
        "job heartbeat interval",
        0.1,
        60,
    )
    while True:
        worked = run_once(
            os.environ["MONGARS_SERVER_URL"],
            os.environ["MONGARS_AGENT_ID"],
            os.environ["MONGARS_AGENT_CREDENTIAL"],
            adapter,
            heartbeat_interval_seconds=heartbeat_seconds,
        )
        if args.once:
            return
        time.sleep(1 if worked else 5)


if __name__ == "__main__":
    main()
