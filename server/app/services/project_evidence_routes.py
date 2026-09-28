"""Authenticated evidence routes installed by the application composition root."""

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi import Path as PathParameter

from app.services.project_evidence import (
    EvidenceGoalNotFound,
    EvidenceMappingConflict,
    read_project_evidence,
    save_project_evidence,
)
from app.services.project_evidence_contracts import EvidenceMappingRequest, RequirementEvidenceView
from app.services.project_graph import ProjectGraphEvidenceError


def install_evidence_routes(
    app: FastAPI,
    db_path: Path,
    require_device: Callable[..., Any],
) -> None:
    @app.get("/goals/{goal_id}/evidence", response_model=RequirementEvidenceView)
    async def get_evidence(
        goal_id: str,
        response: Response,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> RequirementEvidenceView:
        del principal
        response.headers["Cache-Control"] = "private, no-store"
        try:
            view = await read_project_evidence(db_path, goal_id)
        except (ProjectGraphEvidenceError, sqlite3.Error) as exc:
            raise HTTPException(503, "requirement evidence unavailable") from exc
        if view is None:
            raise HTTPException(404, "goal not found")
        return view

    @app.put("/goals/{goal_id}/evidence/{criterion_index}", response_model=RequirementEvidenceView)
    async def put_evidence(
        goal_id: str,
        criterion_index: Annotated[int, PathParameter(ge=0, le=19)],
        request: EvidenceMappingRequest,
        response: Response,
        principal: Annotated[dict[str, Any], Depends(require_device)],
    ) -> RequirementEvidenceView:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            return await save_project_evidence(
                db_path,
                goal_id,
                criterion_index,
                request,
                str(principal["id"]),
            )
        except EvidenceGoalNotFound as exc:
            raise HTTPException(404, "goal not found") from exc
        except EvidenceMappingConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ProjectGraphEvidenceError, sqlite3.Error) as exc:
            raise HTTPException(503, "requirement evidence unavailable") from exc
