"""Consent-bound generated source staging; no model-selected paths or compiler credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

MAX_SOURCE_RESPONSE_BYTES = 8_000_000
MAX_FILES = 80
MAX_FILE_BYTES = 64_000
MAX_PROJECT_BYTES = 1_000_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_RESERVED = frozenset(
    {
        ".git",
        ".build",
        ".swarmer-swift-runs",
        ".ssh",
        ".aws",
        ".codex",
        ".venv",
        "node_modules",
        ".npmrc",
        ".pypirc",
        "auth.json",
        ".env",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
    }
)


class SnapshotError(ValueError):
    """The claimed operation does not identify a currently approved source snapshot."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SnapshotError(message)


def project_reference(value: object) -> dict[str, str]:
    _require(
        isinstance(value, dict)
        and set(value) == {"validation_id", "project_id", "revision_id", "sha256"},
        "invalid project revision reference",
    )
    for key in ("validation_id", "project_id", "revision_id"):
        _require(
            isinstance(value[key], str) and _IDENTIFIER.fullmatch(value[key]) is not None,
            "invalid project revision identifier",
        )
    _require(
        isinstance(value["sha256"], str) and _DIGEST.fullmatch(value["sha256"]) is not None,
        "invalid project revision digest",
    )
    return dict(value)


def claimed_target(payload: object) -> tuple[dict[str, str], dict[str, Any]]:
    _require(isinstance(payload, dict), "project Swift arguments must be an object")
    reference = project_reference(payload.get("project_revision"))
    kind = payload.get("kind")
    fields = {"kind", "source_sha256", "project_revision"}
    if kind == "xcode":
        fields |= {"project", "scheme", "destination"}
    _require(
        kind in ("swiftpm", "xcode") and set(payload) == fields,
        "invalid project Swift argument fields",
    )
    _require(
        isinstance(payload["source_sha256"], str)
        and _DIGEST.fullmatch(payload["source_sha256"]) is not None,
        "invalid source digest",
    )
    if kind == "xcode":
        for key, pattern in {
            "project": r"[A-Za-z0-9_ .-]+\.(?:xcodeproj|xcworkspace)",
            "scheme": r"[A-Za-z0-9_][A-Za-z0-9_ .-]{0,99}",
            "destination": r"[A-Za-z0-9_-]{1,64}",
        }.items():
            _require(
                isinstance(payload[key], str) and re.fullmatch(pattern, payload[key]) is not None,
                "invalid Swift target",
            )
    return reference, {k: v for k, v in payload.items() if k != "project_revision"}


def validate_files(files: object) -> list[dict[str, str]]:
    _require(
        isinstance(files, list) and 1 <= len(files) <= MAX_FILES, "invalid snapshot file count"
    )
    paths: set[str] = set()
    directory_case: dict[str, str] = {}
    total = 0
    normalized = []
    for item in files:
        _require(
            isinstance(item, dict) and set(item) == {"path", "content"},
            "invalid snapshot file fields",
        )
        path, content = item["path"], item["content"]
        _require(
            isinstance(path, str)
            and 1 <= len(path) <= 240
            and re.fullmatch(r"[A-Za-z0-9_.@/-]+", path) is not None
            and not PurePosixPath(path).is_absolute()
            and all(p not in {"", ".", ".."} for p in path.split("/")),
            "noncanonical snapshot path",
        )
        parts = path.split("/")
        for part in parts:
            folded = part.casefold()
            _require(
                folded not in _RESERVED and not folded.endswith((".pem", ".p12", ".pfx", ".key")),
                "reserved snapshot path",
            )
            _require(
                not folded.startswith(".env")
                or folded in {".env.example", ".env.sample", ".env.template"},
                "reserved environment file",
            )
        folded_path = path.casefold()
        _require(folded_path not in paths, "duplicate or case-colliding snapshot path")
        paths.add(folded_path)
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            key = prefix.casefold()
            _require(
                key not in directory_case or directory_case[key] == prefix,
                "case-colliding snapshot directory",
            )
            directory_case[key] = prefix
        _require(
            isinstance(content, str)
            and not any(ord(c) < 32 and c not in "\t\r\n" for c in content),
            "invalid snapshot text",
        )
        size = len(content.encode("utf-8"))
        _require(size <= MAX_FILE_BYTES, "snapshot file exceeds UTF-8 byte limit")
        total += size
        _require(total <= MAX_PROJECT_BYTES, "snapshot exceeds total UTF-8 byte limit")
        normalized.append({"path": path, "content": content})
    for path in paths:
        _require(
            not any(parent.as_posix() in paths for parent in PurePosixPath(path).parents),
            "snapshot file conflicts with directory",
        )
    return normalized


def validate_response(response: object, payload: dict[str, Any]) -> list[dict[str, str]]:
    reference, target = claimed_target(payload)
    _require(
        isinstance(response, dict)
        and set(response) == {"project_revision", "source_sha256", "target", "files"},
        "invalid source response fields",
    )
    _require(
        len(json.dumps(response, ensure_ascii=True, allow_nan=False).encode())
        <= MAX_SOURCE_RESPONSE_BYTES,
        "source response exceeds byte limit",
    )
    _require(
        project_reference(response["project_revision"]) == reference
        and response["target"] == target
        and response["source_sha256"] == target["source_sha256"],
        "source response does not match claimed revision and target",
    )
    files = validate_files(response["files"])
    raw = json.dumps(
        sorted(files, key=lambda f: f["path"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _require(hashlib.sha256(raw).hexdigest() == reference["sha256"], "project JSON digest differs")
    return files


def private_root(root: Path) -> Path:
    _require(not root.is_symlink(), "staging root cannot be a symlink")
    absolute = root.absolute()
    _require(
        not any(parent.is_symlink() for parent in absolute.parents),
        "staging root ancestors cannot be symlinks",
    )
    metadata = root.stat()
    _require(
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_mode & 0o777 == 0o700,
        "staging root must be an operator-owned 0700 directory",
    )
    return root.resolve(strict=True)


class SnapshotWorkspace:
    def __init__(
        self,
        root: Path,
        *,
        workspace_factory: Callable[..., Any],
        source_digest: Callable[[Path], str],
        runner: Callable[..., int],
        destinations: dict[str, str] | None,
        timeout: float,
    ) -> None:
        self.root = private_root(root)
        self.workspace_factory, self.source_digest = workspace_factory, source_digest
        self.runner, self.destinations, self.timeout = runner, destinations, timeout

    def stage(self, files: list[dict[str, str]], expected: str) -> Path:
        private_root(self.root)
        # A fresh exclusive 0700 parent makes publication a private, per-job operation.
        container = Path(tempfile.mkdtemp(prefix="project-", dir=self.root))
        pending, complete = container / ".building", container / "source"
        pending.mkdir(mode=0o700)
        for item in files:
            path = pending / item["path"]
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for parent in (path.parent, *path.parent.parents):
                if parent == container:
                    break
                _require(not parent.is_symlink() and parent.is_dir(), "invalid staging directory")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "nonregular staged file")
                stream.write(item["content"].encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        _require(self.source_digest(pending) == expected, "staged filesystem digest differs")
        _require(
            not complete.exists() and not complete.is_symlink(),
            "snapshot destination already exists",
        )
        pending.rename(complete)
        _require(self.source_digest(complete) == expected, "published filesystem digest differs")
        return complete

    def execute_job(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        source_request: Callable[[], Any],
        ensure_active: Callable[[], None],
    ) -> dict[str, Any]:
        _require(operation in {"build", "test"}, "unsupported project Swift operation")
        reference, target = claimed_target(payload)
        canonical = {**target, "project_revision": reference}
        ensure_active()
        files = validate_response(source_request(), canonical)
        root = self.stage(files, target["source_sha256"])
        last_validation = time.monotonic()

        def refresh(*, force: bool = False) -> None:
            nonlocal last_validation
            ensure_active()
            now = time.monotonic()
            if force or now - last_validation >= 5:
                validate_response(source_request(), canonical)
                last_validation = time.monotonic()
            ensure_active()

        def guarded_runner(argv, cwd, log, timeout, active):
            refresh(force=True)
            return self.runner(argv, cwd, log, timeout, active)

        workspace = self.workspace_factory(
            root,
            approved_source_sha256=target["source_sha256"],
            destinations=self.destinations,
            runner=guarded_runner,
            timeout=self.timeout,
        )
        result = workspace.execute(operation, target, ensure_active=refresh)
        refresh(force=True)
        result["project_revision"] = reference
        result["request_sha256"] = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt = root / result["artifact_directory"] / "receipt.json"
        _require(not receipt.is_symlink() and receipt.is_file(), "invalid Swift receipt artifact")
        receipt.write_text(json.dumps(result, indent=2) + "\n")
        return result
