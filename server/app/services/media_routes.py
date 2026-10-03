"""Authenticated uploads under current worker leases and owner-only binary delivery."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

from app.services.media_contracts import MAX_MEDIA_BYTES
from app.services.media_store import MediaConflict, MediaStore


def install_media_routes(
    app: FastAPI,
    db_path: Path,
    require_device: Callable[..., Any],
    require_agent: Callable[..., Any],
) -> None:
    store = MediaStore(db_path)
    upload_slots = asyncio.Semaphore(2)

    @app.post("/agents/{agent_id}/jobs/{job_id}/media")
    async def upload_media(
        agent_id: str,
        job_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(require_agent)],
        x_claim_token: Annotated[str, Header(min_length=20, max_length=500)],
        x_lease_id: Annotated[str, Header(min_length=10, max_length=200)],
        x_lease_generation: Annotated[int, Header(ge=1)],
        x_artifact_sha256: Annotated[str, Header(pattern=r"^[a-f0-9]{64}$")],
    ) -> dict[str, Any]:
        del principal
        media_type = request.headers.get("content-type", "")
        if media_type not in {"image/png", "audio/wav"}:
            raise HTTPException(415, "media type unsupported")
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or not 0 < int(length) <= MAX_MEDIA_BYTES):
            raise HTTPException(413, "media size limit exceeded")
        try:
            await store.authorize_upload(
                agent_id, job_id, x_claim_token, x_lease_id, x_lease_generation
            )
            async with asyncio.timeout(30), upload_slots:
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_MEDIA_BYTES:
                        raise HTTPException(413, "media size limit exceeded")
                    body.extend(chunk)
                return await store.upload(
                    agent_id,
                    job_id,
                    x_claim_token,
                    x_lease_id,
                    x_lease_generation,
                    bytes(body),
                    media_type,
                    x_artifact_sha256,
                )
        except TimeoutError as exc:
            raise HTTPException(408, "media upload timed out") from exc
        except MediaConflict as exc:
            status_code = {"lease_lost": 409, "storage_limit": 507}.get(exc.code, 422)
            raise HTTPException(status_code, str(exc), headers={"X-Media-Error": exc.code}) from exc

    @app.get("/goals/{goal_id}/media")
    async def list_media(
        goal_id: str,
        response: Response,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            return {"artifacts": await store.artifacts(goal_id, str(principal["id"]))}
        except MediaConflict as exc:
            raise HTTPException(404, "media unavailable") from exc

    @app.get("/goals/{goal_id}/media/{artifact_id}")
    async def get_media(
        goal_id: str,
        artifact_id: str,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> Response:
        try:
            ref, body = await store.content(goal_id, artifact_id, str(principal["id"]))
        except MediaConflict as exc:
            raise HTTPException(404, "media unavailable") from exc
        return Response(
            body,
            media_type=ref["media_type"],
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": ref["sha256"],
                "Content-Disposition": 'inline; filename="'
                + artifact_id
                + ('.png"' if ref["media_type"] == "image/png" else '.wav"'),
            },
        )
