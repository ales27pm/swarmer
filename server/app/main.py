import ipaddress
import logging
import secrets
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    AgentCreate,
    AgentHeartbeat,
    ChatCreate,
    FeedbackCreate,
    HealthResponse,
    MemoryCreate,
    MemorySearch,
    MemoryUpdate,
    TaskCreate,
    TaskRecord,
    TaskStatus,
)
from app.services.approval_binding import (
    public_tool_arguments,
    public_tool_error,
    public_tool_summary,
)
from app.services.approval_gateway import ApprovalConflict, ApprovalGateway
from app.services.auth_service import AuthService, PairingConflict, PairingRateLimited
from app.services.execution_engine import (
    AuthenticatedRequester,
    ExecutionConflict,
    ExecutionEngine,
    ExecutionError,
    ExecutionOutcomeUncertain,
)
from app.services.orchestrator_service import OrchestratorError, OrchestratorService
from app.services.permission_policy import PermissionPolicy
from app.services.state_service import StateConflict, StateService
from app.settings import Settings, get_settings

API_VERSION = "0.7.0"
logger = logging.getLogger(__name__)


class PairComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^\d{6}$")
    device_id: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    name: str = Field(default="iPhone", min_length=1, max_length=200)


class PairingCandidateResponse(BaseModel):
    pairing_id: str = Field(pattern=r"^pair_[0-9a-f]{32}$")
    candidate_token: str = Field(min_length=32, json_schema_extra={"readOnly": True})
    device_id: str
    expires_in_seconds: int = Field(ge=30, le=300)


class PairFinalize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairing_id: str = Field(pattern=r"^pair_[0-9a-f]{32}$")
    device_id: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")


class PairFinalizeResponse(BaseModel):
    status: Literal["ready", "active"]
    device_id: str
    pairing_id: str
    already_finalized: bool


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "allow_once", "deny"]
    user_note: str | None = Field(default=None, max_length=2_000)


class ToolProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any]
    summary: str = Field(
        min_length=1,
        max_length=2_000,
        description=(
            "Proposal-only caller context. Actual tool-call and approval summaries are replaced "
            "with fixed server labels."
        ),
    )


DevicePrincipal = dict[str, Any]


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    return token or None


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_runtime_boundaries(settings: Settings) -> None:
    workspace = settings.workspace_root.resolve()
    if workspace in {Path("/"), Path.home().resolve()}:
        raise RuntimeError("workspace_root must be a dedicated project directory")
    for name, protected in {
        "db_path": settings.db_path,
        "permissions_path": settings.permissions_path,
    }.items():
        resolved = protected.resolve()
        if resolved == workspace or workspace in resolved.parents:
            raise RuntimeError(f"{name} must be outside workspace_root")


def create_app(config: Settings | None = None) -> FastAPI:
    settings = config or get_settings()
    _validate_runtime_boundaries(settings)
    state_service = StateService(settings.db_path)
    configured_pairing_secret = settings.pairing_bootstrap_token
    auth_service = AuthService(
        settings.db_path,
        pairing_ttl_seconds=settings.pairing_code_ttl_seconds,
        pairing_max_attempts=settings.pairing_max_attempts,
        pairing_candidate_ttl_seconds=settings.pairing_candidate_ttl_seconds,
        pairing_pepper=(
            configured_pairing_secret.get_secret_value()
            if configured_pairing_secret is not None
            else secrets.token_urlsafe(32)
        ),
    )
    approval_gateway = ApprovalGateway(settings.db_path)
    permission_policy = PermissionPolicy.from_yaml(settings.permissions_path)
    execution_engine = ExecutionEngine(settings.db_path, settings.workspace_root, permission_policy)
    orchestrator_service = OrchestratorService(settings.llm_base_url, settings.orchestrator_model)
    websockets: set[WebSocket] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await state_service.initialize()
        settings.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        yield

    app = FastAPI(title="monGARS Control Plane", version=API_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.state_service = state_service
    app.state.auth_service = auth_service
    app.state.approval_gateway = approval_gateway
    app.state.execution_engine = execution_engine
    app.state.orchestrator_service = orchestrator_service

    async def require_secure_transport(request: Request) -> None:
        if settings.allow_insecure_remote_http:
            return
        host = request.client.host if request.client else ""
        if not _is_loopback(host) and request.url.scheme != "https":
            raise HTTPException(status_code=426, detail="HTTPS is required outside loopback")

    async def require_device(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> DevicePrincipal:
        await require_secure_transport(request)
        token = _bearer_token(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="device authentication required")
        principal = await auth_service.authenticate_token(token)
        if not principal:
            raise HTTPException(status_code=401, detail="invalid device token")
        return principal

    async def require_bootstrap_principal(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> DevicePrincipal:
        await require_secure_transport(request)
        token = _bearer_token(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="device authentication required")
        principal = await auth_service.authenticate_token(token)
        if principal is None:
            principal = await auth_service.authenticate_pairing_candidate(token)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid device or pairing credential")
        return principal

    async def require_pairing_operator(
        request: Request,
        x_mongars_operator_token: Annotated[str | None, Header()] = None,
    ) -> None:
        configured = settings.pairing_bootstrap_token
        if configured is None or not configured.get_secret_value():
            raise HTTPException(status_code=503, detail="pairing bootstrap is not configured")
        host = request.client.host if request.client else ""
        if not _is_loopback(host):
            raise HTTPException(status_code=403, detail="pairing codes are issued locally only")
        supplied = x_mongars_operator_token or ""
        if not secrets.compare_digest(configured.get_secret_value(), supplied):
            raise HTTPException(status_code=403, detail="operator authentication required")

    async def broadcast(event: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for websocket in websockets:
            try:
                await websocket.send_json(event)
            except (OSError, RuntimeError, WebSocketDisconnect):
                dead.append(websocket)
        for websocket in dead:
            websockets.discard(websocket)

    async def snapshot_tool_and_task(
        tool_call_id: str, task_id: str
    ) -> tuple[dict[str, Any] | None, TaskRecord | None]:
        tool_call = None
        task = None
        try:
            tool_call = await execution_engine.get(tool_call_id)
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            tool_call = None
        try:
            task = await state_service.get_task(task_id)
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            task = None
        return tool_call, task

    async def append_terminal_message(
        task_id: str,
        role: Literal["agent", "system"],
        content: str,
        tool_call_id: str,
        verified_status: Literal["completed", "failed"],
    ) -> None:
        try:
            await state_service.append_task_message(
                task_id,
                role,
                content,
                agent_id="local-executor" if role == "agent" else None,
                metadata={"tool_call_id": tool_call_id, "verified_status": verified_status},
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            # The durable tool/task transition is authoritative even if its UI message fails.
            return

    async def publish_terminal_snapshot(
        tool_call: dict[str, Any], task: TaskRecord | None
    ) -> dict[str, Any]:
        tool_call_id = str(tool_call["id"])
        task_id = str(tool_call["task_id"])
        durable_status = str(tool_call["status"])
        if durable_status == "completed":
            await append_terminal_message(
                task_id,
                "agent",
                f"{tool_call['tool_name']} completed with a verified executor result.",
                tool_call_id,
                "completed",
            )
            event_type = "tool.completed"
        elif durable_status == "failed":
            safe_error = str(tool_call.get("error") or "tool execution failed")
            await append_terminal_message(
                task_id,
                "system",
                f"Tool execution failed: {safe_error}",
                tool_call_id,
                "failed",
            )
            event_type = "tool.failed"
        else:
            raise RuntimeError("only durable terminal tool calls can be published")

        await broadcast({"type": event_type, "payload": tool_call})
        if task:
            await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
        return tool_call

    async def publish_execution_rejected(
        tool_call: dict[str, Any], task: TaskRecord | None, cause: BaseException
    ) -> dict[str, Any]:
        tool_call_id = str(tool_call["id"])
        task_id = str(tool_call["task_id"])
        durable_status = str(tool_call["status"])
        tool_name = str(tool_call.get("tool_name", "unknown"))
        safe_error = public_tool_error(tool_name, cause) or "tool execution was rejected"
        await state_service.append_audit(
            "tool.execution_rejected",
            {
                "tool_call_id": tool_call_id,
                "error": safe_error,
                "durable_status": durable_status,
            },
            task_id=task_id,
            trace_id=task_id,
        )
        await broadcast({"type": "tool.execution_rejected", "payload": tool_call})
        if task:
            await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
        return tool_call

    async def resolve_terminal_or_report_uncertain(
        tool_call_id: str, task_id: str, cause: BaseException
    ) -> dict[str, Any]:
        tool_call, task = await snapshot_tool_and_task(tool_call_id, task_id)
        durable_status = str(tool_call["status"]) if tool_call else "unknown"
        if tool_call and durable_status in {"completed", "failed"}:
            return await publish_terminal_snapshot(tool_call, task)
        if (
            tool_call
            and durable_status != "running"
            and not isinstance(cause, ExecutionOutcomeUncertain)
        ):
            return await publish_execution_rejected(tool_call, task, cause)

        try:
            await state_service.append_audit(
                "tool.outcome_uncertain",
                {
                    "tool_call_id": tool_call_id,
                    "durable_status": durable_status,
                    "outcome": "uncertain",
                    "retry": False,
                },
                task_id=task_id,
                trace_id=task_id,
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            # Reporting must never replace the stable non-retry response after dispatch.
            logger.warning("could not persist the outcome-uncertain audit marker")
        await broadcast(
            {
                "type": "tool.outcome_uncertain",
                "payload": tool_call
                or {"id": tool_call_id, "task_id": task_id, "status": "unknown"},
            }
        )
        if task:
            await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
        raise HTTPException(
            status_code=409,
            detail="tool outcome is uncertain; the call must not be retried",
        ) from cause

    async def run_tool_call(tool_call_id: str, task_id: str) -> dict[str, Any]:
        try:
            result = await execution_engine.execute(tool_call_id)
        except ExecutionOutcomeUncertain as exc:
            return await resolve_terminal_or_report_uncertain(tool_call_id, task_id, exc)
        except Exception as exc:  # noqa: BLE001 - durable state, not exception type, is authority
            current, task = await snapshot_tool_and_task(tool_call_id, task_id)
            durable_status = str(current["status"]) if current else "unknown"
            if current and durable_status in {"completed", "failed"}:
                return await publish_terminal_snapshot(current, task)
            if current is None or durable_status == "running":
                return await resolve_terminal_or_report_uncertain(tool_call_id, task_id, exc)
            return await publish_execution_rejected(current, task, exc)

        try:
            task = await state_service.get_task(task_id)
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            task = None
        return await publish_terminal_snapshot(result, task)

    async def handle_tool_proposal(
        task_id: str, request: ToolProposal, principal: DevicePrincipal
    ) -> dict[str, Any]:
        try:
            record = await execution_engine.create_tool_call(
                task_id=task_id,
                tool_name=request.tool_name,
                arguments=request.arguments,
                summary=request.summary,
                requester=AuthenticatedRequester(
                    id=str(principal["id"]),
                    name=str(principal["name"]),
                ),
            )
        except ExecutionConflict as exc:
            status_code = 404 if str(exc) == "task not found" else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except ExecutionError as exc:
            raise HTTPException(
                status_code=400, detail="tool proposal failed executor validation"
            ) from exc

        await state_service.append_audit(
            "tool.proposed",
            {"tool_call_id": record["id"], "tool_name": request.tool_name},
            actor_type="device",
            actor_id=str(principal["id"]),
            task_id=task_id,
            trace_id=task_id,
        )
        await broadcast({"type": "tool.proposed", "payload": record})

        if execution_engine.requires_approval(request.tool_name):
            approval_id = record["approval_id"]
            approval = await approval_gateway.get(str(approval_id))
            if approval is None:
                raise HTTPException(status_code=500, detail="approval linkage was not persisted")
            await broadcast({"type": "approval.requested", "payload": approval})
            await broadcast({"type": "tool.updated", "payload": record})
            return record

        return await run_tool_call(record["id"], task_id)

    async def plan_existing_task(task_id: str, principal: DevicePrincipal) -> dict[str, Any]:
        task = await state_service.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task.status not in {TaskStatus.CREATED, TaskStatus.PLANNED}:
            raise HTTPException(
                status_code=409, detail=f"task cannot be planned from {task.status.value}"
            )
        try:
            await state_service.update_task_status(task_id, "planned")
        except StateConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await state_service.append_audit(
            "orchestrator.requested",
            {"model": settings.orchestrator_model},
            task_id=task_id,
            trace_id=task_id,
        )
        try:
            proposal = await orchestrator_service.plan(task.input, task.mode.value)
        except OrchestratorError as exc:
            try:
                await state_service.update_task_status(task_id, "failed", error=str(exc))
            except StateConflict:
                pass
            await state_service.append_audit(
                "orchestrator.failed",
                {"error": str(exc)},
                task_id=task_id,
                trace_id=task_id,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if proposal["tool_name"] != "none":
            try:
                execution_engine.validate_arguments(proposal["tool_name"], proposal["arguments"])
            except ExecutionError as exc:
                await state_service.append_audit(
                    "orchestrator.rejected",
                    {"reason": "executor validation failed"},
                    task_id=task_id,
                    trace_id=task_id,
                )
                raise HTTPException(
                    status_code=400, detail="tool proposal failed executor validation"
                ) from exc

        await state_service.append_audit(
            "orchestrator.proposed",
            {"tool_name": proposal["tool_name"], "model": settings.orchestrator_model},
            task_id=task_id,
            trace_id=task_id,
        )
        public_proposal = {
            **proposal,
            "arguments": public_tool_arguments(proposal["tool_name"], proposal["arguments"]),
            "summary": (
                proposal["summary"]
                if proposal["tool_name"] == "none"
                else public_tool_summary(proposal["tool_name"])
            ),
        }
        await broadcast(
            {
                "type": "orchestrator.proposed",
                "payload": {"task_id": task_id, **public_proposal},
            }
        )
        if proposal["tool_name"] == "none":
            updated = await state_service.get_task(task_id)
            if proposal["summary"].strip():
                await state_service.append_task_message(
                    task_id,
                    "agent",
                    proposal["summary"].strip(),
                    agent_id="local-orchestrator",
                    metadata={"verified_status": "proposal_only"},
                )
            return {
                "task_id": task_id,
                "proposal": public_proposal,
                "task": updated.model_dump(mode="json") if updated else None,
            }

        return await handle_tool_proposal(
            task_id,
            ToolProposal(
                tool_name=proposal["tool_name"],
                arguments=proposal["arguments"],
                summary=public_tool_summary(proposal["tool_name"]),
            ),
            principal,
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", service="mongars-control-plane", version=API_VERSION)

    @app.post("/pairing/code", dependencies=[Depends(require_pairing_operator)])
    async def pairing_code() -> dict[str, Any]:
        return {
            "code": await auth_service.create_pairing_code(),
            "expires_in_seconds": auth_service.pairing_ttl_seconds,
        }

    @app.post("/pairing/complete", response_model=PairingCandidateResponse)
    async def pairing_complete(
        request: PairComplete, http_request: Request
    ) -> PairingCandidateResponse:
        await require_secure_transport(http_request)
        try:
            candidate = await auth_service.complete_pairing(
                request.code, request.device_id, request.name
            )
        except PairingRateLimited as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if not candidate:
            raise HTTPException(status_code=400, detail="invalid or expired pairing code")
        return PairingCandidateResponse(
            pairing_id=candidate.pairing_id,
            candidate_token=candidate.token,
            device_id=candidate.device_id,
            expires_in_seconds=candidate.expires_in_seconds,
        )

    @app.post("/pairing/finalize", response_model=PairFinalizeResponse)
    async def pairing_finalize(
        request: PairFinalize,
        http_request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> PairFinalizeResponse:
        await require_secure_transport(http_request)
        token = _bearer_token(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="pairing candidate authentication required")
        try:
            finalized = await auth_service.finalize_pairing(
                token, request.pairing_id, request.device_id
            )
        except PairingConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if finalized is None:
            raise HTTPException(status_code=401, detail="invalid or expired pairing candidate")
        return PairFinalizeResponse(
            status=finalized.status,
            device_id=finalized.device_id,
            pairing_id=finalized.pairing_id,
            already_finalized=finalized.already_finalized,
        )

    @app.post(
        "/tasks",
        response_model=TaskRecord,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_task(
        request: TaskCreate,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> TaskRecord:
        task = await state_service.create_task(TaskRecord.new(request, source=str(principal["id"])))
        await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
        return task

    @app.get("/tasks", response_model=list[TaskRecord])
    async def list_tasks(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    ) -> list[TaskRecord]:
        del principal
        return await state_service.list_tasks(limit, task_status.value if task_status else None)

    @app.get("/tasks/{task_id}")
    async def get_task_detail(
        task_id: str, principal: Annotated[DevicePrincipal, Depends(require_device)]
    ) -> dict[str, Any]:
        del principal
        # Listing first materializes expiry before the task snapshot is read.
        approvals = await approval_gateway.list_for_task(task_id)
        task = await state_service.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        return {
            "task": task.model_dump(mode="json"),
            "messages": await state_service.list_messages_for_task(task_id),
            "approvals": approvals,
            "tool_calls": await execution_engine.list_for_task(task_id),
        }

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(
        task_id: str, principal: Annotated[DevicePrincipal, Depends(require_device)]
    ) -> TaskRecord:
        try:
            task = await state_service.cancel_task(task_id, actor_id=str(principal["id"]))
        except StateConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
        return task

    @app.post("/tasks/{task_id}/plan")
    async def plan_task(
        task_id: str, principal: Annotated[DevicePrincipal, Depends(require_device)]
    ) -> dict[str, Any]:
        return await plan_existing_task(task_id, principal)

    @app.post("/tasks/{task_id}/tool-calls")
    async def propose_tool_call(
        task_id: str,
        request: ToolProposal,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        return await handle_tool_proposal(task_id, request, principal)

    @app.get("/tasks/{task_id}/tool-calls")
    async def list_tool_calls(
        task_id: str, principal: Annotated[DevicePrincipal, Depends(require_device)]
    ) -> list[dict[str, Any]]:
        del principal
        if not await state_service.get_task(task_id):
            raise HTTPException(status_code=404, detail="task not found")
        return await execution_engine.list_for_task(task_id)

    @app.post("/chat", status_code=status.HTTP_201_CREATED)
    async def create_chat(
        request: ChatCreate,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        conversation_id, task = await state_service.create_chat_task(
            request.content, request.conversation_id, request.mode.value, str(principal["id"])
        )
        await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
        return {"conversation_id": conversation_id, "task": task.model_dump(mode="json")}

    @app.get("/conversations")
    async def list_conversations(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[dict[str, Any]]:
        del principal
        return await state_service.list_conversations(limit)

    @app.get("/conversations/{conversation_id}/messages")
    async def list_messages(
        conversation_id: str,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
    ) -> list[dict[str, Any]]:
        del principal
        return await state_service.list_messages(conversation_id, limit)

    @app.get("/sync/bootstrap")
    async def sync_bootstrap(
        principal: Annotated[DevicePrincipal, Depends(require_bootstrap_principal)],
    ) -> dict[str, Any]:
        del principal
        return await state_service.bootstrap()

    @app.get("/approvals")
    async def approvals(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        approval_status: Annotated[
            Literal["pending", "approved", "denied", "expired", "cancelled"],
            Query(alias="status"),
        ] = "pending",
    ) -> list[dict[str, Any]]:
        del principal
        return await approval_gateway.list_by_status(approval_status)

    @app.post("/approvals/{approval_id}/decision")
    async def decide_approval(
        approval_id: str,
        request: ApprovalDecision,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        decision = "approve" if request.decision in {"approve", "allow_once"} else "deny"
        try:
            record = await approval_gateway.decide(
                approval_id,
                decision,
                actor_id=str(principal["id"]),
                user_note=request.user_note,
            )
        except ApprovalConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not record:
            raise HTTPException(status_code=404, detail="approval not found")
        await broadcast({"type": "approval.decided", "payload": record})
        tool_call = await execution_engine.get_by_approval(approval_id)
        if not tool_call:
            return record
        if decision == "deny":
            await broadcast({"type": "tool.denied", "payload": tool_call})
            return {"approval": record, "tool_call": tool_call}
        result = await run_tool_call(tool_call["id"], tool_call["task_id"])
        return {"approval": record, "tool_call": result}

    @app.get("/memory")
    async def list_memory(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> list[dict[str, Any]]:
        del principal
        return await state_service.list_memory(limit)

    @app.post("/memory/search")
    async def search_memory(
        request: MemorySearch,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> list[dict[str, Any]]:
        del principal
        return await state_service.search_memory(request)

    @app.post("/memory", status_code=status.HTTP_201_CREATED)
    @app.post("/memory/remember", status_code=status.HTTP_201_CREATED, include_in_schema=False)
    async def create_memory(
        request: MemoryCreate,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        return await state_service.create_memory(request, str(principal["id"]))

    @app.patch("/memory/{memory_id}")
    async def update_memory(
        memory_id: str,
        request: MemoryUpdate,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        record = await state_service.update_memory(memory_id, request, str(principal["id"]))
        if not record:
            raise HTTPException(status_code=404, detail="memory not found")
        return record

    @app.delete("/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_memory(
        memory_id: str, principal: Annotated[DevicePrincipal, Depends(require_device)]
    ) -> None:
        if not await state_service.delete_memory(memory_id, str(principal["id"])):
            raise HTTPException(status_code=404, detail="memory not found")

    @app.get("/agents")
    async def list_agents(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> list[dict[str, Any]]:
        del principal
        return await state_service.list_agents()

    @app.get("/agents/{agent_id}")
    async def get_agent(
        agent_id: str, principal: Annotated[DevicePrincipal, Depends(require_device)]
    ) -> dict[str, Any]:
        del principal
        record = await state_service.get_agent(agent_id)
        if not record:
            raise HTTPException(status_code=404, detail="agent not found")
        return record

    @app.post("/agents/register", status_code=status.HTTP_201_CREATED)
    async def register_agent(
        request: AgentCreate,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        parsed = urlparse(str(request.endpoint))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(status_code=422, detail="agent endpoint must be an http(s) URL")
        return await state_service.register_agent(request, str(principal["id"]))

    @app.post("/agents/{agent_id}/heartbeat")
    async def heartbeat_agent(
        agent_id: str,
        request: AgentHeartbeat,
        http_request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        await require_secure_transport(http_request)
        credential = _bearer_token(authorization)
        record = await state_service.heartbeat_agent(agent_id, request.status, credential or "")
        if not record:
            raise HTTPException(status_code=401, detail="invalid agent credential")
        return record

    @app.get("/audit")
    @app.get("/sync/audit", include_in_schema=False)
    async def list_audit(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        after_id: Annotated[int | None, Query(ge=0)] = None,
    ) -> list[dict[str, Any]]:
        del principal
        return await state_service.list_audit(limit, after_id)

    @app.post("/feedback", status_code=status.HTTP_201_CREATED)
    @app.post("/sync/feedback", status_code=status.HTTP_201_CREATED, include_in_schema=False)
    async def create_feedback(
        request: FeedbackCreate,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        if request.task_id and not await state_service.get_task(request.task_id):
            raise HTTPException(status_code=404, detail="task not found")
        if request.agent_id and not await state_service.get_agent(request.agent_id):
            raise HTTPException(status_code=404, detail="agent not found")
        return await state_service.create_feedback(request, str(principal["id"]))

    @app.post("/ws/ticket")
    async def websocket_ticket(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        await require_secure_transport(request)
        token = _bearer_token(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="device authentication required")
        ticket = await auth_service.create_websocket_ticket(token)
        if not ticket:
            raise HTTPException(status_code=401, detail="invalid device token")
        return {"ticket": ticket, "expires_in_seconds": 30}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        peer_host = websocket.client.host if websocket.client else ""
        if (
            not settings.allow_insecure_remote_http
            and not _is_loopback(peer_host)
            and websocket.url.scheme != "wss"
        ):
            await websocket.close(code=4403)
            return
        ticket = websocket.query_params.get("ticket", "")
        principal = await auth_service.consume_websocket_ticket(ticket)
        if not principal:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        websockets.add(websocket)
        try:
            await websocket.send_json(
                {
                    "type": "connected",
                    "payload": {"version": API_VERSION, "device_id": principal["device_id"]},
                }
            )
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            websockets.discard(websocket)

    return app


app = create_app()
