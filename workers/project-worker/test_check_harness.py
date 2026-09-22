import shutil
import sys
from pathlib import Path

import check_harness
import pytest


@pytest.mark.parametrize(
    "source,expected",
    [
        ("def compute():\n    return undefined_value\n", False),
        ("import os\ndef compute():\n    return 42\n", True),
        ("def broken(:\n    pass\n", False),
    ],
)
def test_real_python_gate_ignores_generated_config(
    tmp_path: Path, monkeypatch, source: str, expected: bool
) -> None:
    sibling = Path(sys.executable).with_name("ruff")
    ruff = str(sibling) if sibling.is_file() else shutil.which("ruff")
    assert ruff, "Ruff must be installed for runtime qualification"
    (tmp_path / "app.py").write_text(source)
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nignore = ["ALL"]\n')
    monkeypatch.setattr(check_harness, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(check_harness, "RUFF_BINARY", ruff)
    receipt = check_harness.python_build()
    assert (receipt["exit_code"] == 0) is expected
    assert receipt["tests_executed"] == 0


def test_empty_python_project_is_not_a_successful_build(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(check_harness, "PROJECT_ROOT", tmp_path)
    assert check_harness.python_build()["exit_code"] != 0
