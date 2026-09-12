"""Archive gate regressions; run with system Python, without building an app."""

import importlib.util
import plistlib
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "verify_ios_archive", Path(__file__).with_name("verify-ios-archive.py")
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
APP = "Payload/App.app/App"
FRAMEWORK = "Payload/App.app/Frameworks/Contacts.framework/Contacts"
UUID = "0FA206C4-5A55-3F0C-822A-15B62E3B37BA"


def metadata(dependencies=(), rpaths=()):
    return {
        "dependencies": [{"name": name, "weak": weak} for name, weak in dependencies],
        "rpaths": list(rpaths),
        "uuids": [UUID],
    }


def load_output(command=None, dependency=None):
    text = "Load command 0\n      cmd LC_UUID\n cmdsize 24\n    uuid " + UUID + "\n"
    if command:
        text += (
            "Load command 1\n      cmd "
            + command
            + "\n cmdsize 64\n     name "
            + dependency
            + " (offset 24)\n"
        )
    return text


class DependencyTests(unittest.TestCase):
    def issues(self, name, *, weak=False, extra=None):
        images = {
            APP: metadata(rpaths=["@executable_path/Frameworks"]),
            FRAMEWORK: metadata([(name, weak)]),
        }
        images.update(extra or {})
        return CHECK.dependency_issues(images, {"Payload/App.app": APP})

    def test_load_commands_keep_weakness_and_uuid_and_ignore_install_id(self):
        parsed = CHECK.parse_load_commands(
            load_output("LC_LOAD_WEAK_DYLIB", "@rpath/Testing.framework/Testing")
        )
        self.assertEqual(
            parsed["dependencies"],
            [{"name": "@rpath/Testing.framework/Testing", "weak": True}],
        )
        self.assertEqual(parsed["uuids"], [UUID])
        self.assertEqual(
            CHECK.parse_load_commands(
                load_output("LC_ID_DYLIB", "@rpath/Own.framework/Own")
            )["dependencies"],
            [],
        )

    def test_required_and_weak_test_libraries_are_rejected(self):
        names = [
            "@rpath/Testing.framework/Testing",
            "@rpath/XCTest.framework/XCTest",
            "/System/Library/Frameworks/XCTestCore.framework/XCTestCore",
            "@rpath/libXCTestSwiftSupport.dylib",
        ]
        for name in names:
            for weak in (False, True):
                with self.subTest(name=name, weak=weak):
                    self.assertEqual(
                        self.issues(name, weak=weak)[0]["kind"], "test_only_dependency"
                    )

    def test_missing_relative_required_dependencies_fail(self):
        for name in (
            "@rpath/Missing.framework/Missing",
            "@loader_path/Missing.dylib",
            "@executable_path/Frameworks/Missing.dylib",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.issues(name)[0]["kind"], "unresolved_required_dependency"
                )

    def test_bundled_relative_dependencies_resolve(self):
        other = "Payload/App.app/Frameworks/Other.framework/Other"
        for name in (
            "@rpath/Other.framework/Other",
            "@loader_path/../Other.framework/Other",
            "@executable_path/Frameworks/Other.framework/Other",
        ):
            with self.subTest(name=name):
                self.assertEqual(self.issues(name, extra={other: metadata()}), [])

    def test_system_and_optional_non_test_links_are_accepted(self):
        self.assertEqual(
            self.issues("/System/Library/Frameworks/UIKit.framework/UIKit"), []
        )
        self.assertEqual(self.issues("/usr/lib/libSystem.B.dylib"), [])
        self.assertEqual(
            self.issues("@rpath/Optional.framework/Optional", weak=True), []
        )
        self.assertEqual(
            self.issues("/Library/Developer/Unshipped.dylib")[0]["kind"],
            "unresolved_required_dependency",
        )

    def test_embedded_test_library_fails_even_without_incoming_link(self):
        testing = "Payload/App.app/Frameworks/Testing.framework/Testing"
        images = {APP: metadata(), testing: metadata()}
        self.assertEqual(
            CHECK.dependency_issues(images, {"Payload/App.app": APP})[0]["kind"],
            "bundled_test_library",
        )


class ArchiveTests(unittest.TestCase):
    def create_archive(self, path, additions=()):
        with zipfile.ZipFile(path, "w") as stream:
            stream.writestr(
                "Payload/App.app/Info.plist",
                plistlib.dumps({"CFBundleExecutable": "App"}),
            )
            stream.writestr(APP, bytes.fromhex("cffaedfe") + b"fake-Mach-O")
            stream.writestr(FRAMEWORK, bytes.fromhex("cffaedfe") + b"fake-Mach-O")
            stream.writestr(
                "Payload/App.app/assets/example.txt", b"resource stays in ZIP"
            )
            for name, value in additions:
                stream.writestr(name, value)

    def test_extracts_only_macho_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "app.ipa"
            self.create_archive(archive)
            binaries, executables = CHECK.read_archive(archive, root / "extracted")
            self.assertEqual(set(binaries), {APP, FRAMEWORK})
            self.assertEqual(executables, {"Payload/App.app": APP})
            self.assertFalse((root / "extracted/Payload/App.app/Info.plist").exists())
            self.assertFalse(
                (root / "extracted/Payload/App.app/assets/example.txt").exists()
            )

    def test_rejects_unsafe_zip_paths_before_extraction(self):
        for name in (
            "/absolute/file",
            "Payload/../escape",
            "Payload/./file",
            "Payload\\escape",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / "bad.ipa"
                self.create_archive(archive, [(name, b"unsafe")])
                with self.assertRaisesRegex(ValueError, "Unsafe ZIP member"):
                    CHECK.read_archive(archive, root / "extracted")

    def test_rejects_payload_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.ipa"
            link = zipfile.ZipInfo("Payload/App.app/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            self.create_archive(archive, [(link, b"../../outside")])
            with self.assertRaisesRegex(ValueError, "Symlink"):
                CHECK.read_archive(archive, root / "extracted")

    def test_audits_frameworks_even_when_main_binary_has_no_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "broken.ipa"
            self.create_archive(archive)

            def otool(argv, **kwargs):
                output = load_output()
                if str(argv[-1]).endswith("Contacts.framework/Contacts"):
                    output = load_output(
                        "LC_LOAD_DYLIB", "@rpath/Testing.framework/Testing"
                    )
                return mock.Mock(stdout=output)

            with mock.patch.object(CHECK.subprocess, "run", side_effect=otool):
                result = CHECK.audit(archive)
            self.assertEqual(result["mach_o_count"], 2)
            self.assertEqual(result["result"], "failed")
            self.assertEqual(result["issues"][0]["image"], FRAMEWORK)
            self.assertEqual(result["issues"][0]["kind"], "test_only_dependency")


if __name__ == "__main__":
    unittest.main()
