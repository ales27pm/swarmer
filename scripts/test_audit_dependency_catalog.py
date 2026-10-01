import importlib.util
import json
import shutil
from pathlib import Path

import pytest

SOURCE = Path(__file__).with_name("audit_dependency_catalog.py")
SPEC = importlib.util.spec_from_file_location("catalog_audit", SOURCE)
assert SPEC and SPEC.loader
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


@pytest.mark.parametrize(
    "bad",
    [
        "selenium>=4",
        "selenium==4.50.0",
        "selenium @ https://example.org/x.whl",
        "--extra-index-url https://example.org",
        "x==1 \\\n --hash=md5:abcd",
        "",
    ],
)
def test_unlocked_or_external_recipes_are_rejected(bad):
    with pytest.raises(ValueError):
        audit_module.lock_pins(bad)


def fixture_repo(tmp_path):
    root = SOURCE.parents[1]
    for relative in [
        "workers/project-worker/runtime-tools.lock",
        "workers/project-worker/runtime-browser.lock",
        "workers/project-worker/runtime-python-web.lock",
        "workers/project-worker/dependency-catalog.json",
        "mobile/package.json",
        "mobile/package-lock.json",
    ]:
        dest = tmp_path / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, dest)
    return tmp_path


def test_real_catalog_matches_hash_locked_recipes(tmp_path):
    result = audit_module.audit(fixture_repo(tmp_path))
    assert result["status"] == "passed", result["errors"]
    assert result["runtime_qualified"] is False


def test_catalog_version_drift_is_rejected(tmp_path):
    root = fixture_repo(tmp_path)
    path = root / "workers/project-worker/dependency-catalog.json"
    catalog = json.loads(path.read_text())
    catalog["python_imports"]["selenium"]["version"] = "0.0.0"
    path.write_text(json.dumps(catalog))
    result = audit_module.audit(root)
    assert result["status"] == "failed"
    assert "selenium: catalog version is not locked" in result["errors"]


def test_mobile_manifest_drift_is_rejected(tmp_path):
    root = fixture_repo(tmp_path)
    path = root / "mobile/package.json"
    package = json.loads(path.read_text())
    package["devDependencies"]["typescript"] = "latest"
    path.write_text(json.dumps(package))
    result = audit_module.audit(root)
    assert result["status"] == "failed"
    assert "mobile typescript: floating version tag" in result["errors"]


def test_concrete_lock_version_drift_is_rejected(tmp_path):
    root = fixture_repo(tmp_path)
    path = root / "mobile/package-lock.json"
    lock = json.loads(path.read_text())
    lock["packages"]["node_modules/typescript"]["version"] = "0.0.0"
    path.write_text(json.dumps(lock))
    result = audit_module.audit(root)
    assert result["status"] == "failed"
    assert (
        "mobile typescript: concrete locked version violates manifest"
        in result["errors"]
    )


@pytest.mark.parametrize(
    "spec,version,expected",
    [
        ("5.9.3", "5.9.3", True),
        ("5.9.3", "5.9.4", False),
        ("~55.0.31", "55.0.32", True),
        ("~55.0.31", "55.1.0", False),
        ("~55.0.31", "55.0.30", False),
        ("^9.39.0", "9.40.0", True),
        ("^9.39.0", "10.0.0", False),
        ("^0.4.15", "0.4.16", True),
        ("^0.4.15", "0.5.0", False),
        ("^0.0.1", "0.0.2", False),
        (">=1", "2.0.0", False),
        ("latest", "5.9.3", False),
    ],
)
def test_supported_version_constraints(spec, version, expected):
    assert audit_module.version_matches(spec, version) is expected
