from __future__ import annotations

import socket

import pytest
import registry_proxy


@pytest.mark.parametrize(
    "authority",
    [
        "127.0.0.1:443",
        "pypi.org:80",
        "pypi.org.evil:443",
        "registry.npmjs.org:11434",
        "169.254.169.254:443",
        "user@pypi.org:443",
        "pypi.org.:443",
    ],
)
def test_proxy_rejects_non_registry_or_non_tls_targets(authority: str) -> None:
    with pytest.raises(ValueError):
        registry_proxy.validated_target(authority)


def test_proxy_refuses_dns_rebinding_to_private_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="non-public"):
        registry_proxy.public_addresses("pypi.org")


def test_proxy_only_connects_to_prevalidated_public_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.0.223", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: result)
    assert registry_proxy.public_addresses("pypi.org") == result
