"""Exercise the actual imported client and serialized claim for every worker family."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = (
    "file",
    "research",
    "code-review",
    "code",
    "text",
    "project",
    "personal",
    "sqlite",
    "swift",
    "media",
)

# A separate interpreter avoids accidentally proving one family's cached import
# while another family would ship or load a different protocol module.
WIRE_PROBE = r"""
import importlib.util
import io
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

root, family, scenario = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
directory = root / "workers" / (family + "-worker")
sys.path.insert(0, str(directory))

def no_network(*args, **kwargs):
    raise AssertionError("wire probe must not open a socket")

socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network

path = directory / (family.replace("-", "_") + "_worker.py")
spec = importlib.util.spec_from_file_location("handshake_worker", path)
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)

if family in {"file", "research", "code-review"}:
    protocol = worker
elif family == "swift":
    protocol = worker._protocol()
else:
    protocol = worker.protocol
client_type = worker.MediaClient if family == "media" else protocol.ControlPlaneClient
expected_protocol = family if family in {"research", "code-review"} else "file"
assert Path(protocol.__file__).resolve() == (
    root / "workers" / (expected_protocol + "-worker")
    / (expected_protocol.replace("-", "_") + "_worker.py")
).resolve()

requests = []
responses = iter([None, {"id": "job_wire"}, None])

class Opener:
    def open(self, request, timeout):
        assert timeout == 30
        assert request.full_url == "https://control.example/agents/worker%2Fone/claim"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer test-credential"
        assert request.get_header("Content-type") == "application/json"
        requests.append(json.loads(request.data))
        if scenario == "old-api":
            raise urllib.error.HTTPError(request.full_url, 422, "unsupported", {}, None)
        return io.BytesIO(json.dumps(next(responses)).encode())

def build_opener(*handlers):
    assert len(handlers) == 1
    assert isinstance(handlers[0], protocol._RejectRedirects)
    return Opener()

urllib.request.build_opener = build_opener
client = client_type("https://control.example", "worker/one", "test-credential")
if scenario == "old-api":
    try:
        client.claim()
    except protocol.ControlPlaneUnavailable:
        pass
    else:
        raise AssertionError("old API rejection must remain visible")
    assert len(requests) == 1, "never retry without the protocol handshake"
else:
    assert client.claim() is None
    assert client.claim() == {"id": "job_wire"}
    assert client.claim() is None
    assert len(requests) == 3
for request in requests:
    assert request == {"wait_seconds": 0, "context_protocols": ["symbolic-v1"]}, request
print(json.dumps({"family": family, "scenario": scenario, "claims": len(requests)}))
"""


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("scenario", ["every-claim", "old-api"])
def test_actual_worker_client_advertises_protocol_without_legacy_retry(
    family: str, scenario: str
) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", WIRE_PROBE, str(ROOT), family, scenario],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "family": family,
        "scenario": scenario,
        "claims": 1 if scenario == "old-api" else 3,
    }


@pytest.mark.parametrize(
    "relative_path",
    [
        "code-review-worker/agent-card.json",
        "code-worker/agent-card.json",
        "media-worker/agent-card-audio.json",
        "media-worker/agent-card-image.json",
        "media-worker/agent-card-chroma.json",
        "project-worker/agent-card.json",
        "research-worker/agent-card.json",
        "text-worker/agent-card.json",
    ],
)
def test_new_binary_manifest_declares_symbolic_context_version(relative_path: str) -> None:
    manifest = json.loads((ROOT / "workers" / relative_path).read_text())
    version = manifest["limits"].get("symbolic_context_version")
    assert type(version) is int and version == 1
