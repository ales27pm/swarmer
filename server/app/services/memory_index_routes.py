"""Paired-owner, uncached read-only coverage inspection route."""

from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.services.memory_index_coverage import (
    MemoryIndexCoverageError,
    MemoryIndexCoveragePage,
    MemoryIndexCoverageRequest,
)

if TYPE_CHECKING:
    from app.services.state_service import StateService


class _NoStoreRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
            except StarletteHTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler


def install_memory_index_routes(
    app: FastAPI, state: "StateService", require_device: Callable[..., Any]
) -> None:
    router = APIRouter(route_class=_NoStoreRoute)

    @router.get("/memory/index-coverage", response_model=MemoryIndexCoveragePage)
    async def inspect(
        principal: Annotated[dict[str, Any], Depends(require_device)],
        request: Annotated[MemoryIndexCoverageRequest, Query()],
    ) -> dict[str, Any]:
        # Existing control-plane pairing grants owner access. The supplied scope
        # is an exact filter, not a new authorization grant or project expansion.
        del principal
        try:
            return await state.memory_index_coverage(request)
        except MemoryIndexCoverageError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc

    app.include_router(router)
