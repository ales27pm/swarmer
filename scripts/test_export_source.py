"""Source export regression tests; all repositories and exports live in temporary directories."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).with_name("export_source.py")
SPEC = importlib.util.spec_from_file_location("source_export_under_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)


class SourceExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="source-export-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.put("README.md", b"A harmless fixture.\n")
        self.commit()

    def git(self, *args: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args], capture_output=True, check=True
        )
        return result.stdout

    def put(self, name: str, data: bytes) -> Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def commit(self) -> None:
        self.git("add", "-A")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "fixture",
        )

    def create(
        self, name: str = "export", include: list[str] | None = None, limit: int = 1024
    ) -> Path:
        return Path(
            export.create_export(self.repo, self.root / name, "0.14.2-test", include or [], limit)
        )

    def test_deterministic_parts_reconstruction_and_executable_modes(self) -> None:
        payload = ("é中🙂\r\n" * 1500).encode() + b"@@END_FILE@@\n@@END_SNAPSHOT@@\n"
        self.put("src/nested/example.py", payload).chmod(0o755)
        self.put("src/empty.txt", b"")
        self.commit()
        first = self.create()
        # An unrelated, untracked file must not alter a repeated export.
        self.put("unrelated-local-notes.txt", b"not selected\n")
        second = self.create("export-again")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        manifest, reconstructed, sources = export.verify_export(first)
        self.assertGreater(len(manifest["parts"]), 2)
        self.assertEqual(hashlib.sha256(reconstructed).hexdigest(), manifest["snapshot_sha256"])
        for part in manifest["parts"]:
            raw = (first.parent / part["name"]).read_bytes()
            self.assertTrue(0 < len(raw) <= 1024)
            self.assertTrue(raw.decode().startswith("SWARMER / monGARS - TEXT SOURCES"))
            self.assertEqual(raw, (second.parent / part["name"]).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), part["sha256"])
        target = self.root / "restored"
        export.extract_export(first, target)
        for entry, original in zip(manifest["files"], sources, strict=True):
            restored = target / entry["path"]
            self.assertEqual(restored.read_bytes(), original)
            self.assertEqual(restored.stat().st_mode & 0o777, entry["mode"])
        self.assertEqual((target / "src/nested/example.py").read_bytes(), payload)
        self.assertFalse((target / "unrelated-local-notes.txt").exists())

    def test_five_megabyte_decimal_boundary_is_not_a_character_limit(self) -> None:
        payload = "🙂".encode() * 1_250_001
        self.put("large.txt", payload)
        self.commit()
        path = self.create(limit=5_000_000)
        manifest, _, sources = export.verify_export(path)
        self.assertEqual(len(manifest["parts"]), 2)
        self.assertTrue(all(0 < entry["bytes"] <= 5_000_000 for entry in manifest["parts"]))
        self.assertIn(payload, sources)

    def test_only_exact_explicit_untracked_files_are_included(self) -> None:
        self.put("intended.py", b"print('intended')\n")
        self.put("other.py", b"print('unrelated')\n")
        manifest, _ = export.source_inventory(self.repo, ["intended.py"])
        self.assertEqual(
            [entry["path"] for entry in manifest["files"]], ["README.md", "intended.py"]
        )
        self.assertEqual(manifest["files"][1]["origin"], "explicit")
        for invalid in (".", "*.py", "src", "../outside.py", "/tmp/outside.py", "a\\b.py"):
            with self.subTest(invalid=invalid), self.assertRaises((export.ExportError, OSError)):
                export.source_inventory(self.repo, [invalid])

    def test_exclusions_are_visible_and_missing_files_do_not_disappear_silently(self) -> None:
        candidates = {
            ".env": b"local configuration",
            "configs/credentials.json": b"{}",
            "configs/production.yaml": b"private: value",
            "vendor/dependency.py": b"dependency",
            "build/output.js": b"generated",
            "data/runtime.json": b"{}",
            "asset.png": b"text does not make an image a source file",
            "invalid.txt": b"\xff\xfe",
            "binary.txt": b"nul\x00byte",
            "logs/session.txt": b"private runtime log",
            "swarmer-complete-source.txt": b"old export",
            "swarmer-source-old.README.txt": b"generated export instructions",
            "deleted.py": b"remove after commit",
        }
        for name, data in candidates.items():
            self.put(name, data)
        self.put("server/app/data/activity_catalog.json", b'{"public": true}')
        self.put("server/app/services/auth_service.py", b"# Authentication implementation\n")
        self.put("workers/project-worker/Modelfile", b"FROM example\n")
        self.commit()
        (self.repo / "deleted.py").unlink()
        manifest, _ = export.source_inventory(self.repo, [])
        exclusions = {entry["path"]: entry["reason"] for entry in manifest["excluded"]}
        self.assertEqual(set(exclusions), set(candidates))
        self.assertEqual(exclusions["deleted.py"], "missing_from_working_tree")
        self.assertEqual(exclusions["configs/production.yaml"], "sensitive_path")
        self.assertNotIn("server/app/services/auth_service.py", exclusions)
        self.assertNotIn("workers/project-worker/Modelfile", exclusions)
        self.assertIn(
            "server/app/data/activity_catalog.json", [entry["path"] for entry in manifest["files"]]
        )
        for secret in (".env", "configs/credentials.json", "deleted.py"):
            with self.subTest(secret=secret), self.assertRaises(export.ExportError):
                export.source_inventory(self.repo, [secret])

    def test_sensitive_content_is_excluded_without_exposing_it(self) -> None:
        sensitive = b"-----BEGIN " + b"PRIVATE KEY-----\nnot-exportable\n"
        self.put("accidental.txt", sensitive)
        self.put("access.txt", b"hf_" + b"A" * 30)
        self.commit()
        manifest, _ = export.source_inventory(self.repo, [])
        self.assertEqual({entry["reason"] for entry in manifest["excluded"]}, {"sensitive_content"})
        self.assertNotIn("not-exportable", json.dumps(manifest))
        with self.assertRaises(export.ExportError):
            export.source_inventory(self.repo, ["accidental.txt"])

    def test_ignored_explicit_file_is_rejected(self) -> None:
        self.put(".gitignore", b"local.py\n")
        self.commit()
        self.put("local.py", b"private notes")
        with self.assertRaisesRegex(export.ExportError, "ignored by Git"):
            export.source_inventory(self.repo, ["local.py"])

    def test_symlink_files_and_parents_are_never_followed(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"outside secret")
        (self.repo / "alias.txt").symlink_to(outside / "secret.txt")
        self.commit()
        manifest, _ = export.source_inventory(self.repo, [])
        self.assertEqual(
            manifest["excluded"], [{"path": "alias.txt", "reason": "symlink_or_submodule"}]
        )
        (self.repo / "alias-dir").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(export.ExportError):
            export.source_inventory(self.repo, ["alias-dir/secret.txt"])
        with self.assertRaises(OSError):
            export.write_relative(self.repo, "alias-dir/new.txt", b"never", 0o644)
        self.assertFalse((outside / "new.txt").exists())

    def test_tracked_parent_replaced_by_symlink_is_reported(self) -> None:
        original = self.put("nested/selected.txt", b"original")
        self.commit()
        original.unlink()
        original.parent.rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "selected.txt").write_bytes(b"must not read outside")
        original.parent.symlink_to(outside, target_is_directory=True)
        manifest, _ = export.source_inventory(self.repo, [])
        self.assertEqual(
            manifest["excluded"],
            [{"path": "nested/selected.txt", "reason": "symlink_or_non_directory_parent"}],
        )

    def test_version_and_size_validation_precede_output_creation(self) -> None:
        for value in ("../release", "v1/extra", "v1\\extra", "..", "a\nb", "", "a" * 65):
            with self.subTest(version=value), self.assertRaises(export.ExportError):
                export.create_export(self.repo, self.root / "invalid", value, [], 1024)
        for limit in (0, 1023, 5_000_001):
            with self.subTest(limit=limit), self.assertRaises(export.ExportError):
                export.create_export(self.repo, self.root / "invalid", "valid", [], limit)
        self.assertFalse((self.root / "invalid").exists())

    def test_existing_outputs_and_destination_symlinks_are_not_replaced(self) -> None:
        manifest = self.create()
        before = {path.name: path.read_bytes() for path in manifest.parent.iterdir()}
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in manifest.parent.iterdir()}
        )
        existing = self.root / "existing"
        existing.mkdir()
        (existing / "keep.txt").write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            export.extract_export(manifest, existing)
        alias = self.root / "alias"
        alias.symlink_to(existing, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            export.extract_export(manifest, alias)
        self.assertEqual((existing / "keep.txt").read_bytes(), b"keep")

    def test_missing_tampered_and_symlink_parts_fail_verification(self) -> None:
        path = self.create()
        manifest = json.loads(path.read_bytes())
        part = path.parent / manifest["parts"][0]["name"]
        original = part.read_bytes()
        part.write_bytes(original[:-1] + b"X")
        with self.assertRaisesRegex(export.ExportError, "SHA-256"):
            export.verify_export(path)
        part.unlink()
        with self.assertRaises(FileNotFoundError):
            export.verify_export(path)
        outside = self.root / "outside.txt"
        outside.write_bytes(original)
        part.symlink_to(outside)
        with self.assertRaises(OSError):
            export.verify_export(path)

    def test_output_root_replaced_by_symlink_is_rejected_before_writing(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        real_write = export.write_relative
        replaced = False

        def replace_before_write(root: Path, name: str, data: bytes, mode: int) -> None:
            nonlocal replaced
            if not replaced:
                replaced = True
                root.rename(self.root / "reserved-output")
                root.symlink_to(outside, target_is_directory=True)
            real_write(root, name, data, mode)

        with (
            patch.object(export, "write_relative", replace_before_write),
            self.assertRaises(OSError),
        ):
            self.create()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list((self.root / "reserved-output").iterdir()), [])

    def test_manifest_path_traversal_is_rejected_before_reading_or_extraction(self) -> None:
        path = self.create()
        manifest = json.loads(path.read_bytes())
        manifest["parts"][0]["name"] = "../outside.txt"
        path.write_text(json.dumps(manifest))
        with self.assertRaises(export.ExportError):
            export.verify_export(path)
        self.assertFalse((self.root / "restored").exists())

    def test_read_detects_concurrent_source_change(self) -> None:
        real_read = os.read
        changed = False

        def change_once(descriptor: int, count: int) -> bytes:
            nonlocal changed
            result = real_read(descriptor, count)
            if not changed:
                changed = True
                (self.repo / "README.md").write_bytes(b"changed while reading")
            return result

        with (
            patch.object(export.os, "read", change_once),
            self.assertRaisesRegex(export.ExportError, "changed"),
        ):
            export.read_regular(self.repo, "README.md", export.MAX_FILE_BYTES)

    def test_cli_verify_and_reconstruct_use_the_same_checked_snapshot(self) -> None:
        path = self.create()
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "verify", str(path)], capture_output=True, check=True
        )
        self.assertTrue(json.loads(result.stdout)["verified"])
        output = self.root / "reconstructed.txt"
        subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "reconstruct", str(path), "--output", str(output)],
            capture_output=True,
            check=True,
        )
        _, expected, _ = export.verify_export(path)
        self.assertEqual(output.read_bytes(), expected)
        refused = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "reconstruct", str(path), "--output", str(output)],
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(output.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()
