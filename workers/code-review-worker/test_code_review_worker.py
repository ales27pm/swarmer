from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
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
        assert cwd.is_dir()
        assert 0 < timeout_seconds <= 60
        assert 0 < max_output_bytes <= 524_288
        calls.append((list(arguments), dict(env)))
        if "rev-parse" in arguments:
            return worker.CommandResult(0, f"{'a' * 40}\n", False)
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
            work_tree = next(
                value.removeprefix("--work-tree=")
                for value in command
                if value.startswith("--work-tree=")
            )
            assert Path(work_tree).is_absolute()
            assert f"core.worktree={work_tree}" in command
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
        assert env["GIT_LITERAL_PATHSPECS"] == "1"
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1"


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


@pytest.mark.parametrize(
    "configuration",
    [
        '[filter "exfiltrate"]\n\tclean = sh -c "cat /protected/secret"\n',
        '[filter.exfiltrate]\n\tclean = sh -c "cat /protected/secret"\n',
        '\ufeff[filter "exfiltrate"]\n\tclean = /tmp/attacker-filter\n',
        '[filter "persistent"]\n\tprocess = /tmp/attacker-filter\n',
        "[include]\n\tpath = /protected/attacker.gitconfig\n",
        '[includeIf "gitdir:/srv/repositories/**"]\n\tpath = /protected/attacker.gitconfig\n',
    ],
)
def test_executor_rejects_local_executable_git_configuration(
    tmp_path: Path, configuration: str
) -> None:
    worker = load_worker()
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(configuration, encoding="utf-8")

    with pytest.raises(ValueError, match="executable Git configuration"):
        worker.CodeReviewExecutor(
            tmp_path,
            git_binary="/usr/bin/git",
            ruff_binary="/opt/tools/ruff",
        )


def test_executor_revalidates_repository_identity_before_every_command(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_: Any) -> Any:
        calls.append(arguments)
        return worker.CommandResult(0, "", False)

    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )
    moved = tmp_path / ".git.original"
    git_dir.rename(moved)
    git_dir.mkdir()

    with pytest.raises(worker.WorkerExecutionError, match="changed"):
        executor.execute(claimed_job("code_review.git_status"))
    assert calls == []


def test_executor_revalidates_local_git_config_before_command(tmp_path: Path) -> None:
    worker = load_worker()
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_: Any) -> Any:
        calls.append(arguments)
        return worker.CommandResult(0, "", False)

    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )
    (git_dir / "config").write_text(
        '[filter "late"]\n\tprocess = /tmp/attacker-filter\n',
        encoding="utf-8",
    )

    with pytest.raises(worker.WorkerExecutionError, match="changed"):
        executor.execute(claimed_job("code_review.git_status"))
    assert calls == []


def test_static_analysis_uses_private_snapshot_and_rejects_hardlinks(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    root = tmp_path / "repository"
    root.mkdir()
    (root / ".git").mkdir()
    source = root / "safe.py"
    source.write_text("value = 1\n", encoding="utf-8")
    observed: list[str] = []

    def runner(arguments: list[str], *, cwd: Path, **_: Any) -> Any:
        assert arguments[0] == "/opt/tools/ruff"
        assert cwd != root
        source.write_text("value = 'changed after snapshot'\n", encoding="utf-8")
        observed.append((cwd / "safe.py").read_text(encoding="utf-8"))
        return worker.CommandResult(0, "", False)

    executor = worker.CodeReviewExecutor(
        root,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )
    result = executor.execute(claimed_job("code_review.static_analysis", {"paths": ["safe.py"]}))
    assert result["exit_code"] == 0
    assert observed == ["value = 1\n"]

    outside = tmp_path / "outside.py"
    outside.write_text("PASSWORD = 'outside'\n", encoding="utf-8")
    source.unlink()
    os.link(outside, source)
    with pytest.raises(ValueError, match="hard-linked"):
        executor.execute(claimed_job("code_review.static_analysis", {"paths": ["safe.py"]}))


def test_private_snapshot_rejects_same_inode_edit_between_file_copies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    (tmp_path / ".git").mkdir()
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("value = 2\n", encoding="utf-8")
    original_inode = first.stat().st_ino
    original_reader = worker._read_contained_regular_file
    runner_called = False

    def mutate_after_first_copy(
        root: Path,
        relative: str,
        **kwargs: Any,
    ) -> Any:
        nonlocal runner_called
        captured = original_reader(root, relative, **kwargs)
        if relative == "first.py":
            # In-place, same-length edit: path and inode checks alone do not
            # detect that the two copied files came from different generations.
            first.write_text("value = 9\n", encoding="utf-8")
            assert first.stat().st_ino == original_inode
        return captured

    def runner(*_: Any, **__: Any) -> Any:
        nonlocal runner_called
        runner_called = True
        return worker.CommandResult(0, "", False)

    monkeypatch.setattr(worker, "_read_contained_regular_file", mutate_after_first_copy)
    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )

    with pytest.raises(worker.WorkerExecutionError, match="inputs changed"):
        executor.execute(
            claimed_job(
                "code_review.static_analysis",
                {"paths": ["first.py", "second.py"]},
            )
        )
    assert runner_called is False


def test_private_snapshot_rejects_repository_replacement_during_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    root = tmp_path / "repository"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "first.py").write_text("value = 1\n", encoding="utf-8")
    (root / "second.py").write_text("value = 2\n", encoding="utf-8")
    moved = tmp_path / "repository-before-replacement"
    original_reader = worker._read_contained_regular_file
    runner_called = False

    def replace_after_first_copy(
        source_root: Path,
        relative: str,
        **kwargs: Any,
    ) -> Any:
        captured = original_reader(source_root, relative, **kwargs)
        if relative == "first.py":
            root.rename(moved)
            root.mkdir()
            (root / ".git").mkdir()
            (root / "first.py").write_text("attacker = 1\n", encoding="utf-8")
            (root / "second.py").write_text("attacker = 2\n", encoding="utf-8")
        return captured

    def runner(*_: Any, **__: Any) -> Any:
        nonlocal runner_called
        runner_called = True
        return worker.CommandResult(0, "", False)

    monkeypatch.setattr(worker, "_read_contained_regular_file", replace_after_first_copy)
    executor = worker.CodeReviewExecutor(
        root,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )

    with pytest.raises((ValueError, worker.WorkerExecutionError), match="changed"):
        executor.execute(
            claimed_job(
                "code_review.static_analysis",
                {"paths": ["first.py", "second.py"]},
            )
        )
    assert runner_called is False


def test_diff_snapshot_rejects_same_inode_index_edit_during_worktree_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    index = git_dir / "index"
    index.write_bytes(b"index-generation-one")
    original_inode = index.stat().st_ino
    (tmp_path / "first.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "second.py").write_text("value = 2\n", encoding="utf-8")
    original_reader = worker._read_contained_regular_file
    runner_calls = 0

    def mutate_index_after_first_worktree_copy(
        root: Path,
        relative: str,
        **kwargs: Any,
    ) -> Any:
        captured = original_reader(root, relative, **kwargs)
        if root == tmp_path and relative == "first.py":
            index.write_bytes(b"index-generation-two")
            assert index.stat().st_ino == original_inode
        return captured

    def runner(arguments: list[str], **_: Any) -> Any:
        nonlocal runner_calls
        runner_calls += 1
        if "--name-only" in arguments:
            return worker.CommandResult(0, "first.py\0second.py\0", False)
        return worker.CommandResult(0, "must not be returned", False)

    monkeypatch.setattr(
        worker,
        "_read_contained_regular_file",
        mutate_index_after_first_worktree_copy,
    )
    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )

    with pytest.raises(worker.WorkerExecutionError, match="inputs changed"):
        executor.execute(claimed_job("code_review.git_diff"))
    assert runner_calls == 1


def test_git_object_same_inode_mutation_is_fenced_before_result(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    git_dir = tmp_path / ".git"
    object_dir = git_dir / "objects" / "aa"
    object_dir.mkdir(parents=True)
    object_file = object_dir / ("b" * 38)
    object_file.write_bytes(b"object-generation-one")
    original_inode = object_file.stat().st_ino
    commit_id = "c" * 40
    call_count = 0

    def runner(arguments: list[str], **_: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if "rev-parse" in arguments:
            return worker.CommandResult(0, f"{commit_id}\n", False)
        if "--name-only" in arguments:
            return worker.CommandResult(0, "safe.py\0", False)
        object_file.write_bytes(b"object-generation-two")
        assert object_file.stat().st_ino == original_inode
        return worker.CommandResult(0, "untrusted patch", False)

    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )

    with pytest.raises(worker.WorkerExecutionError, match="inputs changed"):
        executor.execute(claimed_job("code_review.git_show", {"revision": "HEAD"}))
    assert call_count == 3


def test_git_paths_are_literal_and_cannot_reinclude_protected_changes(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    unusual = ":(glob)**"
    protected = "credential.env"
    environment = {
        "PATH": os.defpath,
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    subprocess.run(["/usr/bin/git", "init", "-q", str(tmp_path)], check=True, env=environment)
    (tmp_path / unusual).write_text("before\n", encoding="utf-8")
    (tmp_path / protected).write_text("before\n", encoding="utf-8")
    subprocess.run(
        ["/usr/bin/git", "-C", str(tmp_path), "add", "--", unusual, protected],
        check=True,
        env=environment,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
        env=environment,
    )
    (tmp_path / unusual).write_text("after\n", encoding="utf-8")
    (tmp_path / protected).write_text("PASSWORD=must-not-appear\n", encoding="utf-8")
    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
    )

    result = executor.execute(
        claimed_job("code_review.git_diff", {"paths": [unusual], "context_lines": 1})
    )

    assert "after" in result["output"]
    assert "must-not-appear" not in result["output"]
    assert protected not in result["output"]


def test_git_show_pins_head_to_one_full_object_id(tmp_path: Path) -> None:
    worker = load_worker()
    (tmp_path / ".git").mkdir()
    commit_id = "a" * 40
    commands: list[list[str]] = []

    def runner(arguments: list[str], **_: Any) -> Any:
        commands.append(list(arguments))
        if "rev-parse" in arguments:
            return worker.CommandResult(0, f"{commit_id}\n", False)
        if "--name-only" in arguments:
            return worker.CommandResult(0, "safe.py\0", False)
        return worker.CommandResult(0, "patch", False)

    (tmp_path / "safe.py").write_text("value = 1\n", encoding="utf-8")
    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )

    executor.execute(claimed_job("code_review.git_show", {"revision": "HEAD"}))

    show_commands = [command for command in commands if "show" in command]
    assert len(show_commands) == 2
    assert all(commit_id in command for command in show_commands)
    assert not any("HEAD" in command for command in show_commands)


def test_static_analysis_rejects_symlink_swap_after_initial_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = load_worker()
    (tmp_path / ".git").mkdir()
    source = tmp_path / "safe.py"
    source.write_text("value = 1\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("PASSWORD = 'outside'\n", encoding="utf-8")
    called = False

    def runner(*_: Any, **__: Any) -> Any:
        nonlocal called
        called = True
        return worker.CommandResult(0, "", False)

    original = worker._validate_static_file

    def validate_then_swap(root: Path, relative: str, **kwargs: Any) -> None:
        original(root, relative, **kwargs)
        source.unlink()
        source.symlink_to(outside)

    monkeypatch.setattr(worker, "_validate_static_file", validate_then_swap)
    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )

    with pytest.raises(ValueError, match="symbolic link"):
        executor.execute(claimed_job("code_review.static_analysis", {"paths": ["safe.py"]}))
    assert called is False


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


def test_manifest_rejects_excessive_depth_without_recursing(
    tmp_path: Path,
) -> None:
    worker = load_worker()
    (tmp_path / ".git").mkdir()
    current = tmp_path
    for _ in range(worker.MAX_SNAPSHOT_DEPTH + 1):
        current /= "d"
        current.mkdir()
    runner_called = False

    def runner(*_: Any, **__: Any) -> Any:
        nonlocal runner_called
        runner_called = True
        return worker.CommandResult(0, "", False)

    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )

    with pytest.raises(worker.WorkerExecutionError, match="directory depth limit"):
        executor.execute(claimed_job("code_review.git_status"))
    assert runner_called is False


def test_snapshot_enforces_one_cumulative_source_read_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    (tmp_path / ".git").mkdir()
    (tmp_path / "safe.py").write_text("value = 123456789\n", encoding="utf-8")
    monkeypatch.setattr(worker, "MAX_SNAPSHOT_TOTAL_BYTES", 8)
    runner_called = False

    def runner(*_: Any, **__: Any) -> Any:
        nonlocal runner_called
        runner_called = True
        return worker.CommandResult(0, "", False)

    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        runner=runner,
    )

    with pytest.raises(worker.WorkerExecutionError, match="cumulative snapshot byte limit"):
        executor.execute(claimed_job("code_review.static_analysis", {"paths": ["safe.py"]}))
    assert runner_called is False


def test_review_uses_one_monotonic_deadline_across_snapshot_and_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    (tmp_path / ".git").mkdir()
    now = [100.0]
    observed_timeouts: list[float] = []
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])

    def runner(
        _arguments: list[str],
        *,
        timeout_seconds: float,
        **_kwargs: Any,
    ) -> Any:
        observed_timeouts.append(timeout_seconds)
        now[0] = 121.0
        return worker.CommandResult(0, "", False)

    executor = worker.CodeReviewExecutor(
        tmp_path,
        git_binary="/usr/bin/git",
        ruff_binary="/opt/tools/ruff",
        timeout_seconds=20,
        runner=runner,
    )

    with pytest.raises(worker.WorkerExecutionError, match="timed out"):
        executor.execute(claimed_job("code_review.git_status"))
    assert observed_timeouts == [20.0]


def test_lease_heartbeat_stop_is_bounded_when_request_does_not_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = load_worker()
    blocked = threading.Event()
    release = threading.Event()
    calls = 0

    class BlockingClient:
        def heartbeat_job(self, _job_id: str, _lease: Any) -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                blocked.set()
                release.wait(timeout=1)

    monkeypatch.setattr(worker, "HEARTBEAT_JOIN_TIMEOUT_SECONDS", 0.01)
    lease = worker.LeaseProof("claim", "lease", 1)
    heartbeat = worker.LeaseHeartbeat(BlockingClient(), "job", lease, 0.001)
    heartbeat.start()
    assert blocked.wait(timeout=1)

    started = time.monotonic()
    heartbeat.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert heartbeat._thread is not None and heartbeat._thread.is_alive()
    release.set()
    heartbeat._thread.join(timeout=1)


@pytest.mark.parametrize("failure", [RecursionError("deep tree"), MemoryError()])
def test_run_once_submits_resource_failure_instead_of_terminating_loop(
    failure: Exception,
) -> None:
    worker = load_worker()
    submitted: list[dict[str, Any]] = []

    def control_request(
        _base_url: str,
        path: str,
        _token: str,
        _method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        if path.endswith("/claim"):
            return claimed_job("code_review.git_status")
        if path.endswith("/heartbeat"):
            return {"status": "running"}
        if path.endswith("/result"):
            assert body is not None
            submitted.append(body)
            return {"status": "failed"}
        return {"status": "ok"}

    class FailingExecutor:
        def execute(self, _job: dict[str, Any]) -> dict[str, Any]:
            raise failure

    worker.control_plane_request = control_request

    assert worker.run_once(
        "https://control.example",
        "agt_review",
        "agent-secret",
        FailingExecutor(),
        heartbeat_interval_seconds=1,
    )
    assert submitted[0]["status"] == "failed"
    assert submitted[0]["error"] == "read-only inspection exceeded its resource limits"


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
