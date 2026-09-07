from __future__ import annotations

import asyncio
import math
import os
import resource
import signal
import stat
from dataclasses import dataclass
from pathlib import Path

from app.services.permission_policy import PermissionPolicy, PermissionPolicyError


class ProcessSandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    sandbox: str = "bubblewrap"
    network: str = "denied"

    def as_dict(self) -> dict[str, object]:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "sandbox": self.sandbox,
            "network": self.network,
        }


class ProcessSandbox:
    """Build and run a Bubblewrap sandbox without inheriting host authority."""

    _SAFE_GIT_STATUS_OPTIONS = frozenset(
        {
            "--short",
            "--porcelain",
            "--porcelain=v1",
            "--porcelain=v2",
            "--branch",
            "--show-stash",
            "--no-renames",
            "--untracked-files=no",
            "--untracked-files=normal",
            "--untracked-files=all",
        }
    )
    _SAFE_GIT_REV_PARSE_QUERIES = frozenset(
        {"--is-inside-work-tree", "--show-prefix", "--show-toplevel", "--verify"}
    )
    _SAFE_GIT_LS_FILES_OPTIONS = frozenset(
        {"--cached", "--deleted", "--exclude-standard", "--modified", "--others", "--stage"}
    )

    def __init__(self, workspace_root: Path, policy: PermissionPolicy) -> None:
        self.workspace_root = workspace_root.resolve()
        self.policy = policy

    def validate(self, argv: list[str]) -> None:
        if not argv or len(argv) > 64 or not all(isinstance(value, str) for value in argv):
            raise ProcessSandboxError("argv must be a non-empty string array of at most 64 items")
        if any(
            not value or "\x00" in value or len(value.encode("utf-8")) > 16_384 for value in argv
        ):
            raise ProcessSandboxError("process arguments contain an invalid or oversized value")
        try:
            self.policy.validate_command(argv)
        except PermissionPolicyError as exc:
            raise ProcessSandboxError(str(exc)) from exc

        command = argv[0]
        if command in {"node", "python3"} and any(
            self._is_inline_evaluation_argument(command, value) for value in argv[1:]
        ):
            raise ProcessSandboxError("inline interpreter evaluation is not allowed")
        if command == "git":
            self._validate_git(argv)
        if command == "npm":
            if len(argv) < 2 or argv[1] not in {"test", "run"}:
                raise ProcessSandboxError("npm is limited to test and run scripts")
            if argv[1] == "run" and (
                len(argv) < 3 or argv[2] not in {"check", "lint", "test", "typecheck"}
            ):
                raise ProcessSandboxError("npm run script is not allowlisted")
        if command == "npx" and (
            len(argv) < 3 or argv[1] != "--no-install" or argv[2] not in {"expo-doctor", "tsc"}
        ):
            raise ProcessSandboxError("npx requires --no-install and an allowlisted command")

        binary = self.policy.process.binary
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ProcessSandboxError(f"required sandbox backend is unavailable: {binary}")

    @staticmethod
    def _is_inline_evaluation_argument(command: str, value: str) -> bool:
        if value in {"-c", "-e", "--eval", "--print"}:
            return True
        if value.startswith(("-c", "-e", "--eval=")) and value not in {"-", "--"}:
            return True
        return command == "node" and value.startswith(("-p", "--print="))

    def _validate_git(self, argv: list[str]) -> None:
        """Allow only fixed built-in, read-only Git query shapes."""

        if len(argv) < 2:
            raise ProcessSandboxError("git requires an allowlisted read-only subcommand")
        subcommand = argv[1]
        arguments = argv[2:]
        if subcommand == "status":
            if any(value not in self._SAFE_GIT_STATUS_OPTIONS for value in arguments):
                raise ProcessSandboxError("git status options are not allowlisted")
            return
        if subcommand == "rev-parse":
            if not arguments or arguments[0] not in self._SAFE_GIT_REV_PARSE_QUERIES:
                raise ProcessSandboxError("git rev-parse query is not allowlisted")
            if arguments[0] == "--verify":
                if len(arguments) != 2 or arguments[1] not in {"HEAD", "HEAD^{commit}"}:
                    raise ProcessSandboxError("git rev-parse revision is not allowlisted")
            elif len(arguments) != 1:
                raise ProcessSandboxError("git rev-parse accepts one allowlisted query")
            return
        if subcommand == "ls-files":
            if any(value not in self._SAFE_GIT_LS_FILES_OPTIONS for value in arguments):
                raise ProcessSandboxError("git ls-files options are not allowlisted")
            return
        raise ProcessSandboxError("git subcommand is not allowlisted for sandbox execution")

    def build_command(self, argv: list[str], cwd: Path) -> list[str]:
        self.validate(argv)
        cwd = cwd.resolve()
        if cwd != self.workspace_root and self.workspace_root not in cwd.parents:
            raise ProcessSandboxError("process cwd escapes configured workspace")
        if not cwd.is_dir():
            raise ProcessSandboxError("process cwd is not a directory")

        command = [
            str(self.policy.process.binary),
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--cap-drop",
            "ALL",
            "--clearenv",
        ]
        for source in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
            if source.is_symlink():
                command.extend(("--symlink", os.readlink(source), str(source)))
            elif source.exists():
                command.extend(("--ro-bind", str(source), str(source)))

        if Path("/etc/ssl/certs").is_dir():
            command.extend(("--dir", "/etc", "--dir", "/etc/ssl"))
            command.extend(("--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs"))

        command.extend(
            ("--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp")  # nosec B108
        )
        command.extend(("--tmpfs", "/run"))
        for parent in reversed(self.workspace_root.parents):
            if parent != Path("/"):
                command.extend(("--dir", str(parent)))
        command.extend(("--bind", str(self.workspace_root), str(self.workspace_root)))
        command.extend(self._protected_mounts())

        runtime_path = ":".join(
            (
                str(self.workspace_root / "server" / ".venv" / "bin"),
                str(self.workspace_root / ".venv" / "bin"),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            )
        )
        command.extend(
            (
                "--chdir",
                str(cwd),
                "--setenv",
                "HOME",
                "/tmp",  # nosec B108 - private tmpfs inside the sandbox namespace
                "--setenv",
                "TMPDIR",
                "/tmp",  # nosec B108 - private tmpfs inside the sandbox namespace
                "--setenv",
                "PATH",
                runtime_path,
                "--setenv",
                "GIT_CONFIG_NOSYSTEM",
                "1",
                "--setenv",
                "GIT_CONFIG_GLOBAL",
                "/dev/null",
                "--setenv",
                "GIT_CONFIG_COUNT",
                "2",
                "--setenv",
                "GIT_CONFIG_KEY_0",
                "core.fsmonitor",
                "--setenv",
                "GIT_CONFIG_VALUE_0",
                "false",
                "--setenv",
                "GIT_CONFIG_KEY_1",
                "core.hooksPath",
                "--setenv",
                "GIT_CONFIG_VALUE_1",
                "/dev/null",
                "--",
                *argv,
            )
        )
        return command

    def _protected_mounts(self) -> list[str]:
        mounts: list[str] = []
        count = 0

        def fail_walk(error: OSError) -> None:
            raise ProcessSandboxError("cannot verify protected workspace paths") from error

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            workspace_fd = os.open(self.workspace_root, flags)
        except OSError as exc:
            raise ProcessSandboxError("cannot verify protected workspace paths") from exc

        try:
            for root, directories, files, directory_fd in os.fwalk(
                ".",
                follow_symlinks=False,
                onerror=fail_walk,
                dir_fd=workspace_fd,
            ):
                root_relative = Path(root)
                root_is_protected = self._is_protected_or_nested(root_relative)
                for name in directories:
                    relative = root_relative / name
                    try:
                        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError as exc:
                        raise ProcessSandboxError(
                            "cannot verify protected workspace paths"
                        ) from exc
                    if not root_is_protected and self.policy.is_protected(relative):
                        if stat.S_ISLNK(metadata.st_mode):
                            raise ProcessSandboxError(
                                "protected workspace path is a symbolic link; process execution denied"
                            )
                        mounts.extend(("--tmpfs", str(self.workspace_root / relative)))
                        count += 1

                for name in files:
                    relative = root_relative / name
                    if not self._is_protected_or_nested(relative):
                        continue
                    try:
                        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError as exc:
                        raise ProcessSandboxError(
                            "cannot verify protected workspace paths"
                        ) from exc
                    if stat.S_ISLNK(metadata.st_mode):
                        raise ProcessSandboxError(
                            "protected workspace path is a symbolic link; process execution denied"
                        )
                    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                        raise ProcessSandboxError(
                            "protected file has hard-link aliases; process execution denied"
                        )
                    if not root_is_protected:
                        mounts.extend(
                            ("--ro-bind", "/dev/null", str(self.workspace_root / relative))
                        )
                        count += 1

                if count > 4096:
                    raise ProcessSandboxError(
                        "too many protected paths to construct a safe sandbox"
                    )
        finally:
            os.close(workspace_fd)
        return mounts

    def _is_protected_or_nested(self, relative: Path) -> bool:
        return any(
            candidate != Path(".") and self.policy.is_protected(candidate)
            for candidate in (relative, *relative.parents)
        )

    async def run(self, argv: list[str], cwd: Path, timeout_seconds: float) -> ProcessResult:
        command = self.build_command(argv, cwd)
        timeout = min(max(timeout_seconds, 0.1), self.policy.process.max_timeout_seconds)
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=lambda: self._set_resource_limits(timeout),
            )
        )
        spawn_cancellation: asyncio.CancelledError | None = None
        while not spawn.done():
            try:
                await asyncio.shield(spawn)
            except asyncio.CancelledError as exc:
                spawn_cancellation = exc
        process = spawn.result()
        if spawn_cancellation is not None:
            await self._terminate_and_reap(process, ())
            raise spawn_cancellation
        if process.stdout is None or process.stderr is None:
            await self._terminate_and_reap(process, ())
            raise ProcessSandboxError("sandbox output pipes were not created")
        stdout_task = asyncio.create_task(self._read_bounded(process.stdout))
        stderr_task = asyncio.create_task(self._read_bounded(process.stderr))
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
            stdout, stdout_truncated = await stdout_task
            stderr, stderr_truncated = await stderr_task
        except TimeoutError as exc:
            await self._terminate_and_reap(process, (stdout_task, stderr_task))
            raise ProcessSandboxError("process timed out") from exc
        except BaseException:
            await self._terminate_and_reap(process, (stdout_task, stderr_task))
            raise

        return ProcessResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    async def _terminate_and_reap(
        self,
        process: asyncio.subprocess.Process,
        readers: tuple[asyncio.Task[tuple[bytes, bool]], ...],
    ) -> None:
        """Kill the process group and wait until its process and pipe readers settle."""

        if process.returncode is None:
            self._kill_process_group(process.pid)

        async def reap() -> None:
            await process.wait()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)

        cleanup = asyncio.create_task(reap())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Reaping is a critical section. The caller re-raises the original
                # cancellation only after the OS process can no longer outlive it.
                continue
        cleanup.result()

    async def _read_bounded(self, stream: asyncio.StreamReader) -> tuple[bytes, bool]:
        limit = self.policy.process.max_output_bytes
        value = bytearray()
        truncated = False
        while chunk := await stream.read(8192):
            remaining = limit - len(value)
            if remaining > 0:
                value.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(value), truncated

    @staticmethod
    def _kill_process_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _set_resource_limits(self, timeout_seconds: float) -> None:
        """Apply inherited POSIX limits before Bubblewrap starts."""

        cpu_seconds = max(1, math.ceil(timeout_seconds) + 1)
        limits = (
            (resource.RLIMIT_CPU, cpu_seconds),
            (resource.RLIMIT_AS, self.policy.process.max_memory_bytes),
            (resource.RLIMIT_NPROC, self.policy.process.max_processes),
            (resource.RLIMIT_FSIZE, self.policy.process.max_file_bytes),
            (resource.RLIMIT_NOFILE, self.policy.process.max_open_files),
            (resource.RLIMIT_CORE, 0),
        )
        for resource_kind, value in limits:
            resource.setrlimit(resource_kind, (value, value))
