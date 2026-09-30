#!/usr/bin/env python3
"""Deterministic, text-only source snapshots with bounded, independently readable parts.

Inventory is Git-tracked working-tree files plus exact explicit untracked paths.
This never runs exported project code, discovers untracked files, or overwrites output.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import stat

# Subprocesses are fixed read-only Git queries; no shell or project code is executed.
import subprocess  # nosec B404
from pathlib import Path, PurePosixPath
from typing import Any

FORMAT = "SWARMER_TEXT_SOURCE_V2"
MAX_PART_BYTES = 5_000_000
MAX_FILE_BYTES = 64_000_000
MAX_SNAPSHOT_BYTES = 512_000_000
MAX_FILES = 10_000
MAX_PARTS = 10_000
BANNER = b"SWARMER / monGARS - TEXT SOURCE SNAPSHOT V2\n"
END_FILE = b"\n@@END_FILE@@\n"
END_SNAPSHOT = b"@@END_SNAPSHOT@@\n"
DENIED_DIRS = {
    ".git",
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "pods",
    "carthage",
    "build",
    "dist",
    "deriveddata",
    ".build",
    ".expo",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "logs",
    "workspace",
    "workspaces",
    "secrets",
    "credentials",
    "private",
    "exports",
}
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
    ".jsonl",
    ".swift",
    ".sh",
    ".bash",
    ".zsh",
    ".yaml",
    ".yml",
    ".example",
    ".mmd",
    ".txt",
    ".modelfile",
    ".podspec",
    ".h",
    ".hpp",
    ".c",
    ".cc",
    ".cpp",
    ".m",
    ".mm",
    ".html",
    ".css",
    ".scss",
    ".svg",
    ".xml",
    ".plist",
    ".xcconfig",
    ".xcscheme",
    ".pbxproj",
    ".toml",
    ".feature",
    ".lock",
    ".sql",
    ".rs",
    ".go",
    ".graphql",
    ".proto",
    ".properties",
    ".ini",
    ".conf",
    ".ps1",
    ".kt",
    ".java",
    ".gradle",
}
TEXT_NAMES = {
    "dockerfile",
    "caddyfile",
    "makefile",
    "license",
    "notice",
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    ".npmrc",
    ".nvmrc",
    ".node-version",
    "modelfile",
}
SECRET_NAMES = re.compile(
    r"^(?:credentials?|secrets?|tokens?|auth)(?:[-_][^.]+)?\.(?:json|yaml|yml|toml|ini|conf|txt|env)$|"
    r"^(?:prod|production)(?:[._-][^.]+)*\.(?:json|yaml|yml|toml|ini|conf|env)$",
    re.IGNORECASE,
)
SECRET_CONTENT = re.compile(
    rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|"
    rb"\b(?:ghp_|github_pat_|glpat-|hf_)[A-Za-z0-9_-]{20,}\b|"
    rb"\b(?:sk_live_|rk_live_)[A-Za-z0-9]{16,}\b|\bAKIA[A-Z0-9]{16}\b|"
    rb"(?im:^\s*//[^\r\n]*:_authToken\s*=\s*[^$\s][^\r\n]{12,})"
)


class ExportError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or len(value) > 1024
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ExportError("Expected a normalized, relative source path without traversal")
    return value


def safe_version(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) or ".." in value:
        raise ExportError("Version must be a plain release label, without paths or traversal")
    return value


def git(repo: Path, *arguments: str) -> bytes:
    # Fixed Git operations with separate argv and no shell.
    result = subprocess.run(  # nosec B603
        [git_executable(), "--literal-pathspecs", "-C", str(repo), *arguments],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ExportError(f"Git inventory command failed: {arguments[0]}")
    return result.stdout


def git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ExportError("Git is required to enumerate source inputs")
    return executable


def read_regular(root: Path, relative: str, limit: int) -> tuple[bytes, int]:
    """Open every parent by descriptor; never follow a replaced directory symlink."""
    parts = PurePosixPath(safe_path(relative)).parts
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            before = os.fstat(source)
            if not stat.S_ISREG(before.st_mode):
                raise ExportError("Selected path is not a regular file")
            if before.st_size > limit:
                raise ExportError("Selected file exceeds the allowed byte limit")
            chunks = []
            count = 0
            while True:
                chunk = os.read(source, min(1_048_576, limit + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > limit:
                    raise ExportError("Selected file exceeds the allowed byte limit")
            after = os.fstat(source)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
            if any(getattr(before, field) != getattr(after, field) for field in fields):
                raise ExportError("Source changed while it was being read; freeze and retry")
            return b"".join(chunks), 0o755 if before.st_mode & 0o111 else 0o644
        finally:
            os.close(source)
    finally:
        os.close(descriptor)


def exclusion(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    names = [part.lower() for part in parts]
    name = names[-1]
    if any(part.startswith(("swarmer-complete-source", "swarmer-source-")) for part in names):
        return "generated_source_export"
    if any(part in DENIED_DIRS for part in names[:-1]):
        return "private_runtime_build_or_vendor"
    if "data" in names[:-1] and path != "server/app/data/activity_catalog.json":
        return "runtime_data"
    if name.startswith(".env") and not name.endswith(".example"):
        return "private_configuration"
    if SECRET_NAMES.fullmatch(name) or PurePosixPath(name).suffix in {
        ".pem",
        ".key",
        ".p8",
        ".p12",
        ".pfx",
        ".mobileprovision",
        ".keystore",
        ".jks",
    }:
        return "sensitive_path"
    if name not in TEXT_NAMES and PurePosixPath(name).suffix not in TEXT_SUFFIXES:
        return "not_source_text"
    return None


def source_inventory(repo: Path, include: list[str]) -> tuple[dict[str, Any], list[bytes]]:
    repo = repo.resolve(strict=True)
    if Path(git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve() != repo:
        raise ExportError("--repo must be the Git repository root")
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    index = git(repo, "ls-files", "--stage", "-z")
    selected: dict[str, str] = {}
    excluded: list[dict[str, str]] = []
    for record in index.split(b"\0"):
        if not record:
            continue
        info, raw_path = record.split(b"\t", 1)
        index_mode, _, stage = info.decode().split()
        path = safe_path(raw_path.decode("utf-8"))
        if stage != "0":
            raise ExportError("Resolve Git index conflicts before exporting")
        if index_mode not in {"100644", "100755"}:
            excluded.append({"path": path, "reason": "symlink_or_submodule"})
        else:
            selected[path] = "tracked"
    explicit = {safe_path(path) for path in include}
    for path in sorted(explicit):
        if path in selected:
            continue
        if any(entry["path"] == path for entry in excluded):
            raise ExportError(f"Explicit inclusion is not a regular tracked source: {path}")
        # The exact path follows --; no shell or project code is executed.
        result = subprocess.run(  # nosec B603
            [git_executable(), "-C", str(repo), "check-ignore", "--quiet", "--", path],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            raise ExportError(f"Explicit inclusion is ignored by Git: {path}")
        if result.returncode != 1:
            raise ExportError("Could not check the explicit source path against Git ignores")
        selected[path] = "explicit"
    if len(selected) > MAX_FILES:
        raise ExportError("Source inventory exceeds the file limit")
    files, payloads = [], []
    seen = set()
    total = 0
    for path, origin in sorted(selected.items()):
        if path.casefold() in seen:
            raise ExportError("Case-insensitive source path collision")
        seen.add(path.casefold())
        reason = exclusion(path)
        raw = b""
        mode = 0o644
        if reason is None:
            try:
                raw, mode = read_regular(repo, path, MAX_FILE_BYTES)
            except FileNotFoundError:
                reason = "missing_from_working_tree"
            except OSError as exc:
                if exc.errno not in {errno.ELOOP, errno.ENOTDIR}:
                    raise
                reason = "symlink_or_non_directory_parent"
            if reason is None:
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    reason = "non_utf8_binary"
                if b"\0" in raw:
                    reason = "binary_nul"
                if SECRET_CONTENT.search(raw):
                    reason = "sensitive_content"
        if reason:
            if path in explicit:
                raise ExportError(f"Explicit inclusion is excluded ({reason}): {path}")
            excluded.append({"path": path, "reason": reason})
            continue
        total += len(raw)
        if total > MAX_SNAPSHOT_BYTES:
            raise ExportError("Source snapshot exceeds the total byte limit")
        files.append(
            {"path": path, "origin": origin, "bytes": len(raw), "sha256": digest(raw), "mode": mode}
        )
        payloads.append(raw)
    if not files:
        raise ExportError("No eligible source files selected")
    if git(repo, "ls-files", "--stage", "-z") != index:
        raise ExportError("Git inventory changed during export; freeze and retry")
    return {
        "git_commit": commit,
        "selection": "current tracked working-tree bytes plus exact explicit paths; not a deployment receipt",
        "files": files,
        "excluded": sorted(excluded, key=lambda entry: entry["path"]),
        "source_bytes": total,
        "inventory_sha256": digest(canonical(files)),
    }, payloads


def snapshot_bytes(inventory: dict[str, Any], payloads: list[bytes], version: str) -> bytes:
    metadata = {key: inventory[key] for key in ("git_commit", "inventory_sha256")}
    metadata.update(format=FORMAT, version=version)
    chunks = [BANNER, canonical(metadata) + b"\n"]
    for entry, payload in zip(inventory["files"], payloads, strict=True):
        chunks.extend((b"@@FILE@@\n", canonical(entry) + b"\n", payload, END_FILE))
    chunks.append(END_SNAPSHOT)
    result = b"".join(chunks)
    if len(result) > MAX_SNAPSHOT_BYTES:
        raise ExportError("Framed source snapshot exceeds the total byte limit")
    return result


def part_header(manifest: dict[str, Any], index: int, count: int, offset: int) -> bytes:
    return (
        f"SWARMER / monGARS - TEXT SOURCES {manifest['version']}\n"
        f"Part {index:05d} of {count:05d} - UTF-8 text only\n"
        f"Source set: {manifest['inventory_sha256']}\n"
        f"Snapshot byte offset: {offset}\n"
        "Keep this set together. Use export_source.py verify/extract; do not concatenate headers.\n"
        "The source below may continue a file from the preceding part.\n\n"
    ).encode()


def split_snapshot(data: bytes, manifest: dict[str, Any], maximum: int) -> list[tuple[int, bytes]]:
    reserve = len(part_header(manifest, MAX_PARTS, MAX_PARTS, len(data)))
    budget = maximum - reserve
    if budget < 4:
        raise ExportError("Part byte limit is too small for its readable header")
    pieces = []
    offset = 0
    while offset < len(data):
        end = min(offset + budget, len(data))
        if end < len(data):
            while end > offset and data[end] & 0xC0 == 0x80:
                end -= 1
            newline = data.rfind(b"\n", offset + budget // 2, end)
            if newline >= 0:
                end = newline + 1
        if end <= offset:
            raise ExportError("Unable to split source at a UTF-8 boundary")
        pieces.append((offset, data[offset:end]))
        if len(pieces) > MAX_PARTS:
            raise ExportError("Source snapshot would require too many parts")
        offset = end
    return pieces


def write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        os.fchmod(stream.fileno(), mode)


def write_relative(root: Path, relative: str, data: bytes, mode: int) -> None:
    parts = PurePosixPath(safe_path(relative)).parts
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        output = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=descriptor,
        )
        with os.fdopen(output, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
    finally:
        os.close(descriptor)


def new_directory(path: Path) -> Path:
    # Resolve only the existing parent. mkdir itself must fail if the target exists.
    target = path.parent.resolve(strict=True) / path.name
    if path.name in {"", ".", ".."}:
        raise ExportError("Output must name a new directory")
    target.mkdir(mode=0o700, exist_ok=False)
    return target


def create_export(repo: Path, output: Path, version: str, include: list[str], maximum: int) -> Path:
    version = safe_version(version)
    if not 1024 <= maximum <= MAX_PART_BYTES:
        raise ExportError("Part limit must be between 1024 and 5,000,000 bytes")
    inventory, payloads = source_inventory(repo, include)
    snapshot = snapshot_bytes(inventory, payloads, version)
    manifest = {
        **inventory,
        "format": FORMAT,
        "version": version,
        "max_part_bytes": maximum,
        "snapshot_bytes": len(snapshot),
        "snapshot_sha256": digest(snapshot),
    }
    prefix = f"swarmer-source-{version}-{inventory['git_commit'][:8]}-{inventory['inventory_sha256'][:12]}"
    pieces = split_snapshot(snapshot, manifest, maximum)
    parts, outputs = [], []
    for index, (offset, payload) in enumerate(pieces, 1):
        header = part_header(manifest, index, len(pieces), offset)
        raw = header + payload
        name = f"{prefix}.part-{index:05d}-of-{len(pieces):05d}.txt"
        if not 0 < len(raw) <= maximum:
            raise ExportError("Part exceeded its declared byte limit")
        raw.decode("utf-8")
        parts.append(
            {
                "name": name,
                "bytes": len(raw),
                "sha256": digest(raw),
                "header_bytes": len(header),
                "payload_offset": offset,
            }
        )
        outputs.append((name, raw))
    manifest["parts"] = parts
    manifest_name = f"{prefix}.manifest.json"
    manifest_data = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    )
    if len(manifest_data) > MAX_PART_BYTES:
        raise ExportError("Manifest exceeds the five-megabyte limit")
    readme = (
        f"monGARS / Swarmer - source export {version}\n\n"
        f"{len(parts)} readable UTF-8 parts; each at most {maximum:,} bytes.\n"
        f"{len(inventory['files'])} selected text files; {len(inventory['excluded'])} exclusions listed in the manifest.\n"
        "Binary assets, private configuration, dependencies and generated outputs are excluded.\n"
        "This is a source snapshot, not a build, deployment or iPhone-opening receipt.\n"
        "Review the explicit inventory before sharing. Pattern screening cannot certify absence of all secrets.\n\n"
        "From a trusted checkout (Python 3.12+), keeping all parts beside the manifest:\n"
        f"python3 scripts/export_source.py verify /path/to/{manifest_name}\n"
        f"python3 scripts/export_source.py reconstruct /path/to/{manifest_name} --output /path/to/NEW-source.txt\n"
        f"python3 scripts/export_source.py extract /path/to/{manifest_name} --output-dir /path/to/NEW-source-tree\n\n"
        "Outputs must not exist. Verification checks each part and each source file, including exact UTF-8 bytes and executable mode.\n"
        "Extraction only writes files; it never runs project code or installs dependencies. Do not mix versions.\n"
    ).encode()
    target = new_directory(output)
    for name, raw in outputs:
        write_relative(target, name, raw, 0o600)
    write_relative(target, f"{prefix}.README.txt", readme, 0o600)
    # Publish the inventory only after every part has been written.
    write_relative(target, manifest_name, manifest_data, 0o600)
    verify_export(target / manifest_name)
    return target / manifest_name


def verify_export(path: Path) -> tuple[dict[str, Any], bytes, list[bytes]]:
    root = path.parent.resolve(strict=True)
    raw_manifest, _ = read_regular(root, path.name, MAX_PART_BYTES)
    manifest = json.loads(raw_manifest)
    if manifest.get("format") != FORMAT:
        raise ExportError("Unsupported source export format")
    safe_version(manifest["version"])
    if not re.fullmatch(r"[0-9a-f]{40,64}", manifest["git_commit"]):
        raise ExportError("Invalid source commit identity")
    if not 1024 <= manifest["max_part_bytes"] <= MAX_PART_BYTES:
        raise ExportError("Invalid part byte limit")
    files, parts = manifest["files"], manifest["parts"]
    if not 0 < len(files) <= MAX_FILES or not 0 < len(parts) <= MAX_PARTS:
        raise ExportError("Invalid source inventory size")
    if digest(canonical(files)) != manifest["inventory_sha256"]:
        raise ExportError("Source inventory digest mismatch")
    seen_files: set[str] = set()
    for entry in files:
        name = safe_path(entry["path"])
        if name.casefold() in seen_files or any(
            name.casefold().startswith(old + "/") or old.startswith(name.casefold() + "/")
            for old in seen_files
        ):
            raise ExportError("Duplicate or colliding source path")
        if exclusion(name) is not None or entry["mode"] not in {0o644, 0o755}:
            raise ExportError("Invalid exported source path or mode")
        if type(entry["bytes"]) is not int or not 0 <= entry["bytes"] <= MAX_FILE_BYTES:
            raise ExportError("Invalid source byte count")
        seen_files.add(name.casefold())
    if not 0 < manifest["snapshot_bytes"] <= MAX_SNAPSHOT_BYTES:
        raise ExportError("Invalid snapshot byte count")
    seen_parts = set()
    chunks = []
    offset = 0
    for index, entry in enumerate(parts, 1):
        name = safe_path(entry["name"])
        prefix = f"swarmer-source-{manifest['version']}-{manifest['git_commit'][:8]}-{manifest['inventory_sha256'][:12]}"
        expected = f"{prefix}.part-{index:05d}-of-{len(parts):05d}.txt"
        if "/" in name or name in seen_parts or name != expected:
            raise ExportError("Part must be a unique sibling file")
        seen_parts.add(name)
        raw, _ = read_regular(root, name, manifest["max_part_bytes"])
        raw.decode("utf-8")
        if not raw or len(raw) != entry["bytes"] or digest(raw) != entry["sha256"]:
            raise ExportError("Part size or SHA-256 mismatch")
        header = part_header(manifest, index, len(parts), offset)
        if (
            entry["header_bytes"] != len(header)
            or entry["payload_offset"] != offset
            or not raw.startswith(header)
        ):
            raise ExportError("Part header or ordering mismatch")
        payload = raw[len(header) :]
        if not payload:
            raise ExportError("Part contains no source payload")
        chunks.append(payload)
        offset += len(payload)
        if offset > MAX_SNAPSHOT_BYTES:
            raise ExportError("Reconstructed snapshot exceeds the byte limit")
    snapshot = b"".join(chunks)
    if (
        len(snapshot) != manifest["snapshot_bytes"]
        or digest(snapshot) != manifest["snapshot_sha256"]
    ):
        raise ExportError("Snapshot size or SHA-256 mismatch")
    metadata = {
        key: manifest[key] for key in ("format", "version", "git_commit", "inventory_sha256")
    }
    prelude = BANNER + canonical(metadata) + b"\n"
    if not snapshot.startswith(prelude):
        raise ExportError("Snapshot metadata mismatch")
    cursor = len(prelude)
    payloads = []
    for entry in files:
        header = b"@@FILE@@\n" + canonical(entry) + b"\n"
        if snapshot[cursor : cursor + len(header)] != header:
            raise ExportError("File framing or manifest mismatch")
        cursor += len(header)
        payload = snapshot[cursor : cursor + entry["bytes"]]
        payload.decode("utf-8")
        if len(payload) != entry["bytes"] or digest(payload) != entry["sha256"]:
            raise ExportError("Source size or SHA-256 mismatch")
        if b"\0" in payload or SECRET_CONTENT.search(payload):
            raise ExportError("Binary or sensitive content in source export")
        cursor += len(payload)
        if snapshot[cursor : cursor + len(END_FILE)] != END_FILE:
            raise ExportError("File boundary mismatch")
        cursor += len(END_FILE)
        payloads.append(payload)
    if snapshot[cursor:] != END_SNAPSHOT or sum(map(len, payloads)) != manifest["source_bytes"]:
        raise ExportError("Snapshot end or original source byte count mismatch")
    return manifest, snapshot, payloads


def extract_export(manifest_path: Path, output: Path) -> dict[str, int]:
    manifest, _, payloads = verify_export(manifest_path)
    target = new_directory(output)
    for entry, raw in zip(manifest["files"], payloads, strict=True):
        write_relative(target, entry["path"], raw, entry["mode"])
    return {"files": len(payloads), "bytes": sum(map(len, payloads))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inventory", "create"):
        command = commands.add_parser(name)
        command.add_argument("--repo", type=Path, default=Path.cwd())
        command.add_argument("--include", action="append", default=[])
        if name == "create":
            command.add_argument("--output-dir", type=Path, required=True)
            command.add_argument("--version", required=True)
            command.add_argument("--max-part-bytes", type=int, default=MAX_PART_BYTES)
    for name in ("verify", "reconstruct", "extract"):
        command = commands.add_parser(name)
        command.add_argument("manifest", type=Path)
        if name == "reconstruct":
            command.add_argument("--output", type=Path, required=True)
        elif name == "extract":
            command.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "inventory":
            inventory, _ = source_inventory(args.repo, args.include)
            result = inventory
        elif args.command == "create":
            result = {
                "manifest": str(
                    create_export(
                        args.repo, args.output_dir, args.version, args.include, args.max_part_bytes
                    )
                )
            }
        elif args.command == "extract":
            result = extract_export(args.manifest, args.output_dir)
        else:
            manifest, snapshot, _ = verify_export(args.manifest)
            if args.command == "reconstruct":
                write_new(args.output, snapshot)
            result = {
                "verified": True,
                "files": len(manifest["files"]),
                "parts": len(manifest["parts"]),
                "snapshot_sha256": manifest["snapshot_sha256"],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ExportError, OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Source export failed: {exc}\n")


if __name__ == "__main__":
    main()
