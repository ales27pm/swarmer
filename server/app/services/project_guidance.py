"""Validate prompt evidence against the accepted revision, not model output."""

from __future__ import annotations

import hashlib

from app.services.project_contracts import ProjectPayload, ProjectResult


def validate_guidance_reads(payload: ProjectPayload, result: ProjectResult) -> None:
    before = {file.path: file.content for file in payload.files}
    after = {file.path: file.content for file in result.files}
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    guides = {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in before.items()
        if path.split("/")[-1].casefold() == "agents.md"
    }
    reads = {item.path: item.sha256 for item in result.guidance_reads}
    if len(reads) != len(result.guidance_reads) or any(
        guides.get(path) != sha for path, sha in reads.items()
    ):
        raise ValueError("project guidance receipt does not match its source revision")
    if not changed:
        return
    for path, digest in guides.items():
        directory = path.rsplit("/", 1)[0].casefold() + "/" if "/" in path else ""
        if reads.get(path) != digest and (
            not directory or any(target.casefold().startswith(directory) for target in changed)
        ):
            raise ValueError("applicable AGENTS.md must be read before changing project files")
