"""Static-policy unit/integration checks; no browser or generated source executes.

Calculated arguments remain outside this narrow detector's documented scope.
These tests do not claim that arbitrary generated code must use a browser sandbox.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import project_worker as worker
import pytest
import runtime
from project_contract import snapshot_sha
from test_project_worker import Generator, payload, step


def policy_error(line: int) -> str:
    return (
        f"browser_sandbox_disabled: tests/test_ui.py:{line}. "
        "Static browser preflight blocked execution; no tests ran. "
        "Remove --no-sandbox and keep Chromium sandbox enabled."
    )


def make_runner(
    monkeypatch: pytest.MonkeyPatch, *, browser_sandbox: bool = True
) -> runtime.DockerRunner:
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(runtime.os, "getuid", lambda: 1000)
    return runtime.DockerRunner("sha256:" + "a" * 64, browser_sandbox=browser_sandbox)


def fixed_runner_receipts(
    monkeypatch: pytest.MonkeyPatch, runner: runtime.DockerRunner
) -> list[list[str]]:
    commands: list[list[str]] = []

    def process(
        command: list[str],
        directory: Path,
        timeout: float,
        ensure_active: Callable[[], None],
    ) -> tuple[int, str, int]:
        ensure_active()
        commands.append(command)
        if command[-1] in {"python_build", "python_test"}:
            count = int(command[-1] == "python_test")
            receipt = {
                "exit_code": 0,
                "tests_executed": count,
                "test_failures": 0,
            }
            return 0, runtime.RECEIPT_PREFIX + json.dumps(receipt), 1
        return 0, "", 1

    monkeypatch.setattr(runner, "_process", process)
    return commands


@pytest.mark.parametrize(
    "import_line,setup,call",
    [
        (
            "from selenium import webdriver",
            "options = webdriver.ChromeOptions()",
            'options.add_argument("--no-sandbox")',
        ),
        (
            "from selenium.webdriver.chrome.options import Options as BrowserOptions",
            "options = BrowserOptions()",
            'options.add_argument("--no-sandbox=true")',
        ),
        (
            "import selenium.webdriver as webdriver",
            "options = webdriver.ChromeOptions()",
            'options.add_argument("--no-sandbox=false")',
        ),
        (
            "from playwright.sync_api import sync_playwright",
            "with sync_playwright() as playwright:",
            "    playwright.chromium.launch(chromium_sandbox=False)",
        ),
        (
            "from playwright.sync_api import sync_playwright as browser_api",
            "with browser_api() as playwright:",
            '    playwright.chromium.launch_persistent_context("/tmp/profile", chromium_sandbox=False)',
        ),
        (
            "from selenium.webdriver import ChromeOptions",
            "original = ChromeOptions()\noptions = original",
            'options.add_argument("--no-sandbox")',
        ),
        (
            "from playwright.async_api import async_playwright",
            "async def browser_check():\n    async with async_playwright() as playwright:",
            "        playwright.chromium.launch(chromium_sandbox=False)",
        ),
    ],
)
def test_explicit_disable_is_rejected_before_any_process_or_install(
    monkeypatch: pytest.MonkeyPatch, import_line: str, setup: str, call: str
) -> None:
    runner = make_runner(monkeypatch)
    source = (
        import_line
        + '\nprivate_metadata = "do-not-copy-this-metadata"\n'
        + setup
        + "\n"
        + call
        + "\n"
    )
    files = [
        {"path": "tests/test_ui.py", "content": source},
        {"path": "requirements.txt", "content": "Flask==3.1.2\n"},
    ]
    original = copy.deepcopy(files)

    def no_process(*args: Any, **kwargs: Any) -> None:
        pytest.fail(
            "browser-policy rejection must precede installs, checks and cleanup"
        )

    monkeypatch.setattr(runner, "_process", no_process)
    with pytest.raises(runtime.RuntimeError) as error:
        runner.run(files, "python", [], lambda: None)
    assert str(error.value) == policy_error(len(source.splitlines()))
    assert "do-not-copy-this-metadata" not in str(error.value)
    assert files == original


@pytest.mark.parametrize(
    "source",
    [
        'from selenium import webdriver\noptions = webdriver.ChromeOptions()\noptions.add_argument("--headless=new")\n',
        "from playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n    p.chromium.launch(chromium_sandbox=True)\n",
        'from playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n    p.chromium.launch_persistent_context("/tmp/profile", chromium_sandbox=True)\n',
        '''"""options.add_argument('--no-sandbox')"""
from selenium import webdriver
# options.add_argument('--no-sandbox')
text = "options.add_argument('--no-sandbox')"
metadata = {"chromium_sandbox": False, "flag": "--no-sandbox"}
options = webdriver.ChromeOptions()
options.add_argument("--headless=new")
''',
        'import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument("--no-sandbox")\n',
        'import argparse\nimport selenium\nparser = argparse.ArgumentParser()\nparser.add_argument("--no-sandbox", action="store_true")\n',
        "import playwright\nclass Application:\n    def launch(self, **kwargs):\n        return kwargs\napplication = Application()\napplication.launch(chromium_sandbox=False)\n",
        'from selenium import webdriver\noptions = webdriver.ChromeOptions()\ndef configure(options):\n    options.add_argument("--no-sandbox")\n',
        'import argparse\nfrom selenium import webdriver\noptions = webdriver.ChromeOptions()\noptions = argparse.ArgumentParser()\noptions.add_argument("--no-sandbox")\n',
        'import argparse\nfrom selenium import webdriver\noptions = argparse.ArgumentParser()\nclass Application:\n    options = webdriver.ChromeOptions()\n    def configure(self):\n        options.add_argument("--no-sandbox")\n',
    ],
)
def test_safe_calls_and_inert_mentions_reach_the_unchanged_fixed_runner(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    runner = make_runner(monkeypatch)
    commands = fixed_runner_receipts(monkeypatch, runner)
    result = runner.run(
        [{"path": "tests/test_ui.py", "content": source}],
        "python",
        [],
        lambda: None,
    )
    checks = [
        command
        for command in commands
        if command[-1] in {"python_build", "python_test"}
    ]
    assert [command[-1] for command in checks] == ["python_build", "python_test"]
    assert result["build_passed"] and result["tests_executed"] == 1
    assert result["test_failures"] == 0
    assert all(command[command.index("--network") + 1] == "none" for command in checks)
    assert checks[0][checks[0].index("--pids-limit") + 1] == "64"
    assert checks[1][checks[1].index("--pids-limit") + 1] == "256"
    assert any(argument.startswith("seccomp=") for argument in checks[1])


def test_browser_profile_disabled_preserves_existing_runner_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(monkeypatch, browser_sandbox=False)
    commands = fixed_runner_receipts(monkeypatch, runner)
    result = runner.run(
        [
            {
                "path": "tests/test_ui.py",
                "content": 'from selenium import webdriver\noptions = webdriver.ChromeOptions()\noptions.add_argument("--no-sandbox")\n',
            }
        ],
        "python",
        [],
        lambda: None,
    )
    checks = [
        command
        for command in commands
        if command[-1] in {"python_build", "python_test"}
    ]
    assert result["tests_executed"] == 1 and result["build_passed"]
    assert len(checks) == 2
    assert all(command[command.index("--pids-limit") + 1] == "64" for command in checks)
    assert not any(
        argument.startswith("seccomp=") for command in checks for argument in command
    )


def test_class_body_outside_narrow_detector_scope_does_not_crash() -> None:
    # A declared limitation, not proof that this configuration is sandboxed.
    runtime.check_explicit_browser_policy(
        [
            {
                "path": "tests/test_ui.py",
                "content": (
                    "from selenium import webdriver\n"
                    "class BrowserFixture:\n"
                    "    options = webdriver.ChromeOptions()\n"
                    '    options.add_argument("--no-sandbox")\n'
                ),
            }
        ]
    )


def test_deep_alias_origins_are_unknown_without_recursion_crash() -> None:
    source = (
        "from selenium.webdriver.chrome.options import Options\n"
        "v0 = Options()\n"
        + "".join(f"v{i} = v{i - 1}" + ".x" * 60 + "\n" for i in range(1, 23))
        + 'v22.add_argument("--no-sandbox")\n'
    )
    assert len(source.encode()) < 3_100
    # Unknown origin is outside the detector's coverage, not verified sandbox use.
    runtime.check_explicit_browser_policy(
        [{"path": "tests/test_ui.py", "content": source}]
    )


def test_policy_rejection_survives_as_project_check_without_executing_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(monkeypatch)
    calls = 0

    def no_process(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        pytest.fail("an unsafe proposal must not start a process")

    monkeypatch.setattr(runner, "_process", no_process)
    files = [{"path": "index.html", "content": "<!doctype html><title>TODO</title>"}]
    data = {
        **payload(),
        "files": files,
        "base_revision_id": "revision_browser_policy",
        "base_sha256": snapshot_sha(files),
    }
    original = copy.deepcopy(data)
    proposed = {
        "path": "tests/test_ui.py",
        "content": (
            "from selenium import webdriver\n"
            "options = webdriver.ChromeOptions()\n"
            'options.add_argument("--no-sandbox")\n'
        ),
    }
    result = worker.run_iteration(
        data,
        Generator(step(action="complete", edits=[proposed])),
        runner,
        lambda: None,
    )
    assert calls == 0
    assert result["action"] == "continue"
    assert result["checks"] == [
        {
            "command": ["swarmer", "project-checks"],
            "status": "failed",
            "exit_code": 1,
            "output": policy_error(3),
            "duration_ms": 0,
        }
    ]
    assert result["files"] == [*files, proposed]
    assert result["base_revision_id"] == data["base_revision_id"]
    assert result["base_sha256"] == data["base_sha256"]
    assert data == original
