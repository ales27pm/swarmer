import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def load_worker() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "workers" / "file-worker" / "file_worker.py"
    spec = importlib.util.spec_from_file_location("sample_file_worker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sample_file_worker_lists_only_safe_root_entries(tmp_path: Path) -> None:
    worker = load_worker()
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    result = worker.execute(
        tmp_path, {"required_skill": "workspace.list_dir", "payload": {"path": "."}}
    )
    assert result == {"entries": ["README.md"]}


def test_sample_file_worker_rejects_escape_and_protected_paths(tmp_path: Path) -> None:
    worker = load_worker()
    with pytest.raises(ValueError, match="protected or invalid path"):
        worker.safe_path(tmp_path, ".env")
    with pytest.raises(ValueError, match="path escapes worker root"):
        worker.safe_path(tmp_path, "..")
