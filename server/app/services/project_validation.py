"""Coverage limits of the schema 1.0 Python/Node project-validation lane."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import aiosqlite

from app.services.goal_conversation import GoalConversationService
from app.services.project_contracts import ProjectFile, ProjectPayload, ProjectResult

NATIVE_VALIDATION_DIAGNOSTIC = (
    "Validation Swift/iOS non prise en charge par ce parcours. "
    "Les contrôles Python/npm ne valident pas les fichiers Swift/iOS. "
    "Les fichiers et les résultats de vérification sont conservés. "
    "Le projet attend une validation native distincte, après approbation de la révision exacte. "
    "Cette pause ne constitue pas une validation ni une fin de projet."
)


def native_validation_unavailable(files: Iterable[ProjectFile | Mapping[str, Any]]) -> bool:
    """Detect actual native paths, never model prose or a claimed check command.

    Schema 1.0 has no native validation receipt. Even a command named xcodebuild
    cannot independently establish that a supported native runner verified it.
    Historical snapshots remain decodable; readiness is guarded at use sites.
    """
    for file in files:
        path = (file.path if isinstance(file, ProjectFile) else str(file["path"])).casefold()
        if path.endswith(".swift") or any(
            part.endswith((".xcodeproj", ".xcworkspace")) for part in path.split("/")
        ):
            return True
    return False


def native_project(value: ProjectPayload | ProjectResult | Mapping[str, Any]) -> bool:
    """Preserve native coverage even after removal of the final Swift path."""
    if isinstance(value, Mapping):
        return value.get("native_validation") in {
            "authoring",
            "required",
        } or native_validation_unavailable(value.get("files", []))
    return value.native_validation is not None or native_validation_unavailable(value.files)


def native_authoring(_value: ProjectResult | Mapping[str, Any]) -> bool:
    """Compatibility recovery decodes new history but keeps native authoring paused."""
    return False


async def pause_native_validation_locked(
    db: aiosqlite.Connection, goal_id: str, *, now: str
) -> None:
    """Pause an active historical result without rewriting snapshots or receipts.

    The caller owns the write transaction and any maintenance/model-call lease.
    This is a technical capability gap, not a question or permission request.
    """
    updated = await db.execute(
        """UPDATE goal_runs SET status='waiting_permission',current_phase='needs_user',
        evaluator_status=NULL,evaluator_summary=?,paused_at=COALESCE(paused_at,?),updated_at=?
        WHERE id=? AND status NOT IN ('completed','failed','cancelled','budget_exhausted')""",
        (NATIVE_VALIDATION_DIAGNOSTIC, now, now, goal_id),
    )
    if updated.rowcount != 1:
        return
    await db.execute(
        """UPDATE tasks SET status='waiting_permission',updated_at=?
        WHERE id=(SELECT root_task_id FROM goal_runs WHERE id=?)""",
        (now, goal_id),
    )
    await GoalConversationService.assistant_locked(
        db, goal_id, NATIVE_VALIDATION_DIAGNOSTIC, question=False, now=now
    )
