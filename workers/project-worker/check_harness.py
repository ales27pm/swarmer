"""Trusted runtime entry point; installed in the immutable runtime image."""

from __future__ import annotations

import ast
import hashlib
import importlib.machinery
import importlib.metadata
import json
import os
import posixpath
import py_compile
import re
import shutil
import stat

# Fixed commands execute only inside the isolated runtime.
import subprocess  # nosec B404
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from typing import Any

PREFIX = "SWARMER_RUNNER_RECEIPT="
PROJECT_ROOT = Path("/workspace/project")
SOURCE_ROOT = Path("/source")
DEPENDENCY_ROOT = Path("/dependencies")
MEASUREMENT_MAX_FILES = 20_000
MEASUREMENT_MAX_BYTES = 128 * 1024 * 1024
MEASUREMENT_SECONDS = 5
RUFF_BINARY = "/usr/local/bin/ruff"
RUFF_RULES = "E9,F821,F822,F823"
DEPENDENCY_PREFIX = "SWARMER_DEPENDENCY_PREFLIGHT="
DEPENDENCY_CATALOGUE = Path(__file__).with_name("dependency-catalog.json")


def _measurement_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _measurement_tree(
    root: Path, *, dependencies: bool = False
) -> list[dict[str, Any]]:
    """Bounded content inventory. Never follow links or read special files."""
    deadline, total_bytes = time.monotonic() + MEASUREMENT_SECONDS, 0
    entries: list[dict[str, Any]] = []

    def budget() -> None:
        if (
            time.monotonic() > deadline
            or len(entries) >= MEASUREMENT_MAX_FILES
            or total_bytes > MEASUREMENT_MAX_BYTES
        ):
            raise ValueError("measurement bound exceeded")

    def walk(fd: int, prefix: str, depth: int) -> None:
        nonlocal total_bytes
        if depth > 64:
            raise ValueError("measurement depth exceeded")
        names = []
        with os.scandir(fd) as directory:
            for entry in directory:
                budget()
                names.append(entry.name)
                if len(names) > MEASUREMENT_MAX_FILES:
                    raise ValueError("measurement directory bound exceeded")
        for name in sorted(names):
            budget()
            path = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=fd)
                resolved = posixpath.normpath(posixpath.join(prefix, target))
                if dependencies and (
                    target.startswith("/")
                    or resolved == ".."
                    or resolved.startswith("../")
                ):
                    raise ValueError("dependency link is not contained")
                entries.append({"path": path, "kind": "link", "target": target})
            elif stat.S_ISDIR(info.st_mode):
                entries.append({"path": path, "kind": "directory"})
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                try:
                    walk(child, path + "/", depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                file_fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                )
                try:
                    before = os.fstat(file_fd)
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("file type changed during measurement")
                    digest = hashlib.sha256()
                    while block := os.read(file_fd, 65536):
                        total_bytes += len(block)
                        budget()
                        digest.update(block)
                    after = os.fstat(file_fd)
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                    ):
                        raise ValueError("file changed during measurement")
                    entries.append(
                        {
                            "path": path,
                            "kind": "file",
                            "bytes": after.st_size,
                            "sha256": digest.hexdigest(),
                            "mode": stat.S_IMODE(after.st_mode),
                        }
                    )
                finally:
                    os.close(file_fd)
            else:
                raise ValueError("special file cannot be measured")

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(fd, "", 0)
    finally:
        os.close(fd)
    return entries


def _measurement_source(root: Path, paths: tuple[str, ...]) -> str:
    """Hash actual copied input contents using the public project_digest encoding."""
    files, total = [], 0
    if len(paths) > 80:
        raise ValueError("source count exceeded")
    for path in paths:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = path.split("/")
            if any(p in {"", ".", ".."} for p in parts):
                raise ValueError("invalid measured input path")
            for part in parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = child
            file_fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 64_000:
                    raise ValueError("input is not a bounded file")
                data = bytearray()
                while block := os.read(file_fd, 64_001 - len(data)):
                    data.extend(block)
                    if len(data) > 64_000:
                        raise ValueError("source file exceeded its bound")
                after = os.fstat(file_fd)
                if (info.st_ino, info.st_size, info.st_mtime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValueError("input changed during measurement")
            finally:
                os.close(file_fd)
        finally:
            os.close(fd)
        total += len(data)
        if total > 1_000_000:
            raise ValueError("source byte bound exceeded")
        files.append({"path": path, "content": data.decode("utf-8")})
    return _measurement_hash(sorted(files, key=lambda file: file["path"]))


def _measurement_capture(paths: tuple[str, ...] | None) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for key, measure in (
        (
            "source",
            lambda: (
                _measurement_source(PROJECT_ROOT, paths) if paths is not None else None
            ),
        ),
        ("workspace", lambda: _measurement_hash(_measurement_tree(PROJECT_ROOT))),
        (
            "dependency",
            lambda: _measurement_hash(
                _measurement_tree(DEPENDENCY_ROOT, dependencies=True)
            ),
        ),
    ):
        try:
            values[key] = measure()
        except (OSError, ValueError, UnicodeError, RuntimeError, RecursionError):
            values[key] = None
    return values


def _measurement_finish(
    before: dict[str, str | None],
    after: dict[str, str | None],
    harness_sha256: str | None,
) -> dict[str, Any]:
    errors = []
    result: dict[str, Any] = {
        "schema_version": "project-check-observation-v1",
        "harness_sha256": harness_sha256,
    }
    for key, reason in (
        ("source", "source_unavailable"),
        ("workspace", "workspace_unavailable"),
        ("dependency", "environment_unbound"),
    ):
        result[f"{key}_before_sha256"] = before[key]
        result[f"{key}_after_sha256"] = after[key]
        if before[key] is None or after[key] is None:
            errors.append(reason)
    for key, flag in (
        ("source", "source_unchanged"),
        ("dependency", "environment_unchanged"),
    ):
        result[flag] = (
            None
            if before[key] is None or after[key] is None
            else before[key] == after[key]
        )
    if harness_sha256 is None:
        errors.append("harness_unavailable")
    result["errors"] = sorted(errors)
    return result


def dependency_catalogue() -> dict[str, Any]:
    try:
        raw = DEPENDENCY_CATALOGUE.read_bytes()
        if len(raw) > 32_000:
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return {}
        entries = value.get("python_imports")
        return entries if isinstance(entries, dict) and len(entries) <= 100 else {}
    except (OSError, ValueError):
        return {}


def _catalogue_recipe(entries: dict[str, Any], name: str) -> dict[str, Any]:
    entry = entries.get(name)
    if not isinstance(entry, dict):
        return {}
    if (
        any(
            not isinstance(entry.get(key), str) or len(entry[key]) > 200
            for key in ("distribution", "version", "requirement", "profile")
        )
        or type(entry.get("requires_browser_runtime")) is not bool
    ):
        return {}
    if entry["requirement"] != entry["distribution"] + "==" + entry["version"]:
        return {}
    return {
        "catalogue_recipe": {
            key: entry[key]
            for key in (
                "distribution",
                "version",
                "requirement",
                "profile",
                "requires_browser_runtime",
            )
        }
    }


def runtime_profile() -> dict[str, Any]:
    """Observe the trusted image only, before mounting or executing a project."""
    catalogue = dependency_catalogue()
    recipes = {
        name: recipe["catalogue_recipe"]
        for name in catalogue
        if (recipe := _catalogue_recipe(catalogue, name))
    }
    distributions = {"pytest", "ruff", "uv"} | {
        recipe["distribution"] for recipe in recipes.values()
    }
    installed = {}
    for distribution in sorted(distributions):
        try:
            installed[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
    return {
        "schema_version": 1,
        "status": "observed",
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "installed_distributions": installed,
        "binaries_present": {
            name: shutil.which(name) is not None
            for name in ("node", "chromium", "chromedriver", "firefox", "geckodriver")
        },
        "browser_execution": "not_qualified",
        "pytest_plugin_autoload": False,
        "async_tests": "unsupported",
        "catalogue_requirements": {name: recipe["requirement"] for name, recipe in recipes.items()},
    }


class _SourceImports(ast.NodeVisitor):
    """Inspect declarations only; never import a project module or dependency."""

    def __init__(self) -> None:
        self.required: list[tuple[str, int]] = []
        self.optional: list[tuple[str, int]] = []
        self.optional_depth = 0
        self.typing_names: set[str] = set()
        self.type_checking_names: set[str] = set()

    def _record(self, name: str, line: int) -> None:
        destination = self.optional if self.optional_depth else self.required
        destination.append((name.split(".", 1)[0], line))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, node.lineno)
            if alias.name == "typing":
                self.typing_names.add(alias.asname or alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or node.module is None:
            return  # Relative imports refer to project packages, not registry packages.
        self._record(node.module, node.lineno)
        if node.module == "typing":
            self.type_checking_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "TYPE_CHECKING"
            )

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        typing_only = (isinstance(test, ast.Name) and test.id in self.type_checking_names) or (
            isinstance(test, ast.Attribute)
            and isinstance(test.value, ast.Name)
            and test.value.id in self.typing_names
            and test.attr == "TYPE_CHECKING"
        )
        self.visit(test)
        if typing_only:
            self.optional_depth += 1
        for child in node.body:
            self.visit(child)
        if typing_only:
            self.optional_depth -= 1
        for child in node.orelse:
            self.visit(child)

    def visit_Try(self, node: ast.Try) -> None:
        def catches_import_error(handler: ast.ExceptHandler) -> bool:
            if handler.type is None:
                return True
            choices = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
            return any(
                isinstance(choice, ast.Name)
                and choice.id
                in {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
                for choice in choices
            )

        optional = any(catches_import_error(handler) for handler in node.handlers)
        if optional:
            self.optional_depth += 1
        for child in node.body:
            self.visit(child)
        if optional:
            self.optional_depth -= 1
        # A fallback/else branch depends on runtime control flow. Do not label
        # its imports mandatory simply because the AST contains the branch.
        self.optional_depth += 1
        for after_try in [*node.handlers, *node.orelse]:
            self.visit(after_try)
        self.optional_depth -= 1
        for final_statement in node.finalbody:
            self.visit(final_statement)


def dependency_preflight(
    root: Path | None = None,
    *,
    site_paths: tuple[Path, ...] | None = None,
    catalogue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Report missing top-level imports before pytest collection, without execution.

    Static imports are candidates, not a complete proof of runtime readiness.
    Guarded optional imports and typing-only declarations do not block execution.
    No import name is ever converted into an automatic installation command.
    """
    root = (root or PROJECT_ROOT).resolve()
    catalogue = dependency_catalogue() if catalogue is None else catalogue
    if site_paths is None:
        site_paths = tuple(
            dict.fromkeys(
                [Path("/dependencies/python")]
                + [Path(sysconfig.get_path(key)) for key in ("purelib", "platlib")]
            )
        )
    missing: list[dict[str, Any]] = []
    syntax: list[dict[str, Any]] = []
    optional_count = 0
    paths = sorted(root.rglob("*.py"))
    if len(paths) > 80:
        return {
            "schema_version": "1.0",
            "status": "unavailable",
            "reason": "source_limit",
        }
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            return {
                "schema_version": "1.0",
                "status": "unavailable",
                "reason": "source_path",
            }
        source = path.read_bytes()
        if len(source) > 64_000:
            return {
                "schema_version": "1.0",
                "status": "unavailable",
                "reason": "source_limit",
            }
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(source, filename=relative)
            imports = _SourceImports()
            imports.visit(tree)
        except (SyntaxError, RecursionError) as exc:
            syntax.append({"path": relative, "line": getattr(exc, "lineno", None)})
            continue
        optional_count += len(imports.optional)
        for name, line in dict.fromkeys(imports.required):
            if len(name) > 200:
                return {
                    "schema_version": "1.0",
                    "status": "unavailable",
                    "reason": "import_name_limit",
                }
            if name in sys.stdlib_module_names or name in sys.builtin_module_names:
                continue
            found = False
            for directory in (root, path.parent, *site_paths):
                # FileFinder checks filesystem entries directly. It neither runs
                # package __init__ nor invokes project-controlled import hooks.
                finder = importlib.machinery.FileFinder(
                    str(directory),
                    (
                        importlib.machinery.SourceFileLoader,
                        importlib.machinery.SOURCE_SUFFIXES,
                    ),
                    (
                        importlib.machinery.ExtensionFileLoader,
                        importlib.machinery.EXTENSION_SUFFIXES,
                    ),
                    (
                        importlib.machinery.SourcelessFileLoader,
                        importlib.machinery.BYTECODE_SUFFIXES,
                    ),
                )
                if finder.find_spec(name) is not None:
                    found = True
                    break
            if not found:
                missing.append(
                    {
                        "module": name,
                        "path": relative,
                        "line": line,
                        **_catalogue_recipe(catalogue, name),
                    }
                )
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "invalid_source" if syntax else "missing_dependencies" if missing else "ready",
        "missing_imports": missing,
        "syntax_errors": syntax,
        "optional_import_count": optional_count,
        "diagnostics_omitted": 0,
        "action": "Declare known distributions as exact name==version pins in requirements.txt; do not install guessed import names."
        if missing
        else None,
    }
    while len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > 6_000:
        if missing:
            missing.pop()
        elif syntax:
            syntax.pop()
        else:
            break
        result["diagnostics_omitted"] += 1
    return result


def prepare() -> None:
    # Only a private, read-only source snapshot enters this tmpfs workspace.
    shutil.copytree("/source", "/workspace/project")
    if Path("/dependencies/node_modules").is_dir():
        Path("/workspace/project/node_modules").symlink_to(
            "/dependencies/node_modules", target_is_directory=True
        )
    os.chdir("/workspace/project")
    sys.path.insert(0, "/dependencies/python")
    sys.path.insert(0, "/workspace/project")


def python_build() -> dict[str, int]:
    files = sorted(PROJECT_ROOT.rglob("*.py"))
    if not files:
        return {"exit_code": 1, "tests_executed": 0, "test_failures": 0}
    # Trusted image binary and isolated config: generated pyproject/.ruff files
    # cannot weaken the gate, replace the binary, or enable autofixes.
    print(
        "Python build: isolated Ruff E9/F821/F822/F823, then byte compilation.",
        flush=True,
    )
    lint = subprocess.run(  # nosec B603 - fixed trusted executable inside runtime
        [
            RUFF_BINARY,
            "check",
            "--isolated",
            "--no-cache",
            "--select",
            RUFF_RULES,
            "--target-version",
            "py312",
            "--output-format",
            "concise",
            str(PROJECT_ROOT),
        ],
        check=False,
        timeout=30,
    )
    if lint.returncode:
        return {"exit_code": lint.returncode, "tests_executed": 0, "test_failures": 0}
    with tempfile.TemporaryDirectory(prefix="compiled-") as compiled:
        for index, path in enumerate(files):
            py_compile.compile(str(path), cfile=str(Path(compiled) / f"{index}.pyc"), doraise=True)
    return {"exit_code": 0 if files else 1, "tests_executed": 0, "test_failures": 0}


def python_test() -> dict[str, int]:
    import pytest  # Imported from the trusted image before prepare() in main.

    preflight = dependency_preflight()
    print(DEPENDENCY_PREFIX + json.dumps(preflight, separators=(",", ":")), flush=True)
    if preflight["status"] != "ready":
        return {"exit_code": 1, "tests_executed": 0, "test_failures": 0}

    class Counter:
        executed = 0
        failures = 0

        def pytest_runtest_logreport(self, report: Any) -> None:
            if report.when == "call" and not report.skipped:
                self.executed += 1
            if report.failed:
                self.failures += 1

    counter = Counter()
    code = int(
        pytest.main(
            [
                "-q",
                "-p",
                "no:cacheprovider",
                "-c",
                "/opt/swarmer/pytest.ini",
                "--override-ini=addopts=",
                "/workspace/project",
            ],
            plugins=[counter],
        )
    )
    return {
        "exit_code": code if code or counter.executed else 5,
        "tests_executed": counter.executed,
        "test_failures": counter.failures,
    }


def node_test() -> dict[str, int]:
    process = subprocess.Popen(  # nosec B603
        ["/usr/local/bin/node", "--test", "--test-reporter=tap"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    tail = bytearray()
    if process.stdout is None:
        raise RuntimeError("Node runner did not create its output pipe")
    while chunk := process.stdout.read(4_096):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        tail.extend(chunk)
        if len(tail) > 16_000:
            del tail[:-16_000]
    code = process.wait()
    raw = tail.decode("utf-8", errors="replace")
    passed = re.findall(r"^# pass ([0-9]+)$", raw, re.MULTILINE)
    failed = re.findall(r"^# fail ([0-9]+)$", raw, re.MULTILINE)
    if not passed or not failed:
        return {"exit_code": code or 5, "tests_executed": 0, "test_failures": 0}
    failures = int(failed[-1])
    count = int(passed[-1]) + failures
    return {
        "exit_code": code if count else 5,
        "tests_executed": count,
        "test_failures": failures,
    }


def main() -> None:
    mode = sys.argv[1]
    if mode == "runtime_profile":
        print(json.dumps(runtime_profile(), separators=(",", ":")), flush=True)
        return
    if mode == "python_test":
        # Cache the trusted runner before prepare() adds project/dependency paths.
        __import__("pytest")
    prepare()
    try:
        source_entries = _measurement_tree(SOURCE_ROOT)
        if any(item["kind"] == "link" for item in source_entries):
            raise ValueError("input links cannot establish source identity")
        paths = tuple(item["path"] for item in source_entries if item["kind"] == "file")
    except (OSError, ValueError, RuntimeError, RecursionError):
        paths = None
    try:
        with Path(__file__).open("rb") as stream:
            harness = stream.read(256_001)
        harness_sha = hashlib.sha256(harness).hexdigest() if len(harness) <= 256_000 else None
    except OSError:
        harness_sha = None
    before = _measurement_capture(paths)
    try:
        if mode == "python_build":
            result = python_build()
        elif mode == "python_test":
            result = python_test()
        elif mode == "node_build":
            package = json.loads(Path("package.json").read_text())
            if "build" not in package.get("scripts", {}):
                print("No npm build script is declared.")
                result = {"exit_code": 1, "tests_executed": 0, "test_failures": 0}
            else:
                code = subprocess.call(  # nosec B603
                    ["/usr/local/bin/npm", "run", "build", "--ignore-scripts"]
                )
                result = {"exit_code": code, "tests_executed": 0, "test_failures": 0}
        elif mode == "node_test":
            # The Node test runner owns the TAP summary. Project npm scripts are
            # never accepted as proof that tests were collected and executed.
            result = node_test()
        else:
            raise ValueError("unknown fixed check profile")
    except (
        OSError,
        ValueError,
        py_compile.PyCompileError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"Check failed: {type(exc).__name__}: {str(exc)[:1_000]}")
        result = {"exit_code": 1, "tests_executed": 0, "test_failures": 1}
    observed = {**result, "observation": _measurement_finish(
        before, _measurement_capture(paths), harness_sha)}
    print("\n" + PREFIX + json.dumps(observed, separators=(",", ":")), flush=True)
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()
