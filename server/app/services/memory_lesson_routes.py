"""Explicit owner proposals and local operator profile declarations; no execution."""

import sqlite3
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.services.memory_index_routes import _NoStoreRoute
from app.services.memory_lessons import LessonError, MemoryLessonService
from app.services.memory_lessons_contracts import (
    LessonAssessment,
    LessonId,
    LessonPage,
    LessonProposal,
    LessonView,
    LessonWithdrawal,
    ProfileApproval,
    ProfileView,
    ProfileWithdrawal,
)


async def _respond[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except LessonError as exc:
        raise HTTPException(exc.status_code, exc.code) from exc
    except sqlite3.Error as exc:
        raise HTTPException(503, "lesson_unavailable") from exc


class _LessonRoute(_NoStoreRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            if request.method in {"PUT", "POST"}:
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 65536:
                        return JSONResponse(
                            {"detail": "lesson_request_too_large"},
                            status_code=413,
                            headers={"Cache-Control": "no-store"},
                        )
                    body.extend(chunk)
                request._body = bytes(body)
            try:
                return await original(request)
            except RecursionError:
                return JSONResponse(
                    {"detail": "lesson_request_invalid"},
                    status_code=422,
                    headers={"Cache-Control": "no-store"},
                )

        return handler


def install_lesson_routes(
    app: FastAPI,
    db_path: Path,
    require_device: Callable[..., Any],
    require_operator: Callable[..., Any],
) -> None:
    router = APIRouter(route_class=_LessonRoute)
    service = MemoryLessonService(db_path)

    @router.put("/memory/execution-profiles/{profile_id}", response_model=ProfileView)
    async def approve(
        profile_id: LessonId,
        request: ProfileApproval,
        operator: Annotated[None, Depends(require_operator)],
    ) -> ProfileView:
        return await _respond(service.approve_profile(profile_id, request, "local_operator"))

    @router.post("/memory/execution-profiles/{profile_id}/withdraw", response_model=ProfileView)
    async def revoke(
        profile_id: LessonId,
        request: ProfileWithdrawal,
        operator: Annotated[None, Depends(require_operator)],
    ) -> ProfileView:
        return await _respond(service.revoke_profile(profile_id, request, "local_operator"))

    @router.get("/memory/execution-profiles/{profile_id}", response_model=ProfileView)
    async def profile(
        profile_id: LessonId, operator: Annotated[None, Depends(require_operator)]
    ) -> ProfileView:
        return await _respond(service.get_profile(profile_id))

    @router.post("/projects/{project_id}/lessons", response_model=LessonView)
    async def propose(
        project_id: LessonId,
        request: LessonProposal,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> LessonView:
        return await _respond(service.propose(project_id, request, str(principal["id"])))

    @router.get("/projects/{project_id}/lessons", response_model=LessonPage)
    async def listing(
        project_id: LessonId,
        principal: Annotated[dict[str, Any], Depends(require_device)],
        after_id: Annotated[LessonId | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
    ) -> LessonPage:
        return await _respond(service.list(project_id, after_id=after_id, limit=limit))

    @router.get("/projects/{project_id}/lessons/{lesson_id}", response_model=LessonView)
    async def get(
        project_id: LessonId,
        lesson_id: LessonId,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> LessonView:
        return await _respond(service.get(project_id, lesson_id))

    @router.post("/projects/{project_id}/lessons/{lesson_id}/assess", response_model=LessonView)
    async def assess(
        project_id: LessonId,
        lesson_id: LessonId,
        request: LessonAssessment,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> LessonView:
        return await _respond(service.assess(project_id, lesson_id, request, str(principal["id"])))

    @router.post("/projects/{project_id}/lessons/{lesson_id}/withdraw", response_model=LessonView)
    async def withdraw(
        project_id: LessonId,
        lesson_id: LessonId,
        request: LessonWithdrawal,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> LessonView:
        return await _respond(
            service.withdraw(project_id, lesson_id, request, str(principal["id"]))
        )

    app.include_router(router)
