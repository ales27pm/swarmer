"""Archive gate regressions; run with system Python, without building an app."""

import importlib.util
import json
import plistlib
import stat
import subprocess
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
    def create_archive(self, path, additions=(), bundle_identifier=None):
        with zipfile.ZipFile(path, "w") as stream:
            info = {"CFBundleExecutable": "App"}
            if bundle_identifier:
                info["CFBundleIdentifier"] = bundle_identifier
            stream.writestr(
                "Payload/App.app/Info.plist",
                plistlib.dumps(info),
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


class ApplicationBundleTests(unittest.TestCase):
    create_archive = ArchiveTests.create_archive
    # Required literals have runtime meaning; none is a minifiable function name.
    ROUTES = (
        "./(main)/_layout.tsx",
        "./(main)/agents.tsx",
        "./(main)/approvals.tsx",
        "./(main)/catalog.tsx",
        "./(main)/index.tsx",
        "./(main)/memory.tsx",
        "./(main)/settings.tsx",
        "./(main)/swarm.tsx",
        "./(main)/tasks.tsx",
        "./_layout.tsx",
        "./goal/[id].tsx",
        "./local-model.tsx",
        "./task/[id].tsx",
    )
    NATIVE = (
        "SwarmerLocalInference",
        "automationRequest",
        "startAutomationServer",
        "completeAutomationRequest",
    )
    BUNDLE = "Payload/App.app/main.jsbundle"
    BYTECODE = bytes.fromhex("c61fbc03c103191f") + b"fake-Hermes-bytecode"

    @staticmethod
    def dump(strings):
        return (
            "Bytecode File Information:\n  Bytecode version number: 96\n\n"
            "Global String Table:\n"
            + "\n".join(
                f"i{i}[ASCII, 0..{len(value) - 1}] #12345678: {value}"
                for i, value in enumerate(strings)
            )
            + "\n\nFunction<ExpoRoot>():\n"
        )

    def audit_app(self, root, strings, *, bundle=True, additions=(), bytecode=None):
        archive = root / "app.ipa"
        entries = list(additions)
        if bundle:
            entries.append(
                (self.BUNDLE, self.BYTECODE if bytecode is None else bytecode)
            )
        self.create_archive(archive, entries, bundle_identifier="org.27pm.mongars")

        def run(argv, **kwargs):
            if argv[0] == "xcrun":
                return mock.Mock(stdout=load_output())
            kwargs["stdout"].write(self.dump(strings))
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(
                CHECK, "resolve_hermesc", return_value=Path("/fake/hermesc")
            ),
            mock.patch.object(CHECK.subprocess, "run", side_effect=run) as calls,
        ):
            result = CHECK.audit(archive)
        return result, calls

    def test_expo_only_bundle_is_rejected_despite_valid_macho(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, _ = self.audit_app(Path(temporary), ["ExpoRoot", "React"])
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["mach_o_count"], 2)
        issue = result["issues"][0]
        self.assertEqual(issue["kind"], "missing_application_bundle_markers")
        self.assertEqual(set(issue["missing_routes"]), set(self.ROUTES))
        self.assertEqual(set(issue["missing_native_symbols"]), set(self.NATIVE))

    def test_complete_bundle_passes_without_named_functions_or_size_threshold(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, calls = self.audit_app(Path(temporary), self.ROUTES + self.NATIVE)
        self.assertEqual(result["result"], "passed")
        receipt = result["application_bundles"][0]
        self.assertEqual(receipt["bytecode_version"], 96)
        self.assertEqual(receipt["bundle_bytes"], len(self.BYTECODE))
        self.assertEqual(len(receipt["sha256"]), 64)
        compiler_calls = [
            call for call in calls.call_args_list if call.args[0][0] != "xcrun"
        ]
        self.assertEqual(len(compiler_calls), 1)
        self.assertEqual(compiler_calls[0].args[0][1:3], ["-dump-bytecode", "-b"])

    def test_missing_single_route_or_bridge_symbol_fails(self):
        markers = self.ROUTES + self.NATIVE
        for omitted in (
            "./goal/[id].tsx",
            "SwarmerLocalInference",
            "automationRequest",
        ):
            with (
                self.subTest(omitted=omitted),
                tempfile.TemporaryDirectory() as temporary,
            ):
                result, _ = self.audit_app(
                    Path(temporary), [x for x in markers if x != omitted]
                )
                self.assertEqual(result["result"], "failed")
                issue = result["issues"][0]
                self.assertEqual(
                    issue["missing_routes"] + issue["missing_native_symbols"], [omitted]
                )

    def test_route_lookalikes_do_not_count_as_exact_string_entries(self):
        lookalikes = ["prefix" + x for x in self.ROUTES] + [
            x + ".bak" for x in self.ROUTES
        ]
        with tempfile.TemporaryDirectory() as temporary:
            result, _ = self.audit_app(Path(temporary), lookalikes + list(self.NATIVE))
        self.assertEqual(result["issues"][0]["missing_routes"], list(self.ROUTES))

    def test_assets_other_bundles_and_raw_bytes_cannot_supply_missing_routes(self):
        decoy = "\n".join(self.ROUTES + self.NATIVE).encode()
        with tempfile.TemporaryDirectory() as temporary:
            result, _ = self.audit_app(
                Path(temporary),
                ["ExpoRoot"],
                bytecode=self.BYTECODE + decoy,
                additions=[
                    ("Payload/App.app/assets/decoy.js", decoy),
                    ("Payload/App.app/other.jsbundle", decoy),
                ],
            )
        self.assertEqual(result["result"], "failed")

    def test_missing_bundle_fails_without_invoking_compiler(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, calls = self.audit_app(Path(temporary), [], bundle=False)
        self.assertEqual(result["issues"][0]["kind"], "missing_application_bundle")
        self.assertTrue(
            all(call.args[0][0] == "xcrun" for call in calls.call_args_list)
        )

    def test_non_hermes_bundle_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, _ = self.audit_app(
                Path(temporary), self.ROUTES + self.NATIVE, bytecode=b"// JS"
            )
        self.assertEqual(result["issues"][0]["kind"], "unsupported_application_bundle")

    def test_only_global_string_table_entries_are_used(self):
        decoy = self.dump(["ExpoRoot"]) + self.dump(self.ROUTES + self.NATIVE)
        version, strings = CHECK.parse_hermes_string_table(decoy)
        self.assertEqual(version, 96)
        self.assertEqual(strings, {"ExpoRoot"})

    def test_malformed_compiler_output_is_rejected(self):
        for value in ("", "Global String Table:\n", self.dump([])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CHECK.parse_hermes_string_table(value)

    def test_explicit_compiler_path_is_used_and_missing_path_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            compiler = Path(temporary) / "hermesc"
            compiler.write_text("fixture")
            self.assertEqual(CHECK.resolve_hermesc(compiler), compiler.resolve())
            with self.assertRaisesRegex(ValueError, "Hermes compiler"):
                CHECK.resolve_hermesc(Path(temporary) / "absent")

    def test_compiler_auto_detection_can_fall_back_to_hermes_compiler_package(self):
        with mock.patch.object(CHECK.Path, "is_file", side_effect=[False, True]):
            compiler = CHECK.resolve_hermesc()
        self.assertTrue(
            str(compiler).endswith("hermes-compiler/hermesc/osx-bin/hermesc")
        )

    def test_compiler_auto_detection_can_fall_back_to_legacy_react_native(self):
        with mock.patch.object(CHECK.Path, "is_file", side_effect=[False, False, True]):
            compiler = CHECK.resolve_hermesc()
        self.assertTrue(
            str(compiler).endswith("react-native/sdks/hermesc/osx-bin/hermesc")
        )

    def test_shared_contract_matches_required_routes_and_native_api(self):
        contract = CHECK.application_contract()
        self.assertEqual(contract["route_keys"], list(self.ROUTES))
        self.assertEqual(contract["native_strings"], list(self.NATIVE))

    def test_malformed_shared_contract_cannot_disable_application_check(self):
        for contract in (
            {},
            {"route_keys": [], "native_strings": list(self.NATIVE)},
            {"route_keys": ["./_layout.tsx"] * 2, "native_strings": list(self.NATIVE)},
            {"route_keys": [None], "native_strings": list(self.NATIVE)},
            {"route_keys": list(self.ROUTES), "native_strings": "not a list"},
        ):
            with (
                self.subTest(contract=contract),
                mock.patch.object(
                    CHECK.Path, "read_text", return_value=json.dumps(contract)
                ),
                self.assertRaisesRegex(ValueError, "Invalid iOS bundle contract"),
            ):
                CHECK.application_contract()

    def test_compiler_failure_propagates_instead_of_skipping_app_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "app.ipa"
            self.create_archive(
                archive, [(self.BUNDLE, self.BYTECODE)], "org.27pm.mongars"
            )
            with (
                mock.patch.object(
                    CHECK, "resolve_hermesc", return_value=Path("/fake/hermesc")
                ),
                mock.patch.object(
                    CHECK.subprocess,
                    "run",
                    side_effect=subprocess.CalledProcessError(1, "hermesc"),
                ),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                CHECK.application_bundle_audit(
                    archive, Path(temporary), {"Payload/App.app": APP}
                )


if __name__ == "__main__":
    unittest.main()
