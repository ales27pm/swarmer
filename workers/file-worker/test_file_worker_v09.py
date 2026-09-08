import copy
import importlib.util
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any, Self

import pytest


def load_worker() -> ModuleType:
    path = Path(__file__).with_name("file_worker.py")
    spec = importlib.util.spec_from_file_location("sample_file_worker_v09", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def claimed_job(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": "job_123",
        "required_skill": "workspace.list_dir",
        "payload": payload or {"path": "."},
        "claim_token": "claim-token-with-enough-entropy",
        "lease_id": "lease_1234567890",
        "lease_generation": 4,
    }


@pytest.mark.parametrize(
    "origin",
    [
        "https://control.example",
        "https://control.example:8443",
        "http://localhost:8710",
        "http://127.0.0.1:8710",
        "http://[::1]:8710",
    ],
)
def test_control_plane_origin_accepts_https_and_loopback_http(origin: str) -> None:
    worker = load_worker()

    assert worker.validate_control_plane_origin(origin) == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://control.example",
        "https://user:password@control.example",
        "https://control.example/",
        "https://control.example/api",
        "https://control.example?",
        "https://control.example?debug=1",
        "https://control.example#fragment",
        "https://control.example\\@attacker.invalid",
        "https://control.example:",
        "https://control .example",
        "ftp://control.example",
        "not-a-url",
    ],
)
def test_control_plane_origin_rejects_non_bare_or_unsafe_urls(origin: str) -> None:
    worker = load_worker()

    with pytest.raises(ValueError, match="control-plane URL"):
        worker.validate_control_plane_origin(origin)


def test_control_plane_request_disables_redirects_with_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    handlers: list[Any] = []

    class RejectingOpener:
        def open(self, request: Any, timeout: float) -> Any:
            assert timeout == 30
            assert request.get_header("Authorization") == "Bearer agent-secret"
            assert len(handlers) == 1
            assert isinstance(handlers[0], worker._RejectRedirects)
            assert (
                handlers[0].redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {},
                    "https://attacker.invalid/capture",
                )
                is None
            )
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, None)

    def build_opener(*configured: Any) -> RejectingOpener:
        handlers.extend(configured)
        return RejectingOpener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)

    with pytest.raises(urllib.error.HTTPError):
        worker.request(
            "https://control.example",
            "/agents/agent/claim",
            "agent-secret",
            "POST",
            {"wait_seconds": 0},
        )


def test_control_plane_response_read_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    requested_sizes: list[int] = []

    class OversizedResponse:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            requested_sizes.append(size)
            return b"x" * size

    class FakeOpener:
        def open(self, request: Any, timeout: float) -> OversizedResponse:
            return OversizedResponse()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: FakeOpener())

    with pytest.raises(worker.WorkerProtocolError, match="size limit"):
        worker.request("https://control.example", "/agents/agent/claim", "secret")
    assert requested_sizes == [worker.MAX_CONTROL_RESPONSE_BYTES + 1]


def test_repeats_lease_heartbeat_during_execution_and_submits_proof(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = []
    call_lock = threading.Lock()
    renewed_twice = threading.Event()
    heartbeat_count = 0
    job = claimed_job()

    def fake_request(
        base_url: str,
        path: str,
        token: str,
        method: str,
        body: dict[str, Any] | None,
    ) -> Any:
        nonlocal heartbeat_count
        with call_lock:
            calls.append((base_url, path, token, copy.deepcopy(body)))
        if path.endswith("/claim"):
            return job
        if path.endswith("/jobs/job_123/heartbeat"):
            heartbeat_count += 1
            if heartbeat_count >= 2:
                renewed_twice.set()
            return {"id": "job_123", "status": "running"}
        if path.endswith("/jobs/job_123/result"):
            return {"id": "job_123", "status": "completed"}
        return {"status": "ok"}

    def blocked_execute(root: Path, claimed: dict[str, Any]) -> dict[str, Any]:
        assert renewed_twice.wait(timeout=1)
        return {"entries": []}

    worker.request = fake_request
    worker.execute = blocked_execute

    assert worker.run_once(
        "https://control.example",
        "agt_123",
        "agent-credential",
        tmp_path,
        heartbeat_interval_seconds=0.01,
    )

    job_heartbeats = [call for call in calls if "/jobs/job_123/heartbeat" in call[1]]
    assert len(job_heartbeats) >= 2
    expected_proof = {
        "claim_token": job["claim_token"],
        "lease_id": job["lease_id"],
        "lease_generation": job["lease_generation"],
    }
    assert all(call[3] == expected_proof for call in job_heartbeats)
    result_calls = [call for call in calls if call[1].endswith("/result")]
    assert len(result_calls) == 1
    assert result_calls[0][3] == {
        **expected_proof,
        "status": "completed",
        "result": {"entries": []},
    }


def test_stale_lease_discards_local_result(tmp_path: Path) -> None:
    worker = load_worker()
    calls: list[str] = []
    losing_heartbeat_started = threading.Event()
    heartbeat_count = 0

    def fake_request(
        base_url: str,
        path: str,
        token: str,
        method: str,
        body: dict[str, Any] | None,
    ) -> Any:
        nonlocal heartbeat_count
        calls.append(path)
        if path.endswith("/claim"):
            return claimed_job()
        if path.endswith("/jobs/job_123/heartbeat"):
            heartbeat_count += 1
            if heartbeat_count == 1:
                return {"id": "job_123", "status": "running"}
            losing_heartbeat_started.set()
            raise urllib.error.HTTPError(path, 409, "Conflict", {}, None)
        if path.endswith("/jobs/job_123/result"):
            pytest.fail("a stale worker must not submit a terminal result")
        return {"status": "ok"}

    def blocked_execute(root: Path, claimed: dict[str, Any]) -> dict[str, Any]:
        assert losing_heartbeat_started.wait(timeout=1)
        time.sleep(0.02)
        return {"entries": []}

    worker.request = fake_request
    worker.execute = blocked_execute

    assert worker.run_once(
        "https://control.example",
        "agt_123",
        "agent-credential",
        tmp_path,
        heartbeat_interval_seconds=0.01,
    )
    assert not any(path.endswith("/result") for path in calls)


def test_capability_is_created_and_polled_only_through_control_plane(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = []
    heartbeat_count = 0
    capability_path = "/agents/agt_123/jobs/job_123/capability-requests"
    job = claimed_job(
        {
            "path": ".",
            "capability_request": {
                "capability_name": "iphone.location.current",
                "arguments": {},
            },
        }
    )

    def fake_request(
        base_url: str,
        path: str,
        token: str,
        method: str,
        body: dict[str, Any] | None,
    ) -> Any:
        nonlocal heartbeat_count
        calls.append((base_url, path, token, copy.deepcopy(body)))
        if path.endswith("/claim"):
            return job
        if path.endswith("/jobs/job_123/heartbeat"):
            heartbeat_count += 1
            return {"id": "job_123", "status": "running"}
        if path == capability_path:
            return {"request_id": "cap_123", "status": "pending"}
        if path == f"{capability_path}/cap_123/poll":
            return {
                "request_id": "cap_123",
                "status": "completed",
                "result": {"latitude": 45.5, "longitude": -73.6},
            }
        if path.endswith("/jobs/job_123/result"):
            return {"id": "job_123", "status": "completed"}
        return {"status": "ok"}

    worker.request = fake_request

    assert worker.run_once(
        "https://control.example",
        "agt_123",
        "agent-credential",
        tmp_path,
        heartbeat_interval_seconds=0.005,
        capability_poll_seconds=0.02,
    )

    expected_proof = {
        "claim_token": job["claim_token"],
        "lease_id": job["lease_id"],
        "lease_generation": job["lease_generation"],
    }
    create_call = next(call for call in calls if call[1] == capability_path)
    assert create_call[3] == {
        **expected_proof,
        "capability_name": "iphone.location.current",
        "arguments": {},
    }
    poll_call = next(call for call in calls if call[1].endswith("/cap_123/poll"))
    assert poll_call[3] == expected_proof
    result_call = next(call for call in calls if call[1].endswith("/result"))
    assert result_call[3]["result"] == {
        "entries": ["README.md"],
        "capability_result": {
            "request_id": "cap_123",
            "status": "completed",
            "result": {"latitude": 45.5, "longitude": -73.6},
        },
    }
    assert heartbeat_count >= 2
    assert all(call[0] == "https://control.example" for call in calls)
    assert all(call[1].startswith("/agents/") for call in calls)
    assert all(call[2] == "agent-credential" for call in calls)


@pytest.mark.parametrize("status", ["denied", "failed", "expired", "cancelled"])
def test_noncompleted_capability_safely_fails_job(tmp_path: Path, status: str) -> None:
    worker = load_worker()
    submitted: list[dict[str, Any]] = []
    job = claimed_job(
        {
            "path": ".",
            "capability_request": {
                "capability_name": "iphone.location.current",
                "arguments": {},
            },
        }
    )

    def fake_request(
        base_url: str,
        path: str,
        token: str,
        method: str,
        body: dict[str, Any] | None,
    ) -> Any:
        if path.endswith("/claim"):
            return job
        if path.endswith("/heartbeat"):
            return {"status": "running"}
        if path.endswith("/capability-requests"):
            return {"request_id": "cap_123", "status": status}
        if path.endswith("/result"):
            assert body is not None
            submitted.append(body)
            return {"id": "job_123", "status": "failed"}
        return {"status": "ok"}

    worker.request = fake_request

    assert worker.run_once(
        "https://control.example",
        "agt_123",
        "agent-credential",
        tmp_path,
        heartbeat_interval_seconds=1,
    )
    assert submitted[0]["status"] == "failed"
    assert submitted[0]["error"] == f"iPhone capability ended with status {status}"


def test_worker_still_rejects_writes_and_protected_paths(tmp_path: Path) -> None:
    worker = load_worker()
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    with pytest.raises(ValueError, match="protected or invalid path"):
        worker.safe_path(tmp_path, ".env")
    for protected in (
        ".env.local",
        "client-secret.txt",
        "certificate.pem",
        "private.key",
    ):
        (tmp_path / protected).write_text("private", encoding="utf-8")
        with pytest.raises(ValueError, match="protected or invalid path"):
            worker.safe_path(tmp_path, protected)
    (tmp_path / "safe-link").symlink_to(tmp_path / ".env")
    with pytest.raises(ValueError, match="symbolic links"):
        worker.safe_path(tmp_path, "safe-link")
    listing = worker.execute(
        tmp_path,
        {"required_skill": "workspace.list_dir", "payload": {"path": "."}},
    )
    assert not set(listing["entries"]) & {
        ".env",
        ".env.local",
        "client-secret.txt",
        "certificate.pem",
        "private.key",
        "safe-link",
    }
    with pytest.raises(ValueError, match="unsupported worker skill"):
        worker.execute(
            tmp_path,
            {"required_skill": "workspace.write_text", "payload": {"path": "."}},
        )


def test_read_rejects_intermediate_directory_swapped_to_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    root = tmp_path / "root"
    nested = root / "nested"
    outside = tmp_path / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()
    (nested / "safe.txt").write_text("inside", encoding="utf-8")
    (outside / "safe.txt").write_text("outside secret", encoding="utf-8")
    parked = root / "nested-original"
    real_open = worker.os.open
    swapped = False

    def racing_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "nested" and dir_fd is not None and not swapped:
            nested.rename(parked)
            nested.symlink_to(outside, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(worker.os, "open", racing_open)

    with pytest.raises(ValueError, match="symbolic links"):
        worker.execute(
            root,
            {
                "required_skill": "workspace.read_text",
                "payload": {"path": "nested/safe.txt"},
            },
        )
    assert swapped is True


def test_descriptor_relative_traversal_reads_and_lists_nested_paths(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "safe.txt").write_text("verified content", encoding="utf-8")
    (nested / ".env").write_text("SECRET=hidden", encoding="utf-8")

    listing = worker.execute(
        tmp_path,
        {"required_skill": "workspace.list_dir", "payload": {"path": "nested"}},
    )
    content = worker.execute(
        tmp_path,
        {
            "required_skill": "workspace.read_text",
            "payload": {"path": "nested/safe.txt"},
        },
    )

    assert listing == {"entries": ["safe.txt"]}
    assert content == {"content": "verified content"}
