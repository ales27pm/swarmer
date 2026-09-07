import asyncio
import resource
from pathlib import Path

import pytest

from app.services.permission_policy import PermissionPolicy, ProcessPolicy, ToolPermissionRule
from app.services.process_sandbox import ProcessSandbox, ProcessSandboxError


def sandbox(tmp_path: Path) -> tuple[ProcessSandbox, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binary = tmp_path / "bwrap"
    binary.touch(mode=0o700)
    policy = PermissionPolicy(
        protected_paths=("**/.env", "**/.env.*", "**/*.key", "**/*token*"),
        tool_rules={
            "workspace.list_dir": ToolPermissionRule("list", "List files.", "allow", "low"),
            "workspace.read_text": ToolPermissionRule("read", "Read files.", "allow", "low"),
            "workspace.write_text": ToolPermissionRule(
                "write", "Writing requires approval.", "ask", "medium", 300
            ),
            "process.run": ToolPermissionRule(
                "process", "Processes require approval.", "ask", "high", 300
            ),
        },
        process=ProcessPolicy(
            backend="bubblewrap",
            binary=binary,
            network="deny",
            allowed_commands=frozenset({"git", "node", "npm", "npx", "python3", "pytest"}),
            max_timeout_seconds=30,
            max_output_bytes=4096,
            max_memory_bytes=1_073_741_824,
            max_processes=64,
            max_file_bytes=16_777_216,
            max_open_files=256,
        ),
    )
    return ProcessSandbox(workspace, policy), workspace


def test_command_contains_isolation_and_masks_protected_files(tmp_path: Path) -> None:
    process_sandbox, workspace = sandbox(tmp_path)
    (workspace / ".env").write_text("never read", encoding="utf-8")
    command = process_sandbox.build_command(["pytest", "-q"], workspace)

    assert "--unshare-all" in command
    assert command.index("--unshare-user") < command.index("--disable-userns")
    assert "--disable-userns" in command
    assert "--new-session" in command
    assert "--clearenv" in command
    assert "--share-net" not in command
    assert ["--ro-bind", "/dev/null", str(workspace / ".env")] == command[
        command.index("--ro-bind", command.index("--bind") + 1) : command.index(
            "--ro-bind", command.index("--bind") + 1
        )
        + 3
    ]
    assert command[-3:] == ["--", "pytest", "-q"]


def test_process_rejects_protected_file_hardlink_alias(tmp_path: Path) -> None:
    process_sandbox, workspace = sandbox(tmp_path)
    protected = workspace / ".env"
    alias = workspace / "notes.txt"
    protected.write_text("DATABASE_PASSWORD=do-not-read", encoding="utf-8")
    alias.hardlink_to(protected)

    with pytest.raises(ProcessSandboxError, match="protected file has hard-link aliases") as raised:
        process_sandbox.build_command(["pytest", "-q"], workspace)
    assert protected.relative_to(workspace).as_posix() not in str(raised.value)


@pytest.mark.parametrize(
    "protected_parts",
    [
        ("nested", "private.key"),
        ("nested", ".env", "credential.txt"),
    ],
    ids=["nested-protected-file", "file-inside-protected-directory"],
)
def test_process_rejects_nested_protected_hardlink_alias(
    tmp_path: Path, protected_parts: tuple[str, ...]
) -> None:
    process_sandbox, workspace = sandbox(tmp_path)
    protected = workspace.joinpath(*protected_parts)
    protected.parent.mkdir(parents=True)
    protected.write_text("DATABASE_PASSWORD=do-not-read", encoding="utf-8")
    (workspace / "visible.txt").hardlink_to(protected)

    with pytest.raises(ProcessSandboxError, match="protected file has hard-link aliases") as raised:
        process_sandbox.build_command(["pytest", "-q"], workspace)
    assert protected.relative_to(workspace).as_posix() not in str(raised.value)


def test_process_allows_safe_file_hardlinks(tmp_path: Path) -> None:
    process_sandbox, workspace = sandbox(tmp_path)
    original = workspace / "source.txt"
    alias = workspace / "alias.txt"
    original.write_text("safe content", encoding="utf-8")
    alias.hardlink_to(original)

    command = process_sandbox.build_command(["pytest", "-q"], workspace)

    assert command[-3:] == ["--", "pytest", "-q"]


@pytest.mark.parametrize(
    "argv,match",
    [
        (["/usr/bin/git", "status"], "allowlisted name"),
        (["sh", "-c", "id"], "not allowed"),
        (["python3", "-c", "print(1)"], "inline interpreter"),
        (["python3", "-cprint(1)"], "inline interpreter"),
        (["node", "-econsole.log(1)"], "inline interpreter"),
        (["node", "--eval", "1+1"], "inline interpreter"),
        (["node", "--eval=1+1"], "inline interpreter"),
        (["node", "-p", "process.env"], "inline interpreter"),
        (["node", "-pprocess.env"], "inline interpreter"),
        (["node", "--print=process.env"], "inline interpreter"),
        (["git", "-c", "alias.pwn=!sh", "pwn"], "git subcommand"),
        (["git", "config", "alias.pwn", "!sh"], "git subcommand"),
        (["git", "pwn"], "git subcommand"),
        (["git", "status", "--exec-path=/tmp"], "status options"),
        (["npm", "install"], "limited to test"),
        (["npx", "expo-doctor"], "requires --no-install"),
    ],
)
def test_unsafe_command_shapes_are_rejected(tmp_path: Path, argv: list[str], match: str) -> None:
    process_sandbox, _ = sandbox(tmp_path)
    with pytest.raises(ProcessSandboxError, match=match):
        process_sandbox.validate(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status", "--short", "--branch"],
        ["git", "rev-parse", "--is-inside-work-tree"],
        ["git", "rev-parse", "--verify", "HEAD"],
        ["git", "ls-files", "--cached", "--modified"],
    ],
)
def test_read_only_git_query_shapes_are_allowed(tmp_path: Path, argv: list[str]) -> None:
    process_sandbox, _ = sandbox(tmp_path)
    process_sandbox.validate(argv)


def test_posix_resource_limits_are_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process_sandbox, _ = sandbox(tmp_path)
    applied: dict[int, tuple[int, int]] = {}
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda kind, value: applied.__setitem__(kind, value),
    )

    process_sandbox._set_resource_limits(2.2)

    assert applied[resource.RLIMIT_CPU] == (4, 4)
    assert applied[resource.RLIMIT_AS] == (1_073_741_824, 1_073_741_824)
    assert applied[resource.RLIMIT_NPROC] == (64, 64)
    assert applied[resource.RLIMIT_FSIZE] == (16_777_216, 16_777_216)
    assert applied[resource.RLIMIT_NOFILE] == (256, 256)
    assert applied[resource.RLIMIT_CORE] == (0, 0)


@pytest.mark.asyncio
async def test_outer_cancellation_kills_and_reaps_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_sandbox, workspace = sandbox(tmp_path)
    done = asyncio.Event()
    wait_started = asyncio.Event()

    class FakeProcess:
        pid = 4242
        returncode: int | None = None
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        completed_waits = 0

        async def wait(self) -> int:
            wait_started.set()
            await done.wait()
            self.completed_waits += 1
            assert self.returncode is not None
            return self.returncode

    process = FakeProcess()
    process.stdout.feed_eof()
    process.stderr.feed_eof()

    async def create_process(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        return process

    killed: list[int] = []

    def kill_process_group(pid: int) -> None:
        killed.append(pid)
        process.returncode = -9
        done.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(process_sandbox, "_kill_process_group", kill_process_group)
    running = asyncio.create_task(process_sandbox.run(["pytest", "-q"], workspace, 10))
    await asyncio.wait_for(wait_started.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert killed == [4242]
    assert process.completed_waits == 1


@pytest.mark.asyncio
async def test_cancellation_during_spawn_still_reaps_created_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_sandbox, workspace = sandbox(tmp_path)
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    process_done = asyncio.Event()

    class FakeProcess:
        pid = 4243
        returncode: int | None = None
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        completed_waits = 0

        async def wait(self) -> int:
            await process_done.wait()
            self.completed_waits += 1
            assert self.returncode is not None
            return self.returncode

    process = FakeProcess()
    process.stdout.feed_eof()
    process.stderr.feed_eof()

    async def delayed_create(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        spawn_started.set()
        await release_spawn.wait()
        return process

    killed: list[int] = []

    def kill_process_group(pid: int) -> None:
        killed.append(pid)
        process.returncode = -9
        process_done.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create)
    monkeypatch.setattr(process_sandbox, "_kill_process_group", kill_process_group)
    running = asyncio.create_task(process_sandbox.run(["pytest", "-q"], workspace, 10))
    await asyncio.wait_for(spawn_started.wait(), timeout=1)
    running.cancel()
    release_spawn.set()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert killed == [4243]
    assert process.completed_waits == 1
