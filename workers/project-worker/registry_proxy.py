"""Credential-free CONNECT proxy restricted to public Python/npm registries."""

from __future__ import annotations

import ipaddress
import selectors
import socket
import socketserver
import threading
import time
from typing import Any

ALLOWED_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org", "registry.npmjs.org"})
MAX_TRANSFER_BYTES = 128_000_000
_CONNECTIONS = threading.BoundedSemaphore(12)


def validated_target(authority: str) -> str:
    if authority.count(":") != 1:
        raise ValueError("invalid registry authority")
    hostname, port = authority.split(":")
    if hostname not in ALLOWED_HOSTS or port != "443":
        raise ValueError("only approved TLS registries are reachable")
    return hostname


def public_addresses(hostname: str) -> list[tuple[Any, ...]]:
    addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("registry DNS resolved to a non-public address")
    return addresses


class RegistryHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        if not _CONNECTIONS.acquire(blocking=False):
            return
        upstream: socket.socket | None = None
        try:
            self.connection.settimeout(10)
            line = self.rfile.readline(4_097)
            if len(line) > 4_096:
                return
            parts = line.decode("ascii").strip().split(" ")
            if len(parts) != 3 or parts[0] != "CONNECT" or parts[2] != "HTTP/1.1":
                raise ValueError("only CONNECT is supported")
            hostname = validated_target(parts[1])
            total_headers = 0
            while True:
                header = self.rfile.readline(4_097)
                total_headers += len(header)
                if total_headers > 16_384 or not header:
                    raise ValueError("invalid proxy headers")
                if header in {b"\r\n", b"\n"}:
                    break
            for family, socktype, proto, _, address in public_addresses(hostname):
                candidate = socket.socket(family, socktype, proto)
                candidate.settimeout(15)
                try:
                    # Connect to the already-validated IP, without a second DNS lookup.
                    candidate.connect(address)
                    upstream = candidate
                    break
                except OSError:
                    candidate.close()
            if upstream is None:
                raise OSError("registry connection failed")
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay(upstream)
        except (OSError, ValueError, UnicodeError):
            try:
                self.connection.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            if upstream is not None:
                upstream.close()
            _CONNECTIONS.release()

    def _relay(self, upstream: socket.socket) -> None:
        deadline = time.monotonic() + 120
        transferred = 0
        with selectors.DefaultSelector() as selector:
            selector.register(self.connection, selectors.EVENT_READ, upstream)
            selector.register(upstream, selectors.EVENT_READ, self.connection)
            while time.monotonic() < deadline and transferred <= MAX_TRANSFER_BYTES:
                ready = selector.select(timeout=1)
                for key, _ in ready:
                    if not isinstance(key.fileobj, socket.socket):
                        return
                    data = key.fileobj.recv(65_536)
                    if not data:
                        return
                    transferred += len(data)
                    key.data.sendall(data)


class RegistryServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 12


if __name__ == "__main__":
    with RegistryServer(("0.0.0.0", 8_080), RegistryHandler) as server:  # nosec B104
        server.serve_forever()
