"""Explicit conditional recipes assessed from existing reports, without execution.

The operator approves a qualification declaration and a criterion/oracle mapping.
Neither that declaration nor an authenticated worker credential attests a process.
All public observations remain worker-reported and grant no authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import uuid4

import aiosqlite

from app.services.audit_log import append_audit_event
from app.services.memory_lessons_contracts import (
    LessonAssessment,
    LessonEvidence,
    LessonModel,
    LessonNote,
    LessonPage,
    LessonProposal,
    LessonView,
    LessonWithdrawal,
    ProfileApproval,
    ProfileView,
    ProfileWithdrawal,
)
from app.services.memory_view_qualification import qualify_search_rows
from app.services.project_contracts import ProjectResult
from app.services.project_execution_read import read_project_execution_observation
from app.services.project_execution_receipts import canonical_sha

T = TypeVar("T")


class LessonError(ValueError):
    def __init__(self, code: str = "lesson_conflict", status_code: int = 409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _decode(raw: str, limit: int) -> Any:
    try:
        if not isinstance(raw, str) or len(raw.encode()) > limit:
            raise ValueError
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise LessonError("lesson_unavailable", 503) from exc


def _freeze[ModelT: LessonModel](request: ModelT) -> ModelT:
    # Lists inside frozen Pydantic models are still mutable; snapshot before await.
    return type(request).model_validate_json(request.model_dump_json())


async def _one(
    db: aiosqlite.Connection, sql: str, args: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    cursor = await db.execute(sql, args)
    cursor.row_factory = aiosqlite.Row
    row = await cursor.fetchone()
    return dict(row) if row else None


async def _project(db: aiosqlite.Connection, project_id: str) -> None:
    if not await _one(db, "SELECT id FROM coding_projects WHERE id=?", (project_id,)):
        raise LessonError("project_not_found", 404)


async def _latest_lesson(
    db: aiosqlite.Connection, project_id: str, lesson_id: str
) -> dict[str, Any]:
    row = await _one(
        db,
        "SELECT * FROM memory_procedure_lessons WHERE project_id=? AND id=? ORDER BY version DESC LIMIT 1",
        (project_id, lesson_id),
    )
    if row is None:
        raise LessonError("lesson_not_found", 404)
    return row


async def _latest_profile(db: aiosqlite.Connection, profile_id: str) -> dict[str, Any] | None:
    return await _one(
        db,
        "SELECT * FROM memory_execution_profiles WHERE id=? ORDER BY version DESC LIMIT 1",
        (profile_id,),
    )


def _body[ModelT: LessonModel](row: dict[str, Any], model: type[ModelT]) -> ModelT:
    value = _decode(row["body_json"], 32768)
    if canonical_sha(value) != row["body_sha256"]:
        raise LessonError("lesson_unavailable", 503)
    try:
        return model.model_validate(value)
    except (ValueError, TypeError, RecursionError) as exc:
        raise LessonError("lesson_unavailable", 503) from exc


async def _replay(db: aiosqlite.Connection, scope: str, request_id: str, digest: str) -> str | None:
    row = await _one(
        db,
        "SELECT * FROM memory_lesson_requests WHERE scope=? AND request_id=?",
        (scope, request_id),
    )
    if row is None:
        return None
    if row["request_sha256"] != digest:
        raise LessonError("lesson_request_reused")
    return str(row["target_id"])


async def _record_request(
    db: aiosqlite.Connection, scope: str, request_id: str, digest: str, target_id: str
) -> None:
    await db.execute(
        "INSERT INTO memory_lesson_requests VALUES(?,?,?,?,?)",
        (scope, request_id, digest, target_id, datetime.now(UTC).isoformat()),
    )


async def _binding_reasons(
    db: aiosqlite.Connection, project_id: str, candidate: LessonProposal
) -> list[str]:
    row = await _one(
        db,
        """SELECT g.*, c.active_goal_id FROM goal_runs g
        JOIN goal_project_links p ON p.goal_run_id=g.id
        LEFT JOIN goal_conversation_links l ON l.goal_run_id=g.id
        LEFT JOIN goal_conversations c ON c.id=l.conversation_id
        WHERE g.id=? AND p.project_id=?""",
        (candidate.goal_id, project_id),
    )
    if row is None:
        return ["scope_changed"]
    criteria = _decode(row["completion_criteria_json"], 16000)
    if (
        not isinstance(criteria, list)
        or len(criteria) > 20
        or not all(isinstance(v, str) for v in criteria)
    ):
        raise LessonError("lesson_unavailable", 503)
    if (
        row["conversation_revision"] != candidate.conversation_revision
        or row["active_goal_id"] not in (None, candidate.goal_id)
        or candidate.criterion_index >= len(criteria)
        or hashlib.sha256(criteria[candidate.criterion_index].encode()).hexdigest()
        != candidate.criterion_sha256
    ):
        return ["criterion_changed"]
    current = await _one(
        db,
        "SELECT id,sha256,goal_run_id FROM project_revisions WHERE project_id=? ORDER BY revision DESC LIMIT 1",
        (project_id,),
    )
    if current != {
        "id": candidate.revision_id,
        "sha256": candidate.source_sha256,
        "goal_run_id": candidate.goal_id,
    }:
        return ["revision_changed"]
    return []


async def _note(
    db: aiosqlite.Connection, project_id: str, candidate: LessonProposal
) -> tuple[LessonNote | None, str | None]:
    reference = candidate.note_ref
    if reference is None:
        return None, None
    cursor = await db.execute(
        "SELECT * FROM memory_items WHERE id=? AND scope=? AND sensitivity='normal'",
        (reference.memory_id, "project:" + project_id),
    )
    cursor.row_factory = aiosqlite.Row
    row = await cursor.fetchone()
    head = await _one(
        db, "SELECT * FROM memory_text_heads WHERE memory_id=?", (reference.memory_id,)
    )
    if (
        row is None
        or head is None
        or head["revision"] != reference.revision
        or not head["source_id"]
    ):
        return None, None
    if len(str(row["metadata_json"] or "").encode()) > 32768:
        return None, None
    try:
        qualified = (await qualify_search_rows(db, [row])).get(reference.memory_id)
    except (ValueError, TypeError, RecursionError):
        return None, None
    if qualified is None or qualified.original is None or qualified.view_token is None:
        return None, None
    note = LessonNote(
        memory_id=reference.memory_id,
        revision=reference.revision,
        original_content=qualified.original[0],
        canonical_content=row["content"],
    )
    # Text is resolved live, never copied into the lesson registry or audit.
    return note, canonical_sha([head, qualified.view_token, dict(row)])


async def _profile_for(
    db: aiosqlite.Connection, candidate: LessonProposal
) -> tuple[ProfileApproval | None, list[str]]:
    row = await _latest_profile(db, candidate.profile_id)
    if row is None:
        return None, ["profile_unknown"]
    if row["version"] != candidate.profile_version or row["status"] != "approved":
        return None, ["profile_changed"]
    profile = _body(row, ProfileApproval)
    if (
        profile.runtime != candidate.runtime
        or [p.profile for p in profile.profiles] != candidate.profiles
    ):
        return None, ["profile_recipe_mismatch"]
    reasons = [] if profile.qualification_kind == "isolated_runtime" else ["profile_synthetic_only"]
    if not any(c.criterion_sha256 == candidate.criterion_sha256 for c in profile.criteria):
        reasons.append("criterion_oracle_unavailable")
    return profile, reasons


async def _evidence(
    db: aiosqlite.Connection,
    project_id: str,
    candidate: LessonProposal,
    acceptance_id: str,
    profile: ProfileApproval | None,
    expected_sha: str | None = None,
) -> tuple[LessonEvidence, str | None, bool]:
    unknown = LessonEvidence(
        acceptance_id=acceptance_id, outcome="unknown", reasons=["evidence_unavailable"]
    )
    accepted = await _one(
        db,
        "SELECT * FROM project_execution_acceptances WHERE id=? AND project_id=?",
        (acceptance_id, project_id),
    )
    link = await _one(
        db,
        "SELECT * FROM project_execution_revision_links WHERE acceptance_id=? AND project_id=?",
        (acceptance_id, project_id),
    )
    if accepted is None or link is None:
        return unknown, None, False
    digest = canonical_sha([accepted, link])
    if expected_sha is not None and expected_sha != digest:
        return unknown, None, False
    report = await read_project_execution_observation(
        db,
        project_id=project_id,
        node_id=accepted["node_id"],
        job_id=accepted["job_id"],
        revision_id=link["revision_id"],
    )
    if report is None:
        return unknown, None, False
    current = link["revision_id"] == candidate.revision_id
    reasons = []
    if (
        accepted["goal_run_id"] != candidate.goal_id
        or accepted["conversation_revision"] != candidate.conversation_revision
        or report.source_sha256 != candidate.source_sha256
    ):
        reasons.append("execution_conditions_mismatch")
    if report.observation_status != "complete":
        return (
            LessonEvidence(
                acceptance_id=acceptance_id,
                outcome="incomplete",
                reasons=["measurement_incomplete", *reasons],
            ),
            digest,
            current,
        )
    if profile is None:
        return (
            LessonEvidence(
                acceptance_id=acceptance_id,
                outcome="unknown",
                reasons=["profile_unavailable", *reasons],
            ),
            digest,
            current,
        )
    if (
        accepted["producer_agent_id"] not in profile.producer_agent_ids
        or report.runtime != profile.runtime
        or report.runtime_image_id != profile.runtime_image_id
        or report.runner_sha256 != profile.runner_sha256
        or report.policy_sha256 != profile.policy_sha256
        or report.profiles_expected != candidate.profiles
    ):
        reasons.append("producer_profile_mismatch")
        return (
            LessonEvidence(acceptance_id=acceptance_id, outcome="unknown", reasons=reasons),
            digest,
            current,
        )
    for observed, expected in zip(report.profiles, profile.profiles, strict=True):
        observation = observed.observation
        if (
            observation is None
            or observed.profile != expected.profile
            or observation.harness_sha256 != expected.harness_sha256
            or observation.dependency_before_sha256 != expected.dependency_sha256
            or observation.dependency_after_sha256 != expected.dependency_sha256
            or observation.source_before_sha256 != candidate.source_sha256
            or observation.source_after_sha256 != candidate.source_sha256
            or observation.source_unchanged is not True
            or observation.environment_unchanged is not True
        ):
            reasons.append("execution_conditions_mismatch")
    row = await _one(db, "SELECT result_json FROM agent_jobs WHERE id=?", (accepted["job_id"],))
    if row is None:
        return unknown, None, False
    try:
        result = ProjectResult.model_validate(_decode(row["result_json"], 4_000_000))
    except (ValueError, TypeError, KeyError) as exc:
        raise LessonError("lesson_unavailable", 503) from exc
    if any("[output truncated]" in check.output.casefold() for check in result.checks):
        reasons.append("output_truncated")
    oracle = next(
        (o for o in profile.criteria if o.criterion_sha256 == candidate.criterion_sha256), None
    )
    files = {f.path: hashlib.sha256(f.content.encode()).hexdigest() for f in result.files}
    if oracle is None or any(files.get(f.path) != f.sha256 for f in oracle.oracle_files):
        reasons.append("criterion_oracle_mismatch")
    if oracle is not None:
        indexed = {p.profile: p for p in report.profiles}
        if any(
            indexed[name].tests_executed is None
            or (indexed[name].tests_executed or 0) < oracle.minimum_tests
            for name in oracle.test_profiles
        ):
            reasons.append("insufficient_tests")
    if reasons:
        return (
            LessonEvidence(
                acceptance_id=acceptance_id, outcome="unknown", reasons=sorted(set(reasons))
            ),
            digest,
            current,
        )
    outcome: Literal["passed", "failed", "incomplete", "unknown"]
    criterion_profiles = set(oracle.test_profiles) if oracle is not None else set()
    if any(
        p.profile in criterion_profiles and p.exit_code != 0 and (p.test_failures or 0) > 0
        for p in report.profiles
    ):
        outcome, reasons = "failed", []
    elif any(
        p.profile.endswith("_test") and p.profile not in criterion_profiles and p.exit_code != 0
        for p in report.profiles
    ):
        # Other recipe checks still prevent success, but are not evidence
        # contradicting the operator-selected criterion oracle.
        outcome, reasons = "incomplete", ["non_criterion_failure"]
    elif any(p.exit_code != 0 for p in report.profiles):
        outcome, reasons = "incomplete", ["non_test_failure"]
    else:
        outcome, reasons = "passed", []
    return (
        LessonEvidence(acceptance_id=acceptance_id, outcome=outcome, reasons=reasons),
        digest,
        current,
    )


async def _view(db: aiosqlite.Connection, row: dict[str, Any]) -> LessonView:
    candidate = _body(row, LessonProposal)
    reasons = await _binding_reasons(db, row["project_id"], candidate)
    profile, profile_reasons = await _profile_for(db, candidate)
    reasons.extend(profile_reasons)
    note, binding = await _note(db, row["project_id"], candidate)
    if candidate.note_ref is not None and (note is None or binding != row["note_binding_sha256"]):
        note = None
        reasons.append("note_unavailable")
    cursor = await db.execute(
        "SELECT * FROM memory_procedure_evidence WHERE lesson_id=? AND lesson_version=? ORDER BY acceptance_id",
        (row["id"], row["version"]),
    )
    cursor.row_factory = aiosqlite.Row
    refs = list(await cursor.fetchall())
    if (
        len(refs) > 8
        or canonical_sha({r["acceptance_id"]: r["evidence_sha256"] for r in refs})
        != row["evidence_set_sha256"]
    ):
        raise LessonError("lesson_unavailable", 503)
    evidence = []
    current = False
    for ref in refs:
        item, _, on_current = await _evidence(
            db, row["project_id"], candidate, ref["acceptance_id"], profile, ref["evidence_sha256"]
        )
        evidence.append(item)
        current |= on_current and not item.reasons
        reasons.extend(item.reasons)
    if not evidence:
        reasons.append("evidence_required")
    elif not current:
        reasons.append("current_execution_required")
    observation: Literal["passed", "failed", "incomplete", "unknown"]
    if any(e.outcome == "failed" for e in evidence):
        observation = "failed"
    elif evidence and all(e.outcome == "passed" for e in evidence):
        observation = "passed"
    elif any(e.outcome == "incomplete" for e in evidence):
        observation = "incomplete"
    else:
        observation = "unknown"
    state = row["lifecycle"]
    if state == "quarantined" or observation == "failed":
        reasons.append("comparable_failure")
    applicability: Literal["reported_conditions_match", "needs_revalidation", "withdrawn"]
    applicability = "reported_conditions_match" if not reasons else "needs_revalidation"
    if state == "withdrawn":
        applicability = "withdrawn"
        note = None
    return LessonView(
        lesson_id=row["id"],
        project_id=row["project_id"],
        version=row["version"],
        lifecycle=state,
        observation=observation,
        applicability=applicability,
        reasons=sorted(set(reasons)),
        candidate=candidate,
        evidence=evidence,
        note=note,
    )


class MemoryLessonService:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def _write(self, operation: Callable[[aiosqlite.Connection], Awaitable[T]]) -> T:
        async with aiosqlite.connect(self.db_path, timeout=10) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                result = await operation(db)
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def approve_profile(
        self, profile_id: str, request: ProfileApproval, actor_id: str
    ) -> ProfileView:
        request = _freeze(request)

        async def op(db: aiosqlite.Connection) -> ProfileView:
            digest = canonical_sha(["approve", profile_id, request.model_dump()])
            replay = await _replay(db, "profiles", request.request_id, digest)
            old = await _latest_profile(db, profile_id)
            if replay:
                if old is None:
                    raise LessonError("lesson_replay_unavailable")
                return ProfileView(
                    profile_id=profile_id,
                    version=old["version"],
                    status=old["status"],
                    approval=_body(old, ProfileApproval),
                )
            if (old["version"] if old else 0) != request.expected_version:
                raise LessonError("profile_version_changed")
            for agent in request.producer_agent_ids:
                if not await _one(db, "SELECT id FROM agents WHERE id=?", (agent,)):
                    raise LessonError("profile_producer_unavailable")
            version = request.expected_version + 1
            body = request.model_dump()
            await db.execute(
                "INSERT INTO memory_execution_profiles VALUES(?,?,?,?,?,?,?)",
                (
                    profile_id,
                    version,
                    "approved",
                    _json(body),
                    canonical_sha(body),
                    actor_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            await _record_request(db, "profiles", request.request_id, digest, profile_id)
            await append_audit_event(
                db,
                actor_type="operator",
                actor_id=actor_id,
                event_type="memory.profile.approved",
                payload={"profile_id": profile_id, "version": version},
            )
            return ProfileView(
                profile_id=profile_id, version=version, status="approved", approval=request
            )

        return await self._write(op)

    async def revoke_profile(
        self, profile_id: str, request: ProfileWithdrawal, actor_id: str
    ) -> ProfileView:
        request = _freeze(request)

        async def op(db: aiosqlite.Connection) -> ProfileView:
            digest = canonical_sha(["revoke", profile_id, request.model_dump()])
            replay = await _replay(db, "profiles", request.request_id, digest)
            old = await _latest_profile(db, profile_id)
            if old is None:
                raise LessonError("profile_not_found", 404)
            approval = _body(old, ProfileApproval)
            if replay:
                return ProfileView(
                    profile_id=profile_id,
                    version=old["version"],
                    status=old["status"],
                    approval=approval,
                )
            if old["version"] != request.expected_version:
                raise LessonError("profile_version_changed")
            version = old["version"] + 1
            await db.execute(
                "INSERT INTO memory_execution_profiles VALUES(?,?,?,?,?,?,?)",
                (
                    profile_id,
                    version,
                    "revoked",
                    old["body_json"],
                    old["body_sha256"],
                    actor_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            await _record_request(db, "profiles", request.request_id, digest, profile_id)
            await append_audit_event(
                db,
                actor_type="operator",
                actor_id=actor_id,
                event_type="memory.profile.revoked",
                payload={"profile_id": profile_id, "version": version},
            )
            return ProfileView(
                profile_id=profile_id, version=version, status="revoked", approval=approval
            )

        return await self._write(op)

    async def propose(self, project_id: str, request: LessonProposal, actor_id: str) -> LessonView:
        request = _freeze(request)

        async def op(db: aiosqlite.Connection) -> LessonView:
            await _project(db, project_id)
            digest = canonical_sha(["propose", request.model_dump()])
            replay = await _replay(db, project_id, request.request_id, digest)
            if replay:
                return await _view(db, await _latest_lesson(db, project_id, replay))
            if request.expected_version != 0 or await _binding_reasons(db, project_id, request):
                raise LessonError("lesson_binding_changed")
            note, binding = await _note(db, project_id, request)
            if request.note_ref is not None and note is None:
                raise LessonError("lesson_note_unavailable")
            lesson_id = "lesson_" + uuid4().hex
            body = request.model_dump()
            await db.execute(
                "INSERT INTO memory_procedure_lessons VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    lesson_id,
                    1,
                    project_id,
                    "candidate",
                    _json(body),
                    canonical_sha(body),
                    canonical_sha({}),
                    binding,
                    actor_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            await _record_request(db, project_id, request.request_id, digest, lesson_id)
            await append_audit_event(
                db,
                actor_type="device",
                actor_id=actor_id,
                event_type="memory.lesson.proposed",
                payload={"lesson_id": lesson_id, "version": 1},
            )
            return await _view(db, await _latest_lesson(db, project_id, lesson_id))

        return await self._write(op)

    async def _change(
        self,
        project_id: str,
        lesson_id: str,
        request: LessonAssessment | LessonWithdrawal,
        actor_id: str,
    ) -> LessonView:
        request = _freeze(request)
        withdrawing = isinstance(request, LessonWithdrawal)

        async def op(db: aiosqlite.Connection) -> LessonView:
            await _project(db, project_id)
            digest = canonical_sha(
                ["withdraw" if withdrawing else "assess", lesson_id, request.model_dump()]
            )
            replay = await _replay(db, project_id, request.request_id, digest)
            old = await _latest_lesson(db, project_id, lesson_id)
            if replay:
                return await _view(db, old)
            if old["version"] != request.expected_version or old["lifecycle"] == "withdrawn":
                raise LessonError("lesson_version_changed")
            candidate = _body(old, LessonProposal)
            # Preserve the complete selection: assessment may add evidence, never
            # erase a counterexample to obtain a more favorable observation.
            refs = await (
                await db.execute(
                    "SELECT acceptance_id,evidence_sha256 FROM memory_procedure_evidence WHERE lesson_id=? AND lesson_version=?",
                    (lesson_id, old["version"]),
                )
            ).fetchall()
            evidence = {str(ref[0]): str(ref[1]) for ref in refs}
            if canonical_sha(evidence) != old["evidence_set_sha256"]:
                raise LessonError("lesson_unavailable", 503)
            state = "withdrawn" if withdrawing else "assessed"
            if isinstance(request, LessonAssessment):
                profile, profile_reasons = await _profile_for(db, candidate)
                for acceptance_id in request.acceptance_ids:
                    observed, evidence_sha, _ = await _evidence(
                        db,
                        project_id,
                        candidate,
                        acceptance_id,
                        profile,
                        evidence.get(acceptance_id),
                    )
                    if evidence_sha is None:
                        raise LessonError("lesson_evidence_unavailable")
                    evidence[acceptance_id] = evidence_sha
                    if (
                        observed.outcome == "failed"
                        and not observed.reasons
                        and not profile_reasons
                    ):
                        state = "quarantined"
                if old["lifecycle"] == "quarantined":
                    state = "quarantined"
            if len(evidence) > 8:
                raise LessonError("lesson_evidence_budget")
            version = old["version"] + 1
            await db.execute(
                "INSERT INTO memory_procedure_lessons VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    lesson_id,
                    version,
                    project_id,
                    state,
                    old["body_json"],
                    old["body_sha256"],
                    canonical_sha(evidence),
                    old["note_binding_sha256"],
                    actor_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            # Retain selected identities after proof deletion. Missing proof is
            # unavailable, never silently removed from the denominator.
            for acceptance_id, evidence_sha in evidence.items():
                await db.execute(
                    "INSERT INTO memory_procedure_evidence VALUES(?,?,?,?)",
                    (lesson_id, version, acceptance_id, evidence_sha),
                )
            await _record_request(db, project_id, request.request_id, digest, lesson_id)
            await append_audit_event(
                db,
                actor_type="device",
                actor_id=actor_id,
                event_type="memory.lesson." + ("withdrawn" if withdrawing else "assessed"),
                payload={"lesson_id": lesson_id, "version": version, "lifecycle": state},
            )
            return await _view(db, await _latest_lesson(db, project_id, lesson_id))

        return await self._write(op)

    async def assess(
        self, project_id: str, lesson_id: str, request: LessonAssessment, actor_id: str
    ) -> LessonView:
        return await self._change(project_id, lesson_id, request, actor_id)

    async def withdraw(
        self, project_id: str, lesson_id: str, request: LessonWithdrawal, actor_id: str
    ) -> LessonView:
        return await self._change(project_id, lesson_id, request, actor_id)

    async def get(self, project_id: str, lesson_id: str) -> LessonView:
        async with aiosqlite.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            await _project(db, project_id)
            return await _view(db, await _latest_lesson(db, project_id, lesson_id))

    async def list(
        self, project_id: str, *, after_id: str | None = None, limit: int = 50
    ) -> LessonPage:
        if (
            type(limit) is not int
            or not 1 <= limit <= 50
            or (
                after_id is not None
                and (not isinstance(after_id, str) or not 1 <= len(after_id) <= 200)
            )
        ):
            raise LessonError("lesson_page_invalid", 422)
        async with aiosqlite.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            await _project(db, project_id)
            cursor = await db.execute(
                """SELECT l.* FROM memory_procedure_lessons l
                WHERE project_id=? AND id>? AND version=(SELECT MAX(version) FROM memory_procedure_lessons v WHERE v.id=l.id)
                ORDER BY id LIMIT ?""",
                (project_id, after_id or "", limit + 1),
            )
            cursor.row_factory = aiosqlite.Row
            rows = list(await cursor.fetchall())
            more = len(rows) > limit
            selected = rows[:limit]
            return LessonPage(
                items=[await _view(db, dict(row)) for row in selected],
                has_more=more,
                next_after_id=selected[-1]["id"] if more else None,
            )

    async def get_profile(self, profile_id: str) -> ProfileView:
        async with aiosqlite.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            row = await _latest_profile(db, profile_id)
            if row is None:
                raise LessonError("profile_not_found", 404)
            return ProfileView(
                profile_id=profile_id,
                version=row["version"],
                status=row["status"],
                approval=_body(row, ProfileApproval),
            )
