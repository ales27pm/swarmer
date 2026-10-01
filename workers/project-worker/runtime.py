"""Bounded project dependency/build/test execution in disposable Docker containers."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil

# Structured Docker argv, never a shell.
import subprocess  # nosec B404
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from project_contract import ProjectError, files_value

MAX_OUTPUT_BYTES = 64_000
CHECK_COMMANDS = {
    "python_build": ["python", "-m", "compileall", "-q", "."],
    "python_test": ["python", "-m", "pytest", "-q"],
    "node_build": ["npm", "run", "build"],
    "node_test": ["node", "--test"],
}
DEPENDENCY_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{0,99}")
NODE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?")
RECEIPT_PREFIX = "SWARMER_RUNNER_RECEIPT="
BROWSER_SECCOMP_PATH = Path(__file__).resolve().with_name("browser-seccomp-v1.json")
BROWSER_SECCOMP_SHA256 = "0f8bd34cf9980268f45c7f0a2a0d5dbff9559b3baf4d3cc4b464872e175a8c88"


def browser_seccomp_bytes() -> bytes:
    """Load only the operator-reviewed adjacent profile, never project data."""
    try:
        if (
            BROWSER_SECCOMP_PATH.is_symlink()
            or BROWSER_SECCOMP_PATH.resolve(strict=True) != BROWSER_SECCOMP_PATH
            or not BROWSER_SECCOMP_PATH.is_file()
        ):
            raise ValueError("browser seccomp profile must be a regular adjacent file")
        with BROWSER_SECCOMP_PATH.open("rb") as source:
            data = source.read(64_001)
    except OSError as exc:
        raise ValueError("the reviewed browser seccomp profile is unavailable") from exc
    if len(data) > 64_000 or hashlib.sha256(data).hexdigest() != BROWSER_SECCOMP_SHA256:
        raise ValueError("browser seccomp profile does not match the reviewed SHA256")
    return data


class RuntimeError(ProjectError):
    """The isolated project runtime could not establish a valid check result."""


def check_explicit_browser_policy(files: list[dict[str, str]]) -> None:
    """Reject recognized sandbox-disabling calls without importing project code.

    This is an adherence check for explicit Python browser configuration, not
    execution-time enforcement or analysis of dynamically constructed arguments.
    The independent container isolation remains necessary.
    """

    def inspect_source(item: dict[str, str]) -> None:
        if not item["path"].endswith(".py"):
            return
        try:
            tree = ast.parse(item["content"], filename=item["path"])
        except (SyntaxError, ValueError, RecursionError):
            # Existing build/dependency preflight reports invalid Python source.
            return
        # Only unique lexical bindings establish provenance. Rebinding, function
        # parameters, wildcard imports and unsupported assignments are unknown;
        # a familiar variable or method name alone never identifies a browser.
        bindings: dict[int, dict[str, list[tuple[str, str | ast.AST | None]]]] = {}
        parents: dict[int, int | None] = {}
        scopes: dict[int, int] = {}

        def bind(scope: int, name: str, kind: str, value: str | ast.AST | None) -> None:
            bindings[scope].setdefault(name, []).append((kind, value))

        def collect(node: ast.AST, scope: int) -> None:
            if isinstance(node, ast.ClassDef):
                # Class namespaces/descriptors need different name lookup.
                # Keep them outside this explicit, conservative check.
                bind(scope, node.name, "unknown", None)
                return
            if isinstance(
                node,
                (
                    ast.Module,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.Lambda,
                ),
            ):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    bind(scope, node.name, "unknown", None)
                parent = scope if not isinstance(node, ast.Module) else None
                scope = id(node)
                parents[scope] = parent
                bindings[scope] = {}
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                ):
                    for arg in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    ):
                        bind(scope, arg.arg, "unknown", None)
                    for optional_arg in (node.args.vararg, node.args.kwarg):
                        if optional_arg is not None:
                            bind(scope, optional_arg.arg, "unknown", None)
            scopes[id(node)] = scope
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bind(
                        scope,
                        alias.asname or alias.name.split(".")[0],
                        "import",
                        alias.name if alias.asname else alias.name.split(".")[0],
                    )
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    bind(
                        scope,
                        name,
                        "import" if not node.level and name != "*" else "unknown",
                        f"{node.module}.{alias.name}",
                    )
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    for child in ast.walk(target):
                        if isinstance(child, ast.Name) and isinstance(
                            child.ctx, ast.Store
                        ):
                            bind(
                                scope,
                                child.id,
                                "assign" if target is child else "unknown",
                                node.value,
                            )
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for entry in node.items:
                    if entry.optional_vars is not None:
                        for child in ast.walk(entry.optional_vars):
                            if isinstance(child, ast.Name) and isinstance(
                                child.ctx, ast.Store
                            ):
                                bind(
                                    scope,
                                    child.id,
                                    "manager"
                                    if entry.optional_vars is child
                                    else "unknown",
                                    entry.context_expr,
                                )
            elif isinstance(
                node,
                (
                    ast.For,
                    ast.AsyncFor,
                    ast.comprehension,
                    ast.AugAssign,
                    ast.NamedExpr,
                ),
            ):
                for child in ast.walk(node.target):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                        bind(scope, child.id, "unknown", None)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                for name in node.names:
                    bind(scope, name, "unknown", None)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bind(scope, node.name, "unknown", None)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Del):
                bind(scope, node.id, "unknown", None)
            elif isinstance(node, ast.MatchAs) and node.name:
                bind(scope, node.name, "unknown", None)
            for child in ast.iter_child_nodes(node):
                collect(child, scope)

        try:
            collect(tree, id(tree))
        except RecursionError:
            return
        options = {
            "selenium.webdriver.ChromeOptions",
            "selenium.webdriver.chrome.options.Options",
        }
        managers = {
            "playwright.sync_api.sync_playwright",
            "playwright.async_api.async_playwright",
        }

        def origin(
            node: ast.AST, scope: int, seen: frozenset[tuple[int, str]] = frozenset()
        ) -> str | None:
            if len(seen) > 24:
                return None
            if isinstance(node, ast.Name):
                current: int | None = scope
                while current is not None:
                    values = bindings[current]
                    if "*" in values:
                        return None
                    if node.id in values:
                        key = (current, node.id)
                        if len(values[node.id]) != 1 or key in seen:
                            return None
                        kind, value = values[node.id][0]
                        if kind == "import" and isinstance(value, str):
                            return value
                        if kind == "assign" and isinstance(value, ast.AST):
                            return origin(value, current, seen | {key})
                        if (
                            kind == "manager"
                            and isinstance(value, ast.Call)
                            and origin(value.func, current, seen | {key}) in managers
                        ):
                            return "@playwright-manager"
                        return None
                    current = parents[current]
                return None
            if isinstance(node, ast.Attribute):
                receiver = origin(node.value, scope, seen)
                return receiver + "." + node.attr if receiver else None
            if isinstance(node, ast.Call) and origin(node.func, scope, seen) in options:
                return "@selenium-options"
            return None

        for node in ast.walk(tree):
            if id(node) not in scopes:
                continue
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            try:
                receiver = origin(node.func.value, scopes[id(node)])
            except RecursionError:
                # Chained aliases can exceed recursion depth across otherwise
                # shallow ASTs. Unknown provenance must not crash the worker.
                continue
            disabled = (
                receiver == "@selenium-options"
                and node.func.attr == "add_argument"
                and any(
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and arg.value.partition("=")[0] == "--no-sandbox"
                    for arg in node.args
                )
            )
            disabled |= (
                receiver == "@playwright-manager.chromium"
                and node.func.attr in {"launch", "launch_persistent_context"}
                and any(
                    keyword.arg == "chromium_sandbox"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                    for keyword in node.keywords
                )
            )
            if disabled:
                raise RuntimeError(
                    f"browser_sandbox_disabled: {item['path']}:{node.lineno}. "
                    "Static browser preflight blocked execution; no tests ran. "
                    "Remove --no-sandbox and keep Chromium sandbox enabled."
                )

    for item in files:
        inspect_source(item)


def dependency_manifests(files: list[dict[str, str]]) -> tuple[str, dict[str, Any] | None]:
    by_path = {item["path"]: item["content"] for item in files}
    requirements = by_path.get("requirements.txt", "")
    normalized = []
    for line in requirements.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("==")
        if (
            len(parts) != 2
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?", parts[0])
            or not DEPENDENCY_VERSION.fullmatch(parts[1])
        ):
            raise RuntimeError(
                "requirements.txt must contain only exact name==version registry pins"
            )
        normalized.append(line)
    if len(normalized) > 60:
        raise RuntimeError("Python dependency count exceeds its limit")
    package = None
    if "package.json" in by_path:
        try:
            original = json.loads(by_path["package.json"])
        except (ValueError, RecursionError) as exc:
            raise RuntimeError("package.json is invalid JSON") from exc
        if not isinstance(original, dict):
            raise RuntimeError("package.json must be an object")
        package = {"name": "swarmer-private-project", "version": "1.0.0", "private": True}
        for section in ("dependencies", "devDependencies"):
            dependencies = original.get(section, {})
            if not isinstance(dependencies, dict) or len(dependencies) > 60:
                raise RuntimeError("Node dependency count exceeds its limit")
            for name, version in dependencies.items():
                if (
                    not re.fullmatch(r"(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+", name)
                    or not isinstance(version, str)
                    or not NODE_VERSION.fullmatch(version)
                ):
                    raise RuntimeError("Node dependencies require exact registry version pins")
            package[section] = dependencies
    return "\n".join(normalized) + ("\n" if normalized else ""), package


def profiles_for(runtime: str, requested: list[list[str]]) -> list[str]:
    if runtime not in {"python", "node", "python_node"}:
        raise RuntimeError("unsupported project runtime")
    profiles = []
    if runtime in {"python", "python_node"}:
        profiles.extend(["python_build", "python_test"])
    if runtime in {"node", "python_node"}:
        profiles.extend(["node_build", "node_test"])
    aliases = [
        *CHECK_COMMANDS.values(),
        ["pytest", "-q"],
        ["pytest"],
        ["python", "-m", "pytest"],
        ["npm", "test"],
        # Dependency setup already runs through sanitized registry-only manifests.
        # These exact aliases request that existing stage; no supplied argv runs.
        ["python", "-m", "pip", "install", "-r", "requirements.txt"],
        ["pip", "install", "-r", "requirements.txt"],
        ["npm", "install"],
        ["npm", "ci"],
    ]
    if any(command not in aliases for command in requested):
        raise RuntimeError("requested checks must use the documented fixed Python/Node profiles")
    return profiles


def parse_receipt(raw: str, mode: str, returncode: int) -> tuple[int, int, int]:
    markers = [
        line[len(RECEIPT_PREFIX) :] for line in raw.splitlines() if line.startswith(RECEIPT_PREFIX)
    ]
    if not markers:
        raise RuntimeError("isolated check did not return a runner receipt")
    try:
        receipt = json.loads(markers[-1])
        code, count, failures = (
            receipt["exit_code"],
            receipt["tests_executed"],
            receipt["test_failures"],
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("isolated check receipt is invalid") from exc
    if any(type(value) is not int for value in (code, count, failures)) or code != returncode:
        raise RuntimeError("isolated check receipt disagrees with the container exit status")
    if count < 0 or failures < 0 or count > 100_000 or failures > 100_000:
        raise RuntimeError("isolated test counts exceed their bounds")
    if mode.endswith("_test") and code == 0 and (count == 0 or failures):
        raise RuntimeError("successful test receipt requires nonempty passing tests")
    return code, count, failures


class DockerRunner:
    def __init__(
        self, image: str, *, timeout_seconds: float = 45, browser_sandbox: bool = False
    ) -> None:
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("project runtime must be pinned to a local Docker image SHA256 ID")
        if not 5 <= timeout_seconds <= 120:
            raise ValueError("project check timeout must be between 5 and 120 seconds")
        if os.getuid() == 0:
            raise ValueError("project worker must run as an unprivileged operator account")
        binary = shutil.which("docker")
        if binary is None:
            raise ValueError("the operator Docker CLI is unavailable")
        self.docker = str(Path(binary).resolve())
        self.image = image
        self.timeout_seconds = timeout_seconds
        if type(browser_sandbox) is not bool:
            raise ValueError("browser sandbox activation must be an explicit boolean")
        self._browser_seccomp = browser_seccomp_bytes() if browser_sandbox else None

    def probe_profile(self) -> dict[str, Any]:
        """Cache bounded image facts; old images remain explicitly unqualified."""
        cached = getattr(self, "_profile_cache", None)
        if cached is not None and cached[0] == self.image:
            return json.loads(json.dumps(cached[1]))  # type: ignore[no-any-return]
        profile: dict[str, Any] = {
            "schema_version": 1,
            "status": "unknown",
            "image_id": self.image,
            "browser_execution": "not_qualified",
            "pytest_plugin_autoload": False,
            "async_tests": "unsupported",
        }
        with tempfile.TemporaryDirectory(prefix="swarmer-runtime-profile-") as temporary:
            directory = Path(temporary)
            name = "swarmer-runtime-profile-" + uuid.uuid4().hex
            try:
                command = self._base(name, "none") + [
                    self.image,
                    "python",
                    "-I",
                    "/opt/swarmer/check_harness.py",
                    "runtime_profile",
                ]
                code, raw, _duration = self._process(command, directory, 10, lambda: None)
                if code == 0 and len(raw.encode()) <= 16_000:
                    value = json.loads(raw)
                    if (
                        isinstance(value, dict)
                        and value.get("schema_version") == 1
                        and value.get("status") == "observed"
                        and value.get("browser_execution") == "not_qualified"
                        and value.get("pytest_plugin_autoload") is False
                        and value.get("async_tests") == "unsupported"
                        and isinstance(value.get("python_version"), str)
                        and re.fullmatch(
                            r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", value["python_version"]
                        )
                    ):
                        installed = value.get("installed_distributions")
                        recipes = value.get("catalogue_requirements")
                        binaries = value.get("binaries_present")
                        if (
                            isinstance(installed, dict)
                            and isinstance(recipes, dict)
                            and isinstance(binaries, dict)
                            and len(installed) <= 100
                            and len(recipes) <= 100
                            and set(binaries)
                            <= {"node", "chromium", "chromedriver", "firefox", "geckodriver"}
                            and all(type(present) is bool for present in binaries.values())
                            and all(
                                isinstance(key, str)
                                and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", key)
                                and isinstance(text, str)
                                and re.fullmatch(r"[A-Za-z0-9_.+\-=]{1,200}", text)
                                for mapping in (installed, recipes)
                                for key, text in mapping.items()
                            )
                        ):
                            profile.update(
                                status="observed",
                                python_version=value["python_version"],
                                installed_distributions=installed,
                                catalogue_requirements=recipes,
                                binaries_present=binaries,
                            )
            except (OSError, RuntimeError, ValueError, RecursionError, subprocess.TimeoutExpired):
                pass
            finally:
                self._cleanup(["rm", "--force", name], directory)
        self._profile_cache = (self.image, profile)
        return json.loads(json.dumps(profile))  # type: ignore[no-any-return]

    def _base(self, name: str, network: str) -> list[str]:
        # This mount is a private tmpfs in each disposable container.
        temporary_mount = "/tmp:rw,nosuid,nodev,size=256m,mode=1777"  # nosec B108
        return [
            self.docker,
            "run",
            "--rm",
            "--name",
            name,
            "--pull",
            "never",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            "--cpus",
            "2",
            "--ulimit",
            "nofile=256:256",
            "--ulimit",
            "fsize=268435456:268435456",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            temporary_mount,
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,size=256m,mode=1777",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        ]

    def _process(
        self,
        command: list[str],
        directory: Path,
        timeout: float,
        ensure_active: Callable[[], None],
    ) -> tuple[int, str, int]:
        started = time.monotonic()
        # The Docker CLI receives an empty private config, not worker environment
        # credentials. Containers receive only explicit nonsecret --env values.
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(directory),
            "DOCKER_CONFIG": str(directory / "docker-config"),
        }
        output = bytearray()
        tail = bytearray()
        truncated = False
        process = subprocess.Popen(  # nosec B603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            cwd=directory,
            start_new_session=True,
        )

        def read_output() -> None:
            nonlocal truncated
            if process.stdout is None:
                return
            while chunk := process.stdout.read(4_096):
                remaining = MAX_OUTPUT_BYTES - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                tail.extend(chunk)
                if len(tail) > 16_000:
                    del tail[:-16_000]

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                ensure_active()
                if time.monotonic() - started > timeout:
                    raise RuntimeError("isolated operation exceeded its wall-clock limit")
                time.sleep(0.1)
            ensure_active()
            reader.join(timeout=2)
            raw = output.decode("utf-8", errors="replace")
            if truncated:
                raw += "\n[output truncated]\n" + tail.decode("utf-8", errors="replace")
            return process.returncode, raw, int((time.monotonic() - started) * 1_000)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()

    def _cleanup(self, command: list[str], directory: Path) -> None:
        try:
            self._process([self.docker, *command], directory, 10, lambda: None)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass

    def run(
        self,
        files: list[dict[str, str]],
        runtime: str,
        requested_checks: list[list[str]],
        ensure_active: Callable[[], None],
    ) -> dict[str, Any]:
        files = files_value(files)
        if self._browser_seccomp is not None:
            check_explicit_browser_policy(files)
        profiles = profiles_for(runtime, requested_checks)
        requirements, package = dependency_manifests(files)
        checks: list[dict[str, Any]] = []
        tests_executed = 0
        failures = 0
        build_passed = True
        with tempfile.TemporaryDirectory(prefix="swarmer-project-") as temporary:
            directory = Path(temporary)
            source, dependencies, manifests = (
                directory / name for name in ("source", "deps", "manifests")
            )
            for path in (source, dependencies, manifests):
                path.mkdir(mode=0o755)
            for item in files:
                target = source / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(item["content"], encoding="utf-8")
            identifier = "swarmer-project-" + uuid.uuid4().hex
            network, proxy = identifier + "-net", identifier + "-proxy"
            containers: list[str] = []
            try:
                installs: list[tuple[list[str], list[str]]] = []
                if requirements:
                    (manifests / "requirements.txt").write_text(requirements)
                    installs.append(
                        (
                            ["python", "-m", "pip", "install", "-r", "requirements.txt"],
                            [
                                "python",
                                "-I",
                                "-m",
                                "pip",
                                "--isolated",
                                "install",
                                "--disable-pip-version-check",
                                "--no-cache-dir",
                                "--only-binary=:all:",
                                "--index-url",
                                "https://pypi.org/simple",
                                "--proxy",
                                f"http://{proxy}:8080",
                                "--target",
                                "/dependencies/python",
                                "-r",
                                "/manifests/requirements.txt",
                            ],
                        )
                    )
                if package is not None:
                    node_manifest = manifests / "node"
                    node_manifest.mkdir()
                    (node_manifest / "package.json").write_text(json.dumps(package))
                    installs.append(
                        (
                            ["npm", "install", "--ignore-scripts"],
                            [
                                "npm",
                                "install",
                                "--ignore-scripts",
                                "--engine-strict",
                                "--no-audit",
                                "--no-fund",
                                # Keep registry downloads off the 256 MiB /tmp
                                # tmpfs. This private job directory is removed
                                # with the dependencies on success or failure.
                                "--cache",
                                "/dependencies/.npm-cache",
                                "--registry=https://registry.npmjs.org",
                                f"--https-proxy=http://{proxy}:8080",
                                f"--proxy=http://{proxy}:8080",
                                "--prefix",
                                "/dependencies",
                            ],
                        )
                    )
                    # Only sanitized dependency metadata enters the networked stage.
                    shutil.copyfile(node_manifest / "package.json", dependencies / "package.json")
                if installs:
                    code, _, _ = self._process(
                        [self.docker, "network", "create", "--internal", network],
                        directory,
                        15,
                        ensure_active,
                    )
                    if code:
                        raise RuntimeError("could not create the private dependency network")
                    proxy_command = self._base(proxy, "bridge")
                    proxy_command[1] = "create"
                    proxy_command.remove("--rm")
                    proxy_command.extend(
                        [self.image, "python", "-I", "/opt/swarmer/registry_proxy.py"]
                    )
                    containers.append(proxy)
                    for command in (
                        proxy_command,
                        [self.docker, "network", "connect", network, proxy],
                        [self.docker, "start", proxy],
                    ):
                        code, _, _ = self._process(command, directory, 15, ensure_active)
                        if code:
                            raise RuntimeError("could not start the restricted registry proxy")
                for index, (display, actual) in enumerate(installs):
                    name = identifier + f"-install-{index}"
                    containers.append(name)
                    command = self._base(name, network) + [
                        "--mount",
                        f"type=bind,source={dependencies},target=/dependencies",
                        "--mount",
                        f"type=bind,source={manifests},target=/manifests,readonly",
                        self.image,
                        *actual,
                    ]
                    code, raw, duration = self._process(command, directory, 90, ensure_active)
                    checks.append(self._check(display, code, raw, duration))
                    if code:
                        return {
                            "checks": checks,
                            "tests_executed": 0,
                            "test_failures": 0,
                            "build_passed": False,
                        }
                # Dependency containers and their egress proxy end before code runs.
                self._cleanup(["rm", "--force", proxy], directory)
                profile_path = None
                if self._browser_seccomp is not None:
                    profile_path = directory / "browser-seccomp-v1.json"
                    # CLI reads the verified copy from private host scratch. It
                    # is never exposed in the project's source/dependency mounts.
                    with profile_path.open("xb") as profile_file:
                        profile_file.write(self._browser_seccomp)
                    profile_path.chmod(0o600)
                for index, mode in enumerate(profiles):
                    name = identifier + f"-check-{index}"
                    containers.append(name)
                    command = self._base(name, "none")
                    if profile_path is not None and mode in {"python_test", "node_test"}:
                        command[command.index("--pids-limit") + 1] = "256"
                        command.extend(["--security-opt", f"seccomp={profile_path}"])
                    command += [
                        "--mount",
                        f"type=bind,source={source},target=/source,readonly",
                        "--mount",
                        f"type=bind,source={dependencies},target=/dependencies,readonly",
                        self.image,
                        "python",
                        "-I",
                        "/opt/swarmer/check_harness.py",
                        mode,
                    ]
                    raw = ""
                    check_started = time.monotonic()
                    try:
                        code, raw, duration = self._process(
                            command, directory, self.timeout_seconds, ensure_active
                        )
                        code, count, failed = parse_receipt(raw, mode, code)
                        tests_executed += count
                        failures += failed
                        raw += f"\nRunner: {count} tests executed; {failed} failures. Runtime {self.image}."
                    except RuntimeError as exc:
                        self._cleanup(["rm", "--force", name], directory)
                        code = 1
                        raw += "\n" + str(exc)
                        duration = int((time.monotonic() - check_started) * 1_000)
                        count = 0
                    checks.append(self._check(CHECK_COMMANDS[mode], code, raw, duration))
                    if mode.endswith("_build") and code:
                        build_passed = False
                return {
                    "checks": checks,
                    "tests_executed": tests_executed,
                    "test_failures": failures,
                    "build_passed": build_passed,
                }
            finally:
                for container in containers:
                    self._cleanup(["rm", "--force", container], directory)
                self._cleanup(["network", "rm", network], directory)

    @staticmethod
    def _check(command: list[str], code: int, output: str, duration: int) -> dict[str, Any]:
        # Keep the diagnostic beginning and the final receipt, never an unbounded log.
        cleaned = "".join(
            character for character in output if ord(character) >= 32 or character in "\t\n\r"
        )
        if len(cleaned) > 8_000:
            cleaned = cleaned[:3_800] + "\n[output truncated]\n" + cleaned[-4_100:]
        return {
            "command": command,
            "status": "passed" if code == 0 else "failed",
            "exit_code": max(-255, min(255, code)),
            "output": cleaned,
            "duration_ms": duration,
        }
