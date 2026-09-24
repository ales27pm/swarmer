#!/usr/bin/env python3
"""Reject broken native dependencies and missing monGARS code in a signed IPA.

Uses macOS xcrun/otool and the Python standard library. Checks every embedded
Mach-O, including weak test-framework links. Required relative links resolve
against each image's and its enclosing executable's declared runpaths; this is
an archive check, not a replacement for installation and device launch testing.
For monGARS, the Pods Hermes compiler also inspects the bundled bytecode's string
table without executing it. Required route keys and native API literals detect
an Expo-only bundle, independently of minifiable function names or bundle size.
Only Mach-O files and the target JS bundle are extracted, into a private directory.
"""

from __future__ import annotations

import argparse
import hashlib
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
MAX_JS_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_HERMES_TABLE_BYTES = 16 * 1024 * 1024
HERMES_MAGIC = bytes.fromhex("c61fbc03c103191f")
APPLICATION_ID = "org.27pm.mongars"


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


def application_contract():
    contract = json.loads(
        Path(__file__).with_name("ios-bundle-contract.json").read_text()
    )
    if (
        not isinstance(contract, dict)
        or set(contract) != {"route_keys", "native_strings"}
        or any(
            not isinstance(values, list)
            or not values
            or any(
                not isinstance(value, str) or not value or not value.isascii()
                for value in values
            )
            or len(set(values)) != len(values)
            for values in contract.values()
        )
    ):
        raise ValueError("Invalid iOS bundle contract")
    return contract


def resolve_hermesc(explicit=None):
    mobile = Path(__file__).resolve().parents[1]
    candidates = (
        [Path(explicit)]
        if explicit
        else [
            mobile / "ios/Pods/hermes-engine/destroot/bin/hermesc",
            mobile / "node_modules/hermes-compiler/hermesc/osx-bin/hermesc",
            mobile / "node_modules/react-native/sdks/hermesc/osx-bin/hermesc",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError(
        "Hermes compiler is unavailable; supply the build's compiler with --hermesc"
    )


def parse_hermes_string_table(output):
    header, separator, remainder = output.partition("\nGlobal String Table:\n")
    version = re.search(r"^  Bytecode version number: (\d+)$", header, re.MULTILINE)
    table, end, _ = remainder.partition("\n\n")
    if not separator or not version or not end:
        raise ValueError("Hermes output has no complete global string table")
    strings = set()
    for line in table.splitlines():
        match = re.fullmatch(
            r"[is]\d+\[ASCII, \d+\.\.\d+\](?: #[0-9A-Fa-f]+)?: (.*)", line
        )
        if match:
            strings.add(match.group(1))
    if not strings:
        raise ValueError("Hermes global string table is empty or unreadable")
    return int(version.group(1)), strings


def application_bundle_audit(archive, destination, executables, hermesc=None):
    """Inspect only each top-level monGARS app's actual main.jsbundle.

    The ZIP paths and payload symlinks have already been checked by read_archive.
    Resource decoys and string fragments are not evidence of linked application
    code. This is a necessary packaging check, not proof that bootstrap executes.
    """
    issues, receipts = [], []
    with zipfile.ZipFile(archive) as stream:
        for bundle in sorted(executables):
            if len(PurePosixPath(bundle).parts) != 2:
                continue
            info = plistlib.loads(stream.read(bundle + "/Info.plist"))
            if info.get("CFBundleIdentifier") != APPLICATION_ID:
                continue
            image = bundle + "/main.jsbundle"
            try:
                entry = stream.getinfo(image)
            except KeyError:
                issues.append({"image": image, "kind": "missing_application_bundle"})
                continue
            if entry.file_size > MAX_JS_BUNDLE_BYTES:
                raise ValueError(
                    "Application bundle exceeds inspection size limit: " + image
                )
            content = stream.read(entry)
            receipt = {
                "image": image,
                "bundle_identifier": APPLICATION_ID,
                "bundle_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            receipts.append(receipt)
            if not content.startswith(HERMES_MAGIC):
                issues.append(
                    {"image": image, "kind": "unsupported_application_bundle"}
                )
                continue
            contract = application_contract()
            target = destination.joinpath(*PurePosixPath(image).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o600)
            # File-backed output avoids holding the entire disassembly in memory.
            # The bounded prefix must contain a complete table, otherwise fail closed.
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
                subprocess.run(
                    [
                        str(resolve_hermesc(hermesc)),
                        "-dump-bytecode",
                        "-b",
                        str(target),
                    ],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    check=True,
                )
                output.seek(0)
                version, strings = parse_hermes_string_table(
                    output.read(MAX_HERMES_TABLE_BYTES)
                )
            receipt["bytecode_version"] = version
            receipt["required_routes"] = contract["route_keys"]
            receipt["required_native_symbols"] = contract["native_strings"]
            missing_routes = [
                key for key in contract["route_keys"] if key not in strings
            ]
            missing_native = [
                key for key in contract["native_strings"] if key not in strings
            ]
            if missing_routes or missing_native:
                issues.append(
                    {
                        "image": image,
                        "kind": "missing_application_bundle_markers",
                        "missing_routes": missing_routes,
                        "missing_native_symbols": missing_native,
                    }
                )
    return issues, receipts


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


def audit(archive, hermesc=None):
    with tempfile.TemporaryDirectory(prefix="verify-ios-archive-") as temporary:
        destination = Path(temporary)
        destination.chmod(0o700)
        binaries, executables = read_archive(archive, destination)
        application_issues, application_bundles = application_bundle_audit(
            archive, destination, executables, hermesc
        )
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
        issues = dependency_issues(images, executables) + application_issues
        return {
            "archive": str(Path(archive).resolve()),
            "result": "failed" if issues else "passed",
            "mach_o_count": len(images),
            "issues": issues,
            "images": images,
            "application_bundles": application_bundles,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ipa", type=Path, help="Explicit signed .ipa path")
    parser.add_argument(
        "--hermesc",
        type=Path,
        help="Hermes compiler used by this build (auto-detects Pods by default)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print full machine-readable audit"
    )
    args = parser.parse_args()
    try:
        if not args.ipa.is_file() or args.ipa.suffix.lower() != ".ipa":
            raise ValueError("Expected an existing .ipa file")
        result = audit(args.ipa, args.hermesc)
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
            for label in ("missing_routes", "missing_native_symbols"):
                if issue.get(label):
                    print("  " + label + ": " + ", ".join(issue[label]))
    return 1 if result["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
