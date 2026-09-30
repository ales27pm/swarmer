"""Exercise the production interpreter flags, including sibling-module loading."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_isolated_launch_loads_release_module_not_working_directory(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    source = Path(__file__).parent
    for name in ("research_worker.py", "research_collect.py"):
        shutil.copyfile(source / name, release / name)
    hostile = tmp_path / "untrusted"
    hostile.mkdir()
    (hostile / "research_collect.py").write_text("raise RuntimeError('wrong module loaded')\n")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(release / "research_worker.py"), "--help"],
        cwd=hostile,
        env={**os.environ, "PYTHONPATH": str(hostile)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--once" in result.stdout
    assert not list(release.rglob("*.pyc"))


def test_isolated_launch_rejects_incomplete_release(tmp_path: Path) -> None:
    worker = tmp_path / "research_worker.py"
    shutil.copyfile(Path(__file__).with_name("research_worker.py"), worker)
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(worker), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "the adjacent research_collect module is required" in result.stderr
