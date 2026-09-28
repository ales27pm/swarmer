"""Paired-device website API and short-lived, script-free previews."""

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response

from app.services.website_builder import WebsiteBuild
from app.services.website_workflow import WebsiteWorkflow
from app.services.website_workflow_contracts import (
    WebsiteCommand,
    WebsiteCreate,
    WebsitePublish,
    WebsiteReview,
)
from app.services.website_workflow_store import WebsiteConflict

HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
PREVIEW_CSP = (
    "sandbox allow-same-origin; default-src 'none'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def install_website_routes(
    app: FastAPI, service: WebsiteWorkflow, require_device: Callable[..., Any]
) -> None:
    router = APIRouter(
        prefix="/website-projects",
        tags=["website-projects"],
        dependencies=[Depends(require_device)],
    )

    def owner(principal: dict[str, Any]) -> str:
        return str(principal["id"])

    def failure(exc: Exception) -> HTTPException:
        if isinstance(exc, KeyError):
            return HTTPException(404, "Projet web introuvable.")
        return HTTPException(409 if isinstance(exc, WebsiteConflict) else 422, str(exc))

    def screenshot_bytes(data: dict[str, Any], sha256: str) -> bytes:
        if not data.get("capture_dir") or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise KeyError(sha256)
        for render in service._read(f"{data['capture_dir']}/rendered.json"):
            for viewport in render["viewports"]:
                shot = viewport["screenshot"]
                if shot["sha256"] != sha256:
                    continue
                path = Path(shot["local_path"]).resolve()
                if not path.is_relative_to((service.root / data["capture_dir"]).resolve()):
                    raise KeyError(sha256)
                content = path.read_bytes()
                if hashlib.sha256(content).hexdigest() != sha256:
                    raise KeyError(sha256)
                return content
        raise KeyError(sha256)

    @router.post("/{project_id}/screenshots/{sha256}/preview")
    async def screenshot_preview(
        project_id: str, sha256: str, principal: Annotated[dict[str, Any], Depends(require_device)]
    ) -> dict[str, Any]:
        try:
            data = service.store.get(project_id, owner(principal))
            screenshot_bytes(data, sha256)
            token = secrets.token_urlsafe(32)
            with service.store.transaction() as connection:
                connection.execute(
                    "DELETE FROM website_preview_tokens WHERE expires<?", (time.time(),)
                )
                connection.execute(
                    "INSERT INTO website_preview_tokens VALUES(?,?,?,?)",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        project_id,
                        "screenshot:" + sha256,
                        time.time() + 300,
                    ),
                )
            return {
                "path": f"/website-previews/{token}/screenshot.png",
                "sha256": sha256,
                "expires_in_seconds": 300,
            }
        except KeyError as exc:
            raise failure(exc) from None

    @router.get("/capabilities")
    async def capabilities(response: Response) -> dict[str, Any]:
        response.headers.update(HEADERS)
        return service.capabilities()

    @router.get("")
    async def projects(
        response: Response, principal: Annotated[dict[str, Any], Depends(require_device)]
    ) -> list[dict[str, Any]]:
        response.headers.update(HEADERS)
        return [service.public(data) for data in service.store.list(owner(principal))]

    @router.post("", status_code=201)
    async def create(
        request: WebsiteCreate, principal: Annotated[dict[str, Any], Depends(require_device)]
    ) -> dict[str, Any]:
        try:
            return service.create(owner(principal), request)
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.get("/{project_id}")
    async def project(
        project_id: str,
        response: Response,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> dict[str, Any]:
        response.headers.update(HEADERS)
        try:
            return service.public(service.store.get(project_id, owner(principal)))
        except KeyError as exc:
            raise failure(exc) from None

    @router.post("/{project_id}/commands", status_code=202)
    async def command(
        project_id: str,
        request: WebsiteCommand,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> dict[str, Any]:
        try:
            return service.command(owner(principal), project_id, request)
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.post("/{project_id}/preview")
    async def preview(
        project_id: str,
        request: WebsiteReview,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> dict[str, Any]:
        try:
            return service.preview(owner(principal), project_id, request)
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.post("/{project_id}/publication-review")
    async def review(
        project_id: str,
        request: WebsiteReview,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> dict[str, Any]:
        try:
            return service.prepare_publication(owner(principal), project_id, request)
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.post("/{project_id}/publish")
    async def publish(
        project_id: str,
        request: WebsitePublish,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> dict[str, Any]:
        try:
            return await service.publish(owner(principal), project_id, request)
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.get("/{project_id}/dossier")
    async def dossier(
        project_id: str, principal: Annotated[dict[str, Any], Depends(require_device)]
    ) -> Response:
        try:
            data = service.store.get(project_id, owner(principal))
            if not data.get("capture_dir"):
                raise KeyError(project_id)
            return Response(
                json.dumps(
                    service._read(f"{data['capture_dir']}/dossier.json"), ensure_ascii=False
                ),
                media_type="application/json",
                headers=HEADERS,
            )
        except KeyError as exc:
            raise failure(exc) from None

    @router.get("/{project_id}/screenshots/{sha256}")
    async def screenshot(
        project_id: str, sha256: str, principal: Annotated[dict[str, Any], Depends(require_device)]
    ) -> Response:
        try:
            data = service.store.get(project_id, owner(principal))
            return Response(screenshot_bytes(data, sha256), media_type="image/png", headers=HEADERS)
        except KeyError as exc:
            raise failure(exc) from None

    app.include_router(router)

    def preview_response(token: str, file_path: str) -> Response:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", token):
            raise HTTPException(404, "Aperçu introuvable ou expiré.")
        with service.store.transaction() as connection:
            row = connection.execute(
                "SELECT project_id,build_digest FROM website_preview_tokens WHERE token_hash=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Aperçu introuvable ou expiré.")
        data = service.store.get(row["project_id"])
        if row["build_digest"].startswith("screenshot:"):
            if file_path != "screenshot.png":
                raise HTTPException(404, "Capture introuvable.")
            try:
                return Response(
                    screenshot_bytes(data, row["build_digest"].removeprefix("screenshot:")),
                    media_type="image/png",
                    headers=HEADERS,
                )
            except KeyError:
                raise HTTPException(404, "Capture introuvable ou remplacée.") from None
        # A new capture/build invalidates every earlier preview token immediately.
        if not data.get("build") or data["build"]["digest"] != row["build_digest"]:
            raise HTTPException(404, "Cet aperçu a été remplacé.")
        build = WebsiteBuild.model_validate(service._read(data["build_file"]))
        build.verify()
        # Loading can overlap a new capture/build once it runs off the event loop.
        current = service.store.get(row["project_id"])
        if (
            not current.get("build")
            or current["build"]["digest"] != row["build_digest"]
            or build.digest != row["build_digest"]
        ):
            raise HTTPException(404, "Cet aperçu a été remplacé.")
        item = next(
            (f for f in build.files if f.path == file_path and not f.path.startswith("reports/")),
            None,
        )
        if item is None:
            raise HTTPException(404, "Fichier absent de l’aperçu.")
        headers = {**HEADERS, "Content-Security-Policy": PREVIEW_CSP}
        if item.media_type in {"application/pdf", "application/octet-stream"}:
            headers["Content-Disposition"] = 'attachment; filename="document.pdf"'
            return Response(item.bytes(), media_type="application/octet-stream", headers=headers)
        return Response(item.bytes(), media_type=item.media_type, headers=headers)

    @app.get("/website-previews/{token}/{file_path:path}", include_in_schema=False)
    async def preview_file(token: str, file_path: str) -> Response:
        return await asyncio.to_thread(preview_response, token, file_path)
