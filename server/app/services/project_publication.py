from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from app.services.project_contracts import ProjectWriteArguments


def _rename_exclusive(parent_fd: int, source: str, destination: str) -> None:
    """Publish a directory atomically, refusing even an existing empty directory.

    Linux renameat2(RENAME_NOREPLACE) and Darwin renameatx_np(RENAME_EXCL)
    provide this guarantee; ordinary os.rename may overwrite empty directories.
    Unsupported filesystems/platforms fail closed.
    """
    library = ctypes.CDLL(None, use_errno=True)
    symbol, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    rename = getattr(library, symbol, None)
    if rename is None:
        raise OSError("atomic exclusive project publication is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), flag) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def publish_project(parent_fd: int, manifest: ProjectWriteArguments) -> dict[str, Any]:
    """Write a private tree, fsync, then expose exactly one immutable revision.

    parent_fd is the executor's no-follow descriptor for the validated generated
    project revision parent. No caller/model chooses an absolute host path.
    """
    stage = f".project-stage-{uuid4().hex}"
    os.mkdir(stage, mode=0o700, dir_fd=parent_fd)
    published = False
    try:
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            for file in manifest.files:
                directory_fd = os.dup(stage_fd)
                try:
                    parts = PurePosixPath(file.path).parts
                    for part in parts[:-1]:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                            os.fsync(directory_fd)
                        except FileExistsError:
                            pass
                        next_fd = os.open(
                            part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                        )
                        os.close(directory_fd)
                        directory_fd = next_fd
                    descriptor = os.open(
                        parts[-1],
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(file.content.encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        _rename_exclusive(parent_fd, stage, manifest.revision_id)
        published = True
        os.fsync(parent_fd)
    finally:
        if not published:
            shutil.rmtree(stage, dir_fd=parent_fd)
    return {
        "path": manifest.path,
        "sha256": manifest.sha256,
        "files": len(manifest.files),
        "bytes": sum(len(file.content.encode("utf-8")) for file in manifest.files),
    }
