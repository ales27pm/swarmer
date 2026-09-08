from __future__ import annotations

import copy
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def load_worker() -> ModuleType:
    path = Path(__file__).with_name("code_review_worker.py")
    spec = importlib.util.spec_from_file_location("mongars_code_review_worker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def claimed_job(skill: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": "job_review_123",
        "required_skill": skill,
        "payload": payload or {},
        "claim_token": "claim-token-with-enough-entropy",
        "lease_id": "lease_review_123",
        "lease_generation": 5,
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
        worker.control_plane_request(
            "https://control.example",
            "/agents/agent/claim",
            "agent-secret",
            "POST",
            {"wait_seconds": 0},
        )


def test_payloads_are_skill_specific_and_never_accept_commands_or_urls(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    (tmp_path / "safe.py").write_text("value = 1\n", encoding="utf-8")

    assert worker.parse_review_job(tmp_path, claimed_job("code_review.git_status")).skill == (
        "code_review.git_status"
    )
    for skill, payload in (
        ("code_review.git_status", {"command": "git push"}),
        ("code_review.git_diff", {"url": "https://attacker.invalid"}),
        ("code_review.git_diff", {"paths": [".env"]}),
        ("code_review.git_diff", {"paths": ["../outside"]}),
        ("code_review.git_diff", {"staged": 1}),
        ("code_review.git_show", {"revision": "--help"}),
        ("code_review.git_show", {"revision": "HEAD~1"}),
        ("code_review.static_analysis", {"paths": []}),
        ("code_review.static_analysis", {"paths": ["safe.txt"]}),
    ):
        with pytest.raises((TypeError, ValueError)):
            worker.parse_review_job(tmp_path, claimed_job(skill, payload))
    with pytest.raises(ValueError, match="unsupported worker skill"):
        worker.parse_review_job(tmp_path, claimed_job("code_review.shell", {"command": "id"}))


def test_static_analysis_rejects_symlinks_and_protected_files(tmp_path: Path) -> None:
    worker = load_worker()
    (tmp_path / "safe.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "secret.py").write_text("TOKEN = 'x'\n", encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(tmp_path / "safe.py")

    request = worker.parse_review_job(
        tmp_path,
        claimed_job("code_review.static_analysis", {"paths": ["safe.py"]}),
    )

    assert request.paths == ("safe.py",)
    for path in ("secret.py", "linked.py"):
        with pytest.raises(ValueError):
            worker.parse_review_job(
                tmp_path,
                claimed_job("code_review.static_analysis", {"paths": [path]}),
            )


def test_executor_builds_only_fixed_read_only_git_and_ruff_commands(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    (tmp_path / ".git").mkdir()
    (tmp_path / "safe.py").write_text("value = 1\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(
        arguments: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
        env: dict[str, str],
    ) -> Any:
        assert cwd == tmp_path.resolve()
        assert 0 < timeout_seconds <= 60
        assert 0 < max_output_bytes <= 524_288
        calls.append((list(arguments), dict(env)))
        if "--porcelain=v1" in arguments:
            return worker.CommandResult(0, " M safe.py\0?? .env\0", False)
        if "--name-only" in arguments:
            return worker.CommandResult(0, "safe.py\0.env\0", False)
        return worker.CommandResult(0, "bounded analyzer output", False)

    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )

    status = executor.execute(claimed_job("code_review.git_status"))
    diff = executor.execute(
        claimed_job(
            "code_review.git_diff",
            {"staged": False, "paths": [], "context_lines": 3},
        )
    )
    shown = executor.execute(
        claimed_job(
            "code_review.git_show",
            {"revision": "HEAD", "paths": [], "context_lines": 2},
        )
    )
    analysis = executor.execute(claimed_job("code_review.static_analysis", {"paths": ["safe.py"]}))

    assert status == {
        "content_trust": "untrusted",
        "entries": [{"status": " M", "path": "safe.py"}],
        "protected_entries_omitted": 1,
        "truncated": False,
    }
    for result in (diff, shown, analysis):
        assert result["content_trust"] == "untrusted"
        assert result["output"] == "bounded analyzer output"
        assert result["truncated"] is False
    commands = [arguments for arguments, _ in calls]
    for command in commands:
        if command[0] == "/usr/bin/git":
            assert f"--git-dir={tmp_path / '.git'}" in command
            assert f"--work-tree={tmp_path}" in command
            assert "-C" not in command
    assert any("diff" in command for command in commands)
    assert any("show" in command for command in commands)
    assert any(command[0] == "/opt/tools/ruff" for command in commands)
    assert all(command[0] in {"/usr/bin/git", "/opt/tools/ruff"} for command in commands)
    assert not any(
        forbidden in command
        for command in commands
        for forbidden in ("push", "fetch", "pull", "clone", "commit", "reset")
    )
    for _, env in calls:
        assert "MONGARS_AGENT_CREDENTIAL" not in env
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        assert env["GIT_NO_LAZY_FETCH"] == "1"


def test_executor_rejects_redirected_or_linked_git_metadata(tmp_path: Path) -> None:
    worker = load_worker()
    outside = tmp_path.parent / "outside-git"
    outside.mkdir()

    (tmp_path / ".git").write_text(f"gitdir: {outside}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Git metadata"):
        worker.CodeReviewExecutor(
            tmp_path,
            git_binary="/usr/bin/git",
            ruff_binary="/opt/tools/ruff",
        )

    (tmp_path / ".git").unlink()
    (tmp_path / ".git").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Git metadata"):
        worker.CodeReviewExecutor(
            tmp_path,
            git_binary="/usr/bin/git",
            ruff_binary="/opt/tools/ruff",
        )


def test_executor_rejects_external_object_alternates(tmp_path: Path) -> None:
    worker = load_worker()
    info = tmp_path / ".git" / "objects" / "info"
    info.mkdir(parents=True)
    (info / "alternates").write_text("/protected/object-store\n", encoding="utf-8")

    with pytest.raises(ValueError, match="alternate object"):
        worker.CodeReviewExecutor(
            tmp_path,
            git_binary="/usr/bin/git",
            ruff_binary="/opt/tools/ruff",
        )


def test_bounded_runner_truncates_output_and_enforces_timeout(tmp_path: Path) -> None:
    worker = load_worker()
    result = worker.run_bounded_command(
        [sys.executable, "-c", "print('x' * 10000)"],
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_bytes=128,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.truncated is True
    assert len(result.output.encode("utf-8")) <= 128

    with pytest.raises(worker.WorkerExecutionError, match="timed out"):
        worker.run_bounded_command(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            cwd=tmp_path,
            timeout_seconds=0.01,
            max_output_bytes=128,
            env={"PATH": "/usr/bin:/bin"},
        )


def test_run_once_preserves_lease_proof_and_submits_bounded_untrusted_result(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = []
    lock = threading.Lock()
    renewed_twice = threading.Event()
    heartbeat_count = 0
    job = claimed_job("code_review.git_status")

    def control_request(
        base_url: str,
        path: str,
        token: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal heartbeat_count
        with lock:
            calls.append((base_url, path, token, copy.deepcopy(body)))
        if path.endswith("/claim"):
            return job
        if path.endswith("/jobs/job_review_123/heartbeat"):
            heartbeat_count += 1
            if heartbeat_count >= 2:
                renewed_twice.set()
            return {"id": job["id"], "status": "running"}
        if path.endswith("/jobs/job_review_123/result"):
            return {"id": job["id"], "status": "completed"}
        return {"status": "ok"}

    class BlockingExecutor:
        def execute(self, claimed: dict[str, Any]) -> dict[str, Any]:
            assert renewed_twice.wait(timeout=1)
            return {
                "content_trust": "untrusted",
                "entries": [],
                "protected_entries_omitted": 0,
                "truncated": False,
            }

    worker.control_plane_request = control_request

    assert worker.run_once(
        "https://control.example",
        "agt_review",
        "agent-secret",
        BlockingExecutor(),
        heartbeat_interval_seconds=0.01,
    )

    proof = {
        "claim_token": job["claim_token"],
        "lease_id": job["lease_id"],
        "lease_generation": job["lease_generation"],
    }
    result_call = next(call for call in calls if call[1].endswith("/result"))
    assert result_call[3] == {
        **proof,
        "status": "completed",
        "result": {
            "content_trust": "untrusted",
            "entries": [],
            "protected_entries_omitted": 0,
            "truncated": False,
        },
    }
    assert (
        len([call for call in calls if "/jobs/" in call[1] and call[1].endswith("heartbeat")]) >= 2
    )
    assert "agent-secret" not in json.dumps(result_call[3])


def test_run_once_discards_result_after_lease_loss() -> None:
    worker = load_worker()
    paths: list[str] = []
    lease_lost = threading.Event()
    heartbeat_count = 0

    def control_request(
        base_url: str,
        path: str,
        token: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal heartbeat_count
        paths.append(path)
        if path.endswith("/claim"):
            return claimed_job("code_review.git_status")
        if "/jobs/" in path and path.endswith("/heartbeat"):
            heartbeat_count += 1
            if heartbeat_count > 1:
                lease_lost.set()
                raise worker.LeaseLost("stale lease")
        return {"status": "ok"}

    class BlockingExecutor:
        def execute(self, job: dict[str, Any]) -> dict[str, Any]:
            assert lease_lost.wait(timeout=1)
            return {"content_trust": "untrusted", "entries": []}

    worker.control_plane_request = control_request

    assert worker.run_once(
        "https://control.example",
        "agt_review",
        "agent-secret",
        BlockingExecutor(),
        heartbeat_interval_seconds=0.01,
    )
    assert not any(path.endswith("/result") for path in paths)


def test_agent_card_contains_only_static_safe_metadata() -> None:
    manifest = json.loads(Path(__file__).with_name("agent-card.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == "1"
    assert manifest["name"] == "mongars-code-review-worker"
    assert [skill["id"] for skill in manifest["skills"]] == [
        "code_review.git_status",
        "code_review.git_diff",
        "code_review.git_show",
        "code_review.static_analysis",
    ]
    encoded = json.dumps(manifest).casefold()
    for forbidden in (
        "credential",
        "token",
        "secret",
        "hostname",
        "/users/",
        "http://",
        "https://",
    ):
        assert forbidden not in encoded
