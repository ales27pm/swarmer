from __future__ import annotations

import json
from pathlib import Path

import check_harness
import pytest


def inspect(root: Path, source: str, *, catalogue=None, site_paths=()):
    (root / "test_app.py").write_text(source)
    return check_harness.dependency_preflight(
        root, site_paths=site_paths, catalogue=catalogue or {}
    )


def test_missing_selenium_names_only_the_known_catalogue_recipe(tmp_path: Path) -> None:
    recipe = {
        "distribution": "selenium",
        "version": "4.50.0",
        "requirement": "selenium==4.50.0",
        "profile": "browser",
        "requires_browser_runtime": True,
    }
    result = inspect(tmp_path, "from selenium import webdriver\n", catalogue={"selenium": recipe})
    assert result["status"] == "missing_dependencies"
    assert result["missing_imports"] == [
        {
            "module": "selenium",
            "path": "test_app.py",
            "line": 1,
            "catalogue_recipe": recipe,
        }
    ]
    assert "browser_available" not in json.dumps(result)


def test_unknown_import_does_not_guess_distribution_or_version(tmp_path: Path) -> None:
    result = inspect(tmp_path, "import unknown_example_library\n")
    assert result["missing_imports"] == [
        {"module": "unknown_example_library", "path": "test_app.py", "line": 1}
    ]
    assert "do not install guessed import names" in result["action"]


@pytest.mark.parametrize(
    "local_path", ["application.py", "application/__init__.py", "application/tools.py"]
)
def test_local_modules_and_namespace_packages_are_not_registry_dependencies(
    tmp_path: Path, local_path: str
) -> None:
    file = tmp_path / local_path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("VALUE = 42\n")
    result = inspect(tmp_path, "import application\nfrom pathlib import Path\nimport json\n")
    assert result["status"] == "ready" and not result["missing_imports"]


@pytest.mark.parametrize(
    "source",
    [
        "try:\n import optional_lib\nexcept ImportError:\n pass\n",
        "try:\n import optional_lib\nexcept (ModuleNotFoundError, ValueError):\n pass\n",
        "from typing import TYPE_CHECKING as TC\nif TC:\n import optional_lib\n",
        "import typing as t\nif t.TYPE_CHECKING:\n import optional_lib\n",
    ],
)
def test_explicit_optional_imports_do_not_block_tests(tmp_path: Path, source: str) -> None:
    result = inspect(tmp_path, source)
    assert result["status"] == "ready"
    assert result["optional_import_count"] == 1


def test_non_import_handler_does_not_hide_a_required_dependency(tmp_path: Path) -> None:
    result = inspect(tmp_path, "try:\n import required_lib\nexcept ValueError:\n pass\n")
    assert result["status"] == "missing_dependencies"


def test_source_and_installed_package_initializers_are_never_executed(
    tmp_path: Path,
) -> None:
    site = tmp_path / "installed"
    package = site / "present_lib"
    package.mkdir(parents=True)
    marker = tmp_path / "executed"
    code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    (package / "__init__.py").write_text(code)
    project = tmp_path / "project"
    project.mkdir()
    result = inspect(project, code + "import present_lib\n", site_paths=(site,))
    assert result["status"] == "ready"
    assert not marker.exists()


def test_syntax_is_reported_before_pytest_collection(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "test_app.py").write_text("def broken(:\n pass\n")
    monkeypatch.setattr(check_harness, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pytest, "main", lambda *a, **kw: pytest.fail("must not collect"))
    result = check_harness.python_test()
    assert result == {"exit_code": 1, "tests_executed": 0, "test_failures": 0}
    receipt = json.loads(capsys.readouterr().out.split(check_harness.DEPENDENCY_PREFIX)[1])
    assert receipt["status"] == "invalid_source"
    assert receipt["syntax_errors"] == [{"path": "test_app.py", "line": 1}]


def test_missing_dependency_blocks_collection_without_executing_project(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    (tmp_path / "test_app.py").write_text(
        "import definitely_absent_test_dependency\nraise RuntimeError('not executed')\n"
    )
    monkeypatch.setattr(check_harness, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pytest, "main", lambda *a, **kw: pytest.fail("must not collect"))
    result = check_harness.python_test()
    assert result["exit_code"] == 1 and result["tests_executed"] == 0
    assert '"status":"missing_dependencies"' in capsys.readouterr().out


@pytest.mark.parametrize("exit_code,expected", [(0, 5), (2, 2), (4, 4)])
def test_collection_errors_are_not_relabelled_empty_tests(
    monkeypatch, capsys, exit_code, expected
) -> None:
    monkeypatch.setattr(check_harness, "dependency_preflight", lambda: {"status": "ready"})
    monkeypatch.setattr(pytest, "main", lambda *a, **kw: exit_code)
    assert check_harness.python_test()["exit_code"] == expected


def test_diagnostics_are_bounded_without_claiming_all_missing_imports_visible(
    tmp_path: Path,
) -> None:
    source = "\n".join(f"import package_{i:03}_" + "x" * 180 for i in range(60))
    result = inspect(tmp_path, source)
    assert result["status"] == "missing_dependencies"
    assert result["diagnostics_omitted"] > 0
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 6000


@pytest.mark.parametrize("contents", [None, "not JSON", '{"schema_version":2,"python_imports":{}}'])
def test_old_or_invalid_catalogue_is_unavailable_not_an_installation_guess(
    tmp_path: Path, monkeypatch, contents
) -> None:
    path = tmp_path / "catalogue.json"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setattr(check_harness, "DEPENDENCY_CATALOGUE", path)
    assert check_harness.dependency_catalogue() == {}


def test_runtime_profile_mode_never_prepares_project_or_imports_pytest(monkeypatch, capsys):
    import builtins
    import runpy
    import sys

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"pytest", "selenium", "playwright"}:
            pytest.fail("profile must not import third-party code")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(sys, "argv", ["check_harness.py", "runtime_profile"])
    runpy.run_path(check_harness.__file__, run_name="__main__")
    profile = json.loads(capsys.readouterr().out)
    assert profile["status"] == "observed"
    assert profile["browser_execution"] == "not_qualified"
    assert profile["pytest_plugin_autoload"] is False
    assert profile["async_tests"] == "unsupported"


def test_profile_catalogue_is_not_installed_package_proof(monkeypatch):
    catalogue = {
        "selenium": {
            "distribution": "selenium",
            "version": "4.50.0",
            "requirement": "selenium==4.50.0",
            "profile": "browser",
            "requires_browser_runtime": True,
        }
    }
    monkeypatch.setattr(check_harness, "dependency_catalogue", lambda: catalogue)

    def version(name):
        if name == "pytest":
            return "8.4.2"
        raise check_harness.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(check_harness.importlib.metadata, "version", version)
    monkeypatch.setattr(check_harness.shutil, "which", lambda name: "/usr/bin/" + name)
    profile = check_harness.runtime_profile()
    assert profile["installed_distributions"] == {"pytest": "8.4.2"}
    assert profile["catalogue_requirements"]["selenium"] == catalogue["selenium"]["requirement"]
    assert profile["binaries_present"]["chromium"] is True
    assert profile["browser_execution"] == "not_qualified"


@pytest.mark.parametrize(
    "source",
    [
        "try:\n import json\nexcept ImportError:\n import unavailable_fallback\n",
        "try:\n import optional_lib\nexcept ImportError:\n pass\nelse:\n import conditional_feature\n",
    ],
)
def test_conditional_fallbacks_do_not_become_mandatory_imports(tmp_path, source):
    assert inspect(tmp_path, source)["status"] == "ready"
