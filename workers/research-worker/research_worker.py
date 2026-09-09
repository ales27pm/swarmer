#!/usr/bin/env python3
"""Lease-aware research worker with one fixed, operator-configured adapter."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import logging
import os
import queue
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

LOGGER = logging.getLogger("mongars.research_worker")
HEARTBEAT_JOIN_TIMEOUT_SECONDS = 1.0
_DNS_RESOLVER_SLOT = threading.BoundedSemaphore(value=1)

RESEARCH_SKILL = "research.query"
MAX_QUERY_CHARACTERS = 2_000
MAX_RESULTS = 10
MAX_TITLE_CHARACTERS = 300
MAX_URL_CHARACTERS = 2_048
MAX_SNIPPET_CHARACTERS = 4_000
MAX_ADAPTER_REQUEST_BYTES = 16_384
MAX_ADAPTER_RESPONSE_BYTES = 262_144
MAX_ADAPTER_ENDPOINT_CHARACTERS = 2_048
ADAPTER_READ_CHUNK_BYTES = 16_384
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


class ResolvedAddress:
    """One validated numeric destination returned by the single DNS lookup."""

    __slots__ = ("family", "protocol", "sockaddr", "socket_type")

    def __init__(
        self,
        family: int,
        socket_type: int,
        protocol: int,
        sockaddr: tuple[Any, ...],
    ) -> None:
        self.family = family
        self.socket_type = socket_type
        self.protocol = protocol
        self.sockaddr = sockaddr


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
            self._thread.join(timeout=HEARTBEAT_JOIN_TIMEOUT_SECONDS)


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


def _normalized_ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_global_address(value: str) -> bool:
    try:
        address = _normalized_ip_address(value)
    except ValueError:
        return False
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_adapter_endpoint(endpoint: str) -> str:
    """Return the canonical, credential-free HTTPS adapter URL."""

    if (
        not isinstance(endpoint, str)
        or not endpoint
        or len(endpoint) > MAX_ADAPTER_ENDPOINT_CHARACTERS
        or endpoint != endpoint.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in endpoint)
        or "\\" in endpoint
        or "?" in endpoint
        or "#" in endpoint
    ):
        raise ValueError("research adapter endpoint must be a credential-free HTTPS URL")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("research adapter endpoint has an invalid authority") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path
        or not parsed.path.startswith("/")
        or parsed.netloc.endswith(":")
    ):
        raise ValueError("research adapter endpoint must be a credential-free HTTPS URL")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ValueError("research adapter endpoint has an invalid hostname") from exc
    if (
        not hostname
        or len(hostname) > 253
        or hostname == "localhost"
        or hostname.endswith((".", ".localhost"))
    ):
        raise ValueError("research adapter endpoint has an invalid hostname")
    try:
        literal_address = _normalized_ip_address(hostname)
    except ValueError:
        if all(character in "0123456789." for character in hostname):
            raise ValueError("research adapter endpoint has an invalid IP address") from None
        authority_host = hostname
    else:
        if not _is_global_address(str(literal_address)):
            raise ValueError("research adapter endpoint IP address must be globally routable")
        authority_host = (
            f"[{literal_address.compressed}]"
            if isinstance(literal_address, ipaddress.IPv6Address)
            else literal_address.compressed
        )
    selected_port = 443 if port is None else port
    if selected_port < 1:
        raise ValueError("research adapter endpoint has an invalid port")
    authority = authority_host if selected_port == 443 else f"{authority_host}:{selected_port}"
    return f"https://{authority}{parsed.path}"


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ResearchAdapterError("research adapter deadline exceeded")
    return remaining


def _resolve_global_addresses(
    hostname: str,
    port: int,
    deadline: float,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> list[ResolvedAddress]:
    """Resolve once, then reject the complete answer set if any address is unsafe."""

    if not _DNS_RESOLVER_SLOT.acquire(timeout=_remaining_seconds(deadline)):
        raise ResearchAdapterError("research adapter deadline exceeded during DNS")
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            try:
                answer = resolver(
                    hostname,
                    port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
            except (OSError, TypeError, ValueError) as exc:
                result_queue.put((False, exc))
            else:
                result_queue.put((True, answer))
        finally:
            _DNS_RESOLVER_SLOT.release()

    resolver_thread = threading.Thread(
        target=resolve,
        name="research-adapter-dns",
        daemon=True,
    )
    try:
        resolver_thread.start()
    except RuntimeError as exc:
        _DNS_RESOLVER_SLOT.release()
        raise ResearchAdapterError("research adapter DNS resolution could not start") from exc
    try:
        succeeded, raw_answer = result_queue.get(timeout=_remaining_seconds(deadline))
    except queue.Empty as exc:
        raise ResearchAdapterError("research adapter deadline exceeded during DNS") from exc
    if not succeeded:
        if isinstance(raw_answer, BaseException):
            raise ResearchAdapterError("research adapter DNS resolution failed") from raw_answer
        raise ResearchAdapterError("research adapter DNS resolution failed")
    if not isinstance(raw_answer, (list, tuple)) or not raw_answer:
        raise ResearchAdapterError("research adapter DNS resolution returned no addresses")

    addresses: list[ResolvedAddress] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for item in raw_answer:
        if not isinstance(item, tuple) or len(item) != 5:
            raise ResearchAdapterError("research adapter DNS resolution was invalid")
        family, socket_type, protocol, _canonical_name, raw_sockaddr = item
        if (
            family not in {socket.AF_INET, socket.AF_INET6}
            or socket_type != socket.SOCK_STREAM
            or protocol != socket.IPPROTO_TCP
            or not isinstance(raw_sockaddr, tuple)
        ):
            raise ResearchAdapterError("research adapter DNS resolution was invalid")
        if family == socket.AF_INET:
            if len(raw_sockaddr) != 2:
                raise ResearchAdapterError("research adapter DNS resolution was invalid")
            address_text, answer_port = raw_sockaddr
            extra: tuple[int, ...] = ()
        else:
            if len(raw_sockaddr) != 4:
                raise ResearchAdapterError("research adapter DNS resolution was invalid")
            address_text, answer_port, flow_info, scope_id = raw_sockaddr
            if (
                not isinstance(flow_info, int)
                or not isinstance(scope_id, int)
                or flow_info != 0
                or scope_id != 0
            ):
                raise ResearchAdapterError("research adapter DNS resolution was invalid")
            extra = (flow_info, scope_id)
        if (
            not isinstance(address_text, str)
            or not isinstance(answer_port, int)
            or isinstance(answer_port, bool)
            or answer_port != port
            or not _is_global_address(address_text)
        ):
            raise ResearchAdapterError(
                "research adapter DNS resolution included a non-global address"
            )
        normalized = _normalized_ip_address(address_text)
        if family == socket.AF_INET6 and isinstance(normalized, ipaddress.IPv4Address):
            raise ResearchAdapterError(
                "research adapter DNS resolution included a non-global address"
            )
        sockaddr: tuple[Any, ...] = (normalized.compressed, port, *extra)
        identity = family, sockaddr
        if identity in seen:
            continue
        seen.add(identity)
        addresses.append(ResolvedAddress(family, socket_type, protocol, sockaddr))
    if not addresses:
        raise ResearchAdapterError("research adapter DNS resolution returned no addresses")
    return addresses


def _verify_pinned_peer(sock: Any, address: ResolvedAddress) -> None:
    try:
        peer = sock.getpeername()
        peer_address = peer[0]
        expected = _normalized_ip_address(str(address.sockaddr[0]))
        actual = _normalized_ip_address(str(peer_address))
    except (AttributeError, IndexError, TypeError, ValueError, OSError) as exc:
        raise ResearchAdapterError("research adapter peer identity could not be verified") from exc
    if actual != expected:
        raise ResearchAdapterError("research adapter peer did not match its pinned address")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that never re-resolves or consults proxy settings."""

    def __init__(
        self,
        hostname: str,
        port: int,
        resolved: ResolvedAddress,
        deadline: float,
    ) -> None:
        self._ssl_context = ssl.create_default_context()
        super().__init__(
            hostname,
            port=port,
            timeout=None,
            context=self._ssl_context,
        )
        self._resolved_address = resolved
        self._deadline = deadline

    def connect(self) -> None:
        raw_socket: socket.socket | None = None
        tls_socket: ssl.SSLSocket | None = None
        try:
            raw_socket = socket.socket(
                self._resolved_address.family,
                self._resolved_address.socket_type,
                self._resolved_address.protocol,
            )
            raw_socket.settimeout(_remaining_seconds(self._deadline))
            raw_socket.connect(self._resolved_address.sockaddr)
            _verify_pinned_peer(raw_socket, self._resolved_address)
            raw_socket.settimeout(_remaining_seconds(self._deadline))
            tls_socket = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)
            raw_socket = None
            _verify_pinned_peer(tls_socket, self._resolved_address)
            tls_socket.settimeout(_remaining_seconds(self._deadline))
            self.sock = tls_socket
            tls_socket = None
        except Exception:
            if raw_socket is not None:
                raw_socket.close()
            if tls_socket is not None:
                tls_socket.close()
            if self.sock is not None:
                self.sock.close()
                self.sock = None
            raise


def _set_connection_deadline(connection: Any, deadline: float) -> None:
    sock = getattr(connection, "sock", None)
    if sock is None:
        raise ResearchAdapterError("research adapter connection is unavailable")
    sock.settimeout(_remaining_seconds(deadline))


def _abort_connection(connection: Any) -> None:
    sock = getattr(connection, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
    connection.close()


def _connection_operation(
    operation: Callable[[], Any],
    connection: Any,
    deadline: float,
) -> Any:
    """Bound a potentially multi-read HTTP operation by the absolute deadline."""

    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result = operation()
        except Exception as exc:  # noqa: BLE001 - marshal transport failures to caller
            result_queue.put((False, exc))
        else:
            result_queue.put((True, result))

    operation_thread = threading.Thread(
        target=run,
        name="research-adapter-io",
        daemon=True,
    )
    operation_thread.start()
    try:
        succeeded, result = result_queue.get(timeout=_remaining_seconds(deadline))
    except queue.Empty as exc:
        _abort_connection(connection)
        raise ResearchAdapterError("research adapter deadline exceeded") from exc
    if not succeeded:
        if isinstance(result, BaseException):
            raise result
        raise ResearchAdapterError("research adapter request failed")
    return result


def _read_bounded_adapter_json(response: Any, connection: Any, deadline: float) -> Any:
    raw_encoding = response.getheader("Content-Encoding")
    encoding = "identity" if raw_encoding is None else raw_encoding.strip().casefold()
    if encoding in {"", "identity"}:
        decompressor: Any = None
    elif encoding in {"gzip", "x-gzip"}:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decompressor = zlib.decompressobj()
    else:
        raise ResearchAdapterError("research adapter returned an unsupported content encoding")

    raw_length = response.getheader("Content-Length")
    if raw_length is not None:
        try:
            content_length = int(raw_length, 10)
        except ValueError as exc:
            raise ResearchAdapterError(
                "research adapter returned an invalid content length"
            ) from exc
        if content_length < 0 or content_length > MAX_ADAPTER_RESPONSE_BYTES:
            raise ResearchAdapterError("research adapter response exceeded its size limit")

    content = bytearray()
    wire_bytes = 0
    reader = getattr(response, "read1", None)
    if not callable(reader):
        reader = response.read
    try:
        while True:
            _set_connection_deadline(connection, deadline)
            chunk = reader(ADAPTER_READ_CHUNK_BYTES)
            _remaining_seconds(deadline)
            if not isinstance(chunk, bytes):
                raise ResearchAdapterError("research adapter returned invalid response bytes")
            if not chunk:
                break
            wire_bytes += len(chunk)
            if wire_bytes > MAX_ADAPTER_RESPONSE_BYTES:
                raise ResearchAdapterError("research adapter response exceeded its size limit")
            if decompressor is None:
                content.extend(chunk)
            else:
                remaining = MAX_ADAPTER_RESPONSE_BYTES - len(content)
                content.extend(decompressor.decompress(chunk, remaining + 1))
                if decompressor.unconsumed_tail:
                    raise ResearchAdapterError("research adapter response exceeded its size limit")
            if len(content) > MAX_ADAPTER_RESPONSE_BYTES:
                raise ResearchAdapterError("research adapter response exceeded its size limit")
        if decompressor is not None:
            remaining = MAX_ADAPTER_RESPONSE_BYTES - len(content)
            content.extend(decompressor.flush(remaining + 1))
            if (
                len(content) > MAX_ADAPTER_RESPONSE_BYTES
                or not decompressor.eof
                or decompressor.unused_data
            ):
                raise ResearchAdapterError("research adapter returned invalid compressed data")
        _remaining_seconds(deadline)
        return json.loads(content) if content else None
    except zlib.error as exc:
        raise ResearchAdapterError("research adapter returned invalid compressed data") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchAdapterError("research adapter returned invalid JSON") from exc


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
    if (
        not isinstance(token, str)
        or not token
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
    ):
        raise ResearchAdapterError("research adapter credential is invalid")
    if not 0 < timeout_seconds <= 60:
        raise ResearchAdapterError("research adapter timeout is invalid")
    try:
        endpoint = validate_adapter_endpoint(endpoint)
    except ValueError as exc:
        raise ResearchAdapterError("research adapter endpoint is invalid") from exc
    encoded = json.dumps(
        body,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_ADAPTER_REQUEST_BYTES:
        raise ResearchAdapterError("research adapter request exceeded its size limit")
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname
    if hostname is None:
        raise ResearchAdapterError("research adapter endpoint is invalid")
    port = 443 if parsed.port is None else parsed.port
    deadline = time.monotonic() + timeout_seconds
    addresses = _resolve_global_addresses(hostname, port, deadline)
    connection: Any = None
    last_connect_error: Exception | None = None
    try:
        for address in addresses:
            candidate = _PinnedHTTPSConnection(hostname, port, address, deadline)
            try:
                candidate.connect()
            except (
                ResearchAdapterError,
                http.client.HTTPException,
                ssl.SSLError,
                OSError,
            ) as exc:
                candidate.close()
                last_connect_error = exc
                continue
            connection = candidate
            break
        if connection is None:
            raise ResearchAdapterError("research adapter connection failed") from last_connect_error
        _set_connection_deadline(connection, deadline)
        _connection_operation(
            lambda: connection.request(
                "POST",
                parsed.path,
                body=encoded,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                },
            ),
            connection,
            deadline,
        )
        _set_connection_deadline(connection, deadline)
        response = _connection_operation(connection.getresponse, connection, deadline)
        _remaining_seconds(deadline)
        if 300 <= response.status < 400:
            raise ResearchAdapterError("research adapter redirects are not allowed")
        if not 200 <= response.status < 300:
            raise ResearchAdapterError("research adapter request failed")
        return _read_bounded_adapter_json(response, connection, deadline)
    except ResearchAdapterError:
        raise
    except (
        http.client.HTTPException,
        ssl.SSLError,
        TimeoutError,
        OSError,
    ) as exc:
        raise ResearchAdapterError("research adapter request failed") from exc
    finally:
        if connection is not None:
            connection.close()


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
        if (
            not isinstance(token, str)
            or not token
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
        ):
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
