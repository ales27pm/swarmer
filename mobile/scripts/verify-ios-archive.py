#!/usr/bin/env python3
"""Reject test-only links and missing bundled dependencies in a signed iOS IPA.

Uses macOS xcrun/otool and the Python standard library. Checks every embedded
Mach-O, including weak test-framework links. Required relative links resolve
against each image's and its enclosing executable's declared runpaths; this is
an archive check, not a replacement for installation and device launch testing.
Only Mach-O files are extracted, into a private temporary directory.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

MAGIC = {
    bytes.fromhex(x)
    for x in (
        "feedface",
        "cefaedfe",
        "feedfacf",
        "cffaedfe",
        "cafebabe",
        "bebafeca",
        "cafebabf",
        "bfbafeca",
    )
}
LOADS = {
    "LC_LOAD_DYLIB",
    "LC_LOAD_WEAK_DYLIB",
    "LC_REEXPORT_DYLIB",
    "LC_LOAD_UPWARD_DYLIB",
    "LC_LAZY_LOAD_DYLIB",
}
TEST_LIBRARY = re.compile(
    r"^(?:lib)?(?:Testing|XCTest[^./]*|XCTAutomationSupport|XCUnit|XCUIAutomation)"
    r"(?:\.framework|\.dylib)?$",
    re.IGNORECASE,
)
MAX_BINARY_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


def safe_member(name):
    clean = name.rstrip("/")
    path = PurePosixPath(clean)
    if (
        not clean
        or "\\" in name
        or "\0" in name
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != clean
    ):
        raise ValueError("Unsafe ZIP member path: " + repr(name))
    return path


def is_test_library(name):
    return any(TEST_LIBRARY.fullmatch(part) for part in PurePosixPath(name).parts)


def parse_load_commands(output):
    result = {"dependencies": [], "rpaths": [], "uuids": []}
    command = None
    for line in output.splitlines():
        match = re.match(r"\s*cmd (LC_[A-Z_]+)\s*$", line)
        if match:
            command = match.group(1)
        match = re.match(r"\s*(?:name|path) (.+) \(offset \d+\)\s*$", line)
        if match and command in LOADS:
            entry = {"name": match.group(1), "weak": command == "LC_LOAD_WEAK_DYLIB"}
            if entry not in result["dependencies"]:
                result["dependencies"].append(entry)
        elif match and command == "LC_RPATH" and match.group(1) not in result["rpaths"]:
            result["rpaths"].append(match.group(1))
        match = re.match(r"\s*uuid ([A-Fa-f0-9-]+)\s*$", line)
        if match and command == "LC_UUID":
            result["uuids"].append(match.group(1).upper())
    return result


def read_archive(archive, destination):
    binaries, executables, seen = {}, {}, set()
    total = 0
    with zipfile.ZipFile(archive) as stream:
        for entry in stream.infolist():
            path = safe_member(entry.orig_filename)
            name = path.as_posix()
            if name in seen:
                raise ValueError("Duplicate ZIP member: " + name)
            seen.add(name)
            # SwiftSupport outside Payload is not available to the app loader.
            if (
                len(path.parts) < 3
                or path.parts[0] != "Payload"
                or not path.parts[1].endswith(".app")
            ):
                continue
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("Symlink in IPA payload: " + name)
            if entry.is_dir():
                continue
            if (
                path.name == "Info.plist"
                and path.parent.suffix in {".app", ".appex"}
                and entry.file_size <= 1024 * 1024
            ):
                info = plistlib.loads(stream.read(entry))
                executable = info.get("CFBundleExecutable")
                if (
                    not isinstance(executable, str)
                    or PurePosixPath(executable).name != executable
                ):
                    raise ValueError("Invalid bundle executable in " + name)
                executables[str(path.parent)] = str(path.parent / executable)
            with stream.open(entry) as source:
                if source.read(4) not in MAGIC:
                    continue
            total += entry.file_size
            if entry.file_size > MAX_BINARY_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("IPA exceeds Mach-O extraction size limit")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o600)
            binaries[name] = target
    if (
        not binaries
        or not executables
        or any(x not in binaries for x in executables.values())
    ):
        raise ValueError("IPA has no complete executable app bundle")
    return binaries, executables


def expand_path(value, image, executable):
    for prefix, base in (
        ("@loader_path", posixpath.dirname(image)),
        ("@executable_path", posixpath.dirname(executable)),
    ):
        if value == prefix or value.startswith(prefix + "/"):
            return posixpath.normpath(base + value[len(prefix) :])
    return value


def dependency_issues(images, executables):
    issues = []
    for image, metadata in sorted(images.items()):
        enclosing = [bundle for bundle in executables if image.startswith(bundle + "/")]
        if not enclosing:
            issues.append({"image": image, "kind": "missing_executable_context"})
            continue
        executable = executables[max(enclosing, key=len)]
        runpaths = []
        for owner in (image, executable):
            for value in images[owner]["rpaths"]:
                runpaths.append(expand_path(value, owner, executable))
        if is_test_library(image):
            issues.append({"image": image, "kind": "bundled_test_library"})
        for dependency in metadata["dependencies"]:
            name = dependency["name"]
            if is_test_library(name):
                issues.append(
                    {
                        "image": image,
                        "dependency": name,
                        "kind": "test_only_dependency",
                        "weak": dependency["weak"],
                    }
                )
                continue
            if dependency["weak"] or name.startswith(("/System/Library/", "/usr/lib/")):
                continue
            candidates = []
            if name.startswith("@rpath/"):
                candidates = [posixpath.normpath(p + "/" + name[7:]) for p in runpaths]
            elif name.startswith(("@loader_path/", "@executable_path/")):
                candidates = [expand_path(name, image, executable)]
            if any(candidate in images for candidate in candidates):
                continue
            # Apple Swift runtime overlays may be supplied by the iOS dyld cache.
            if any(
                re.fullmatch(r"/usr/lib/swift/libswift[^/]+\.dylib", p)
                for p in candidates
            ):
                continue
            issues.append(
                {
                    "image": image,
                    "dependency": name,
                    "kind": "unresolved_required_dependency",
                    "candidates": candidates,
                }
            )
    return issues


def audit(archive):
    with tempfile.TemporaryDirectory(prefix="verify-ios-archive-") as temporary:
        destination = Path(temporary)
        destination.chmod(0o700)
        binaries, executables = read_archive(archive, destination)
        images = {}
        for name, binary in sorted(binaries.items()):
            output = subprocess.run(
                ["xcrun", "otool", "-l", str(binary)],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout
            metadata = parse_load_commands(output)
            if not metadata["uuids"]:
                raise ValueError(
                    "Mach-O has no UUID or unreadable load commands: " + name
                )
            images[name] = metadata
        issues = dependency_issues(images, executables)
        return {
            "archive": str(Path(archive).resolve()),
            "result": "failed" if issues else "passed",
            "mach_o_count": len(images),
            "issues": issues,
            "images": images,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ipa", type=Path, help="Explicit signed .ipa path")
    parser.add_argument(
        "--json", action="store_true", help="Print full machine-readable audit"
    )
    args = parser.parse_args()
    try:
        if not args.ipa.is_file() or args.ipa.suffix.lower() != ".ipa":
            raise ValueError("Expected an existing .ipa file")
        result = audit(args.ipa)
    except (
        OSError,
        ValueError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as error:
        print(
            "iOS archive verification could not complete: " + str(error),
            file=sys.stderr,
        )
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            "iOS archive verification: {} ({} Mach-O images)".format(
                result["result"], result["mach_o_count"]
            )
        )
        for issue in result["issues"]:
            print(
                "{}: {} -> {}".format(
                    issue["kind"], issue["image"], issue.get("dependency", "")
                )
            )
    return 1 if result["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
