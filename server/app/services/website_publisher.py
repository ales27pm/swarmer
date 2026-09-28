"""Immutable local static releases; the caller owns revision approval and single use."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.services.website_builder import BuildFile, WebsiteBuild, canonical_json


class PublicationError(ValueError):
    """Stable error reason suitable for a workflow receipt."""


class StaticDirectoryPublisher:
    def __init__(
        self,
        root: Path | None,
        public_base_url: str | None,
        *,
        attachment_headers_configured: bool = False,
    ):
        self.root = root
        self.public_base_url = public_base_url
        self.attachment_headers_configured = attachment_headers_configured

    def _validated(
        self, build: WebsiteBuild, *, expected_digest: str, release_id: str
    ) -> tuple[WebsiteBuild, Path, str]:
        if self.root is None or not self.public_base_url:
            raise PublicationError("website_hosting_not_configured")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", release_id):
            raise PublicationError("invalid_release_id")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise PublicationError("invalid_expected_digest")
        try:
            parsed = urlsplit(self.public_base_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or "?" in self.public_base_url
                or "#" in self.public_base_url
                or "\\" in self.public_base_url
                or any(ord(c) < 32 for c in self.public_base_url)
            ):
                raise ValueError
        except ValueError as exc:
            raise PublicationError("invalid_hosting_public_url") from exc
        # Snapshot the reviewed object before touching the filesystem. Never read caller-mutable
        # fields while writing, and verify the exact digest including reports and binary assets.
        snapshot = WebsiteBuild.model_validate_json(build.model_dump_json())
        snapshot.verify()
        if snapshot.digest != expected_digest:
            raise PublicationError("publication_digest_mismatch")
        if (
            any(f.media_type == "application/octet-stream" for f in snapshot.files)
            and not self.attachment_headers_configured
        ):
            raise PublicationError("hosting_attachment_headers_not_configured")
        configured_root = self.root.absolute()
        # Inspect before lexical normalization: a symlink in `link/..` must not disappear.
        if not configured_root.is_dir() or any(
            path.is_symlink() for path in (configured_root, *configured_root.parents)
        ):
            raise PublicationError("hosting_root_missing_or_symlinked")
        root = Path(os.path.abspath(configured_root))
        if root.resolve() != root:
            raise PublicationError("hosting_root_missing_or_symlinked")
        release = release_id + "-" + expected_digest[:16]
        return snapshot, root, release

    def preflight(self, build: WebsiteBuild, *, expected_digest: str, release_id: str) -> None:
        """Validate the configured destination before asking for final publication consent."""
        self._validated(build, expected_digest=expected_digest, release_id=release_id)

    def publish(
        self, build: WebsiteBuild, *, expected_digest: str, release_id: str
    ) -> dict[str, Any]:
        snapshot, root, release = self._validated(
            build, expected_digest=expected_digest, release_id=release_id
        )
        stage = ".pending-" + uuid.uuid4().hex
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock_fd = -1
        stage_created = False
        try:
            lock_fd = os.open(
                ".publish.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root_fd
            )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PublicationError("publication_busy") from exc
            try:
                os.stat(release, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PublicationError("release_already_exists")
            os.mkdir(stage, 0o700, dir_fd=root_fd)
            stage_created = True
            stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                for file in self._public_files(snapshot):
                    self._write_file(stage_fd, file.path, file.bytes())
                self._write_file(
                    stage_fd, "build-manifest.json", canonical_json(self._public_manifest(snapshot))
                )
                # Make the completed tree readable before the atomic rename, so a process crash
                # after rename needs only a read-only verification to recover its receipt.
                os.fchmod(stage_fd, 0o755)  # nosec B103 - public site; private reports excluded
                os.fsync(stage_fd)
            finally:
                os.close(stage_fd)
            # Both paths are anchored to the opened root. Other publishers use the same lock.
            os.rename(stage, release, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            stage_created = False
            os.fsync(root_fd)
        except OSError as exc:
            raise PublicationError("publication_filesystem_error") from exc
        finally:
            if stage_created:
                # The private staging directory was created by this invocation only.
                shutil.rmtree(stage, dir_fd=root_fd)
            if lock_fd >= 0:
                os.close(lock_fd)
            os.close(root_fd)
        return self._receipt(snapshot, release)

    def recover(
        self, build: WebsiteBuild, *, expected_digest: str, release_id: str
    ) -> dict[str, Any] | None:
        """Reconcile a crash after atomic rename, without changing any file or following links."""
        snapshot, root, release = self._validated(
            build, expected_digest=expected_digest, release_id=release_id
        )
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock_fd = -1
        release_fd = -1
        try:
            try:
                lock_fd = os.open(".publish.lock", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            else:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise PublicationError("publication_busy") from exc
            try:
                release_fd = os.open(
                    release, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
                )
            except FileNotFoundError:
                return None
            expected = {file.path: file.bytes() for file in self._public_files(snapshot)}
            expected["build-manifest.json"] = canonical_json(self._public_manifest(snapshot))
            self._verify_tree(release_fd, expected)
            return self._receipt(snapshot, release)
        except OSError as exc:
            raise PublicationError("release_verification_failed") from exc
        finally:
            if release_fd >= 0:
                os.close(release_fd)
            if lock_fd >= 0:
                os.close(lock_fd)
            os.close(root_fd)

    @staticmethod
    def _verify_tree(directory_fd: int, expected: dict[str, bytes]) -> None:
        before = os.fstat(directory_fd)
        if before.st_mode & 0o055 != 0o055:
            raise PublicationError("release_not_readable_by_host")
        children = {path.split("/", 1)[0] for path in expected}
        if set(os.listdir(directory_fd)) != children:
            raise PublicationError("release_file_set_mismatch")
        for name in children:
            if name in expected:
                child_fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
                )
                with os.fdopen(child_fd, "rb") as source:
                    details = os.fstat(source.fileno())
                    if not stat.S_ISREG(details.st_mode) or details.st_size != len(expected[name]):
                        raise PublicationError("release_file_mismatch")
                    if details.st_mode & 0o044 != 0o044:
                        raise PublicationError("release_not_readable_by_host")
                    if source.read(len(expected[name]) + 1) != expected[name]:
                        raise PublicationError("release_file_mismatch")
                    after = os.fstat(source.fileno())
                    if (details.st_mtime_ns, details.st_ino, details.st_size) != (
                        after.st_mtime_ns,
                        after.st_ino,
                        after.st_size,
                    ):
                        raise PublicationError("release_changed_during_verification")
            else:
                child_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                try:
                    StaticDirectoryPublisher._verify_tree(
                        child_fd,
                        {
                            path.split("/", 1)[1]: body
                            for path, body in expected.items()
                            if path.startswith(name + "/")
                        },
                    )
                finally:
                    os.close(child_fd)
        after = os.fstat(directory_fd)
        if (before.st_mtime_ns, before.st_ino) != (after.st_mtime_ns, after.st_ino):
            raise PublicationError("release_changed_during_verification")

    def _receipt(self, snapshot: WebsiteBuild, release: str) -> dict[str, Any]:
        return {
            "status": "published",
            "provider": "static_directory",
            "release_id": release,
            "digest": snapshot.digest,
            "reviewed_build_digest": snapshot.digest,
            "deployed_digest": hashlib.sha256(
                canonical_json(self._public_manifest(snapshot))
            ).hexdigest(),
            "file_count": len(self._public_files(snapshot)),
            "private_reports_excluded": len(snapshot.files) - len(self._public_files(snapshot)),
            "url": (self.public_base_url or "").rstrip("/") + "/" + release + "/index.html",
            "previous_releases_preserved": True,
            "hosting_requirements": {
                "application/octet-stream": "attachment; X-Content-Type-Options: nosniff"
            },
        }

    @staticmethod
    def _public_files(snapshot: WebsiteBuild) -> list[BuildFile]:
        # Internal migration/marketing/provider reports are review evidence, never site content.
        return [file for file in snapshot.files if not file.path.startswith("reports/")]

    @staticmethod
    def _public_manifest(snapshot: WebsiteBuild) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "reviewed_build_digest": snapshot.digest,
            "files": [
                {
                    "path": file.path,
                    "sha256": file.sha256,
                    "size_bytes": file.size_bytes,
                    "media_type": file.media_type,
                }
                for file in StaticDirectoryPublisher._public_files(snapshot)
            ],
        }

    @staticmethod
    def _write_file(root_fd: int, path: str, content: bytes) -> None:
        parts = path.split("/")
        current = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                try:
                    os.mkdir(part, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = child
                os.fchmod(current, 0o755)  # nosec B103 - public subtree inside private stage
            file_fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=current,
            )
            with os.fdopen(file_fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fchmod(file.fileno(), 0o644)
                os.fsync(file.fileno())
        finally:
            os.close(current)
