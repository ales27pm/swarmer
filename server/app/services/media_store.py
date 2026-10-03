"""Private bounded blobs. Job leases and owner identities remain SQLite-authoritative."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import aiosqlite
from PIL import Image

from app.services.agent_lease import lease_matches
from app.services.media_contracts import (
    MAX_MEDIA_BYTES,
    MEDIA_SKILLS,
    MediaArtifact,
    MediaResult,
    media_payload,
)


class MediaConflict(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_output") -> None:
        super().__init__(message)
        self.code = code


def media_root(db_path: Path) -> Path:
    return db_path.resolve().parent / (db_path.stem + "-media")


async def media_job_locked(db: aiosqlite.Connection, job_id: str) -> dict[str, Any]:
    row = await (
        await db.execute(
            """SELECT j.*,n.goal_run_id,n.status AS node_status,g.status AS goal_status,
        g.conversation_revision AS current_revision,n.conversation_revision AS node_revision,
        t.status AS task_status,root.source AS owner_id
        FROM agent_jobs j JOIN plan_nodes n ON n.task_id=j.task_id
        JOIN goal_runs g ON g.id=n.goal_run_id JOIN tasks root ON root.id=g.root_task_id
        JOIN tasks t ON t.id=j.task_id WHERE j.id=? AND n.required_skill=j.required_skill""",
            (job_id,),
        )
    ).fetchone()
    if row is None or row["required_skill"] not in MEDIA_SKILLS:
        raise MediaConflict("media job unavailable", code="lease_lost")
    return dict(row)


def require_media_lease(
    row: dict[str, Any], agent_id: str, token: str, lease_id: str, generation: int
) -> None:
    if (
        row["status"] not in {"claimed", "running"}
        or row["goal_status"] != "running"
        or row["task_status"] != "running"
        or row["node_status"] not in {"dispatched", "running"}
        or row["current_revision"] != row["node_revision"]
        or not lease_matches(
            row,
            agent_id=agent_id,
            token=token,
            lease_id=lease_id,
            lease_generation=generation,
            now=datetime.now(UTC).isoformat(),
            require_unexpired=True,
        )
    ):
        raise MediaConflict("media job lease is stale or inactive", code="lease_lost")


def decoded_metadata(body: bytes, media_type: str, row: dict[str, Any]) -> dict[str, Any]:
    payload = media_payload(row["required_skill"], json.loads(row["payload_json"]))
    details: dict[str, Any] = {
        "width": None,
        "height": None,
        "duration_ms": None,
        "sample_rate": None,
        "channels": None,
    }
    try:
        if row["required_skill"] == "image.generate":
            if (
                media_type != "image/png"
                or not body.startswith(b"\x89PNG\r\n\x1a\n")
                or not body.endswith(b"\0\0\0\0IEND\xaeB`\x82")
            ):
                raise ValueError("not a complete PNG")
            with Image.open(io.BytesIO(body)) as image:
                if (
                    image.format != "PNG"
                    or image.size != (payload["width"], payload["height"])
                    or getattr(image, "n_frames", 1) != 1
                    or image.mode not in {"RGB", "RGBA", "L"}
                ):
                    raise ValueError("image shape mismatch")
                image.verify()
            with Image.open(io.BytesIO(body)) as image:
                image.load()
                details.update(width=image.width, height=image.height)
        else:
            if (
                media_type != "audio/wav"
                or body[:4] != b"RIFF"
                or body[8:12] != b"WAVE"
                or int.from_bytes(body[4:8], "little") + 8 != len(body)
            ):
                raise ValueError("not a complete WAV")
            with wave.open(io.BytesIO(body), "rb") as audio:
                frames, rate = audio.getnframes(), audio.getframerate()
                if (
                    audio.getnchannels() != 1
                    or audio.getsampwidth() != 2
                    or rate != 24_000
                    or audio.getcomptype() != "NONE"
                    or frames <= 0
                    or frames > rate * payload["max_duration_seconds"]
                ):
                    raise ValueError("audio format or duration mismatch")
                if len(audio.readframes(frames + 1)) != frames * 2:
                    raise ValueError("truncated audio")
                details.update(
                    duration_ms=math.ceil(frames * 1000 / rate), sample_rate=rate, channels=1
                )
    except (OSError, ValueError, EOFError, wave.Error, Image.DecompressionBombError) as exc:
        raise MediaConflict("media format, dimensions or duration invalid") from exc
    return details


def _read_regular(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        import stat

        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise MediaConflict("media storage invalid")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise MediaConflict("media storage exceeds limit")
    return data


def read_metadata(root: Path, artifact_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"media_[a-f0-9]{40}", artifact_id) or root.is_symlink():
        raise MediaConflict("media unavailable")
    try:
        metadata = json.loads(_read_regular(root / (artifact_id + ".json"), 8192))
        ref = MediaArtifact.model_validate(metadata["artifact"]).model_dump()
        if ref["artifact_id"] != artifact_id:
            raise MediaConflict("media identity mismatch")
        return cast(dict[str, Any], metadata)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise MediaConflict("media unavailable or corrupt") from exc


def read_artifact(root: Path, artifact_id: str) -> tuple[dict[str, Any], bytes]:
    metadata = read_metadata(root, artifact_id)
    ref = metadata["artifact"]
    try:
        content = _read_regular(root / (artifact_id + ".blob"), MAX_MEDIA_BYTES)
    except OSError as exc:
        raise MediaConflict("media unavailable or corrupt") from exc
    if len(content) != ref["size_bytes"] or hashlib.sha256(content).hexdigest() != ref["sha256"]:
        raise MediaConflict("media checksum mismatch")
    return metadata, content


def verify_media_result(
    root: Path, row: dict[str, Any], result: object, *, check_blob: bool = True
) -> dict[str, Any]:
    try:
        parsed = MediaResult.model_validate(result)
        metadata = (
            read_artifact(root, parsed.artifact.artifact_id)[0]
            if check_blob
            else read_metadata(root, parsed.artifact.artifact_id)
        )
        ref = parsed.artifact.model_dump()
        if (
            metadata["artifact"] != ref
            or ref["job_id"] != row["id"]
            or metadata["lease_generation"] != row["lease_generation"]
            or metadata["agent_id"] != row["claimed_by"]
            or ref["media_type"]
            != ("image/png" if row["required_skill"] == "image.generate" else "audio/wav")
        ):
            raise MediaConflict("media reference does not belong to this execution")
        return parsed.model_dump()
    except (ValueError, TypeError, KeyError) as exc:
        raise MediaConflict("media result has no verified artifact") from exc


class MediaStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path, self.root = db_path, media_root(db_path)

    async def authorize_upload(
        self, agent_id: str, job_id: str, token: str, lease_id: str, generation: int
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            require_media_lease(
                await media_job_locked(db, job_id), agent_id, token, lease_id, generation
            )

    async def upload(
        self,
        agent_id: str,
        job_id: str,
        token: str,
        lease_id: str,
        generation: int,
        body: bytes,
        media_type: str,
        digest: str,
    ) -> dict[str, Any]:
        if (
            not 0 < len(body) <= MAX_MEDIA_BYTES
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or hashlib.sha256(body).hexdigest() != digest
        ):
            raise MediaConflict("media size or checksum invalid")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await media_job_locked(db, job_id)
            require_media_lease(row, agent_id, token, lease_id, generation)
            details = decoded_metadata(body, media_type, row)
            artifact_id = (
                "media_" + hashlib.sha256(f"{job_id}:{generation}".encode()).hexdigest()[:40]
            )
            ref = MediaArtifact(
                artifact_id=artifact_id,
                job_id=job_id,
                goal_id=row["goal_run_id"],
                media_type=cast(Literal["image/png", "audio/wav"], media_type),
                size_bytes=len(body),
                sha256=digest,
                **details,
            ).model_dump()
            if self.root.is_symlink():
                raise MediaConflict("media storage invalid")
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root.chmod(0o700)
            target = self.root / (artifact_id + ".json")
            if target.exists() or target.is_symlink():
                prior, _ = read_artifact(self.root, artifact_id)
                if prior["artifact"] != ref or prior["agent_id"] != agent_id:
                    raise MediaConflict("media already uploaded with different bytes")
                return ref
            metadata = json.dumps(
                {"artifact": ref, "agent_id": agent_id, "lease_generation": generation},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            blob = self.root / (artifact_id + ".blob")
            orphan = blob.exists() or blob.is_symlink()
            if orphan:
                # A crashed upload may have persisted bytes before its receipt. Never
                # replace different bytes, and never follow a symlink while recovering.
                try:
                    if _read_regular(blob, MAX_MEDIA_BYTES) != body:
                        raise MediaConflict("media orphan has different bytes")
                except OSError as exc:
                    raise MediaConflict("media orphan is unavailable") from exc
            entries = list(self.root.iterdir())
            added_bytes = len(metadata) + (0 if orphan else len(body))
            if len(entries) + (1 if orphan else 2) > 2048 or (
                sum(p.lstat().st_size for p in entries) + added_bytes > 512 * 1024 * 1024
            ):
                raise MediaConflict("media storage quota exceeded", code="storage_limit")
            require_media_lease(row, agent_id, token, lease_id, generation)
            created: list[Path] = []
            try:
                writes = [(target, metadata)] if orphan else [(blob, body), (target, metadata)]
                for path, data in writes:
                    temporary = self.root / f".pending-{artifact_id}-{uuid4().hex}"
                    fd = os.open(
                        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                    )
                    created.append(temporary)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # The SQLite writer lock serializes publication for this job.
                    # Readers never observe partial bytes or a partial receipt.
                    os.replace(temporary, path)
                    created.remove(temporary)
                    created.append(path)
                require_media_lease(row, agent_id, token, lease_id, generation)
                await db.commit()
            except BaseException:
                for path in reversed(created):
                    path.unlink(missing_ok=True)
                raise
            return ref

    async def artifacts(self, goal_id: str, owner_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            owner = await (
                await db.execute(
                    "SELECT t.source FROM goal_runs g JOIN tasks t ON t.id=g.root_task_id WHERE g.id=?",
                    (goal_id,),
                )
            ).fetchone()
            if owner is None or owner[0] != owner_id:
                raise MediaConflict("media goal unavailable")
            rows = await (
                await db.execute(
                    """SELECT j.* FROM agent_jobs j JOIN plan_nodes n ON n.task_id=j.task_id
                WHERE n.goal_run_id=? AND j.status='completed'
                AND j.required_skill IN ('image.generate','audio.synthesize') ORDER BY j.created_at,j.id LIMIT 101""",
                    (goal_id,),
                )
            ).fetchall()
            rows = list(rows)
            if len(rows) > 100:
                raise MediaConflict("media listing limit exceeded")
            results = []
            for row in rows:
                ref = verify_media_result(
                    self.root, dict(row), json.loads(row["result_json"]), check_blob=False
                )["artifact"]
                if ref["goal_id"] != goal_id:
                    raise MediaConflict("media goal mismatch")
                results.append(ref)
            return results

    async def content(
        self, goal_id: str, artifact_id: str, owner_id: str
    ) -> tuple[dict[str, Any], bytes]:
        refs = await self.artifacts(goal_id, owner_id)
        ref = next((r for r in refs if r["artifact_id"] == artifact_id), None)
        if ref is None:
            raise MediaConflict("media unavailable")
        metadata, body = read_artifact(self.root, artifact_id)
        if metadata["artifact"] != ref:
            raise MediaConflict("media changed")
        return ref, body
