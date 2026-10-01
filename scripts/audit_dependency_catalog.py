"""Read-only audit of the reviewed dependency recipes and mobile lockfile.

This checks declarations, not runtime readiness. Browser execution is qualified
separately with browser_runtime_probe.py inside the candidate runtime image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def version_matches(spec: str, version: str) -> bool:
    """Check the exact, tilde and caret forms used by this mobile manifest.

    Unknown syntax fails closed instead of pretending to implement all npm semver.
    npm ci remains the authority for complete dependency graph resolution.
    """
    requested = re.fullmatch(r"([~^]?)([0-9]+)\.([0-9]+)\.([0-9]+)", spec)
    actual = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", version)
    if requested is None or actual is None:
        return False
    lower = tuple(int(part) for part in requested.groups()[1:])
    resolved = tuple(int(part) for part in actual.groups())
    operator = requested[1]
    if not operator:
        return resolved == lower
    if resolved < lower:
        return False
    if operator == "~":
        return resolved[:2] == lower[:2]
    if lower[0]:
        return resolved[0] == lower[0]
    if lower[1]:
        return resolved[:2] == lower[:2]
    return resolved == lower


def lock_pins(text: str) -> dict[str, str]:
    """Accept only the hash-locked, exact-version format used by these recipes."""
    pins: dict[str, str] = {}
    for entry in re.split(r"\n(?=[A-Za-z])", text):
        lines = [line.strip() for line in entry.splitlines() if line.strip()]
        lines = [line for line in lines if not line.startswith("#")]
        if not lines:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)\s*\\?", lines[0])
        if not match or len(lines) < 2:
            raise ValueError("recipe requires an exact version and SHA256 hashes")
        if any(
            not re.fullmatch(r"--hash=sha256:[a-f0-9]{64}\s*\\?", line)
            for line in lines[1:]
        ):
            raise ValueError("recipe contains an unsupported source or hash")
        name, version = normalized(match[1]), match[2]
        if name in pins:
            raise ValueError("duplicate distribution in recipe")
        pins[name] = version
    if not pins:
        raise ValueError("empty dependency recipe")
    return pins


def audit(root: Path) -> dict[str, Any]:
    worker = root / "workers/project-worker"
    errors: list[str] = []
    recipes: dict[str, Any] = {}
    combined: dict[str, str] = {}
    for name in (
        "runtime-tools.lock",
        "runtime-browser.lock",
        "runtime-python-web.lock",
    ):
        path = worker / name
        try:
            raw = path.read_bytes()
            pins = lock_pins(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            errors.append(f"{name}: invalid or missing hash-locked recipe")
            continue
        recipes[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "distributions": pins,
        }
        for package, version in pins.items():
            if package in combined and combined[package] != version:
                errors.append(f"{package}: conflicting versions across recipes")
            combined[package] = version
    try:
        raw = (worker / "dependency-catalog.json").read_bytes()
        catalog = json.loads(raw)
        if catalog.get("schema_version") != 1 or not isinstance(
            catalog.get("python_imports"), dict
        ):
            raise ValueError("invalid catalog")
        for module, item in catalog["python_imports"].items():
            package, version = item["distribution"], item["version"]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module):
                errors.append("catalog contains an invalid import name")
            if combined.get(normalized(package)) != version:
                errors.append(f"{module}: catalog version is not locked")
            if item["requirement"] != f"{package}=={version}":
                errors.append(f"{module}: requirement disagrees with catalog")
        catalog_info = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "imports": len(catalog["python_imports"]),
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        errors.append("dependency-catalog.json: invalid or missing catalog")
        catalog_info = {}
    try:
        package = json.loads((root / "mobile/package.json").read_text())
        lock_raw = (root / "mobile/package-lock.json").read_bytes()
        lock = json.loads(lock_raw)
        locked_root = lock["packages"][""]
        for section in ("dependencies", "devDependencies"):
            declared = package.get(section, {})
            if declared != locked_root.get(section, {}):
                errors.append(f"mobile {section}: manifest and lock root disagree")
            for name, spec in declared.items():
                if spec in {"latest", "*", "next"}:
                    errors.append(f"mobile {name}: floating version tag")
                installed = lock["packages"].get(f"node_modules/{name}", {})
                if not installed.get("version") or not installed.get("integrity"):
                    errors.append(f"mobile {name}: locked version/integrity missing")
                elif not version_matches(spec, installed["version"]):
                    errors.append(
                        f"mobile {name}: concrete locked version violates manifest"
                    )
        mobile_info = {
            "lock_sha256": hashlib.sha256(lock_raw).hexdigest(),
            "node_engines": package.get("engines", {}).get("node"),
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        errors.append("mobile manifest/lock: invalid or missing")
        mobile_info = {}
    return {
        "schema_version": 1,
        "status": "failed" if errors else "passed",
        "scope": "declarations_only",
        "runtime_qualified": False,
        "distribution_count": len(combined),
        "catalog": catalog_info,
        "recipes": recipes,
        "mobile": mobile_info,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    result = audit(args.root.resolve())
    print(json.dumps(result, sort_keys=True, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
