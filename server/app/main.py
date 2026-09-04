from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from app.models import HealthResponse, TaskCreate, TaskRecord
from app.services.approval_gateway import ApprovalGateway
from app.services.auth_service import AuthService
from app.services.execution_engine import ExecutionEngine, ExecutionError
from app.services.orchestrator_service import OrchestratorError, OrchestratorService
from app.services.state_service import StateService
from app.settings import get_settings

settings = get_settings()
state = StateService(settings.db_path)
auth = AuthService(settings.db_path)
gateway = ApprovalGateway(settings.db_path)
executor = ExecutionEngine(settings.db_path, settings.workspace_root)
orchestrator = OrchestratorService(settings.llm_base_url, settings.orchestrator_model)
websockets: set[WebSocket] = set()


class PairComplete(BaseModel):
    code: str
    device_id: str
    name: str = "iPhone"


class ApprovalDecision(BaseModel):
    decision: str


class ApprovalRequest(BaseModel):
    task_id: str
    action: str
    summary: str
    risk: str = "medium"


class ToolProposal(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(min_length=1, max_length=2000)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await state.initialize()
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="monGARS Control Plane", version="0.4.0", lifespan=lifespan)


async def require_device(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="device authentication required")
    if not await auth.validate_token(authorization.removeprefix("Bearer ")):
        raise HTTPException(status_code=401, detail="invalid device token")


async def broadcast(event: dict) -> None:
    dead: list[WebSocket] = []
    for ws in websockets:
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        websockets.discard(ws)


async def run_tool_call(tool_call_id: str, task_id: str) -> dict:
    await state.update_task_status(task_id, "running")
    await broadcast({"type": "tool.running", "payload": {"id": tool_call_id, "task_id": task_id}})
    try:
        result = await executor.execute(tool_call_id)
    except Exception as exc:
        task = await state.update_task_status(task_id, "failed")
        await state.append_audit(
            "tool.failed", {"tool_call_id": tool_call_id, "task_id": task_id, "error": str(exc)}
        )
        failed = await executor.get(tool_call_id)
        await broadcast({"type": "tool.failed", "payload": failed or {"id": tool_call_id}})
        if task:
            await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
        return failed or {"id": tool_call_id, "status": "failed", "error": str(exc)}

    task = await state.update_task_status(task_id, "completed")
    await state.append_audit(
        "tool.completed", {"tool_call_id": tool_call_id, "task_id": task_id, "tool_name": result["tool_name"]}
    )
    await broadcast({"type": "tool.completed", "payload": result})
    if task:
        await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
    return result


async def handle_tool_proposal(task_id: str, request: ToolProposal) -> dict:
    if not await state.get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    try:
        record = await executor.create_tool_call(
            task_id=task_id,
            tool_name=request.tool_name,
            arguments=request.arguments,
            summary=request.summary,
        )
    except ExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await state.append_audit(
        "tool.proposed",
        {"tool_call_id": record["id"], "task_id": task_id, "tool_name": request.tool_name},
    )
    await broadcast({"type": "tool.proposed", "payload": record})

    if executor.requires_approval(request.tool_name):
        approval = await gateway.request(
            task_id,
            request.tool_name,
            request.summary,
            executor.default_risk(request.tool_name),
        )
        await executor.attach_approval(record["id"], approval["id"])
        record = await executor.get(record["id"]) or record
        await broadcast({"type": "approval.requested", "payload": approval})
        await broadcast({"type": "tool.updated", "payload": record})
        return record

    return await run_tool_call(record["id"], task_id)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="mongars-control-plane", version="0.4.0")


@app.post("/pairing/code")
async def pairing_code() -> dict:
    return {"code": await auth.create_pairing_code(), "expires_in_seconds": 600}


@app.post("/pairing/complete")
async def pairing_complete(request: PairComplete) -> dict:
    token = await auth.complete_pairing(request.code, request.device_id, request.name)
    if not token:
        raise HTTPException(status_code=400, detail="invalid or expired pairing code")
    return {"token": token}


@app.post("/tasks", response_model=TaskRecord, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_device)])
async def create_task(request: TaskCreate) -> TaskRecord:
    task = await state.create_task(TaskRecord.new(request))
    await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
    return task


@app.get("/tasks", response_model=list[TaskRecord], dependencies=[Depends(require_device)])
async def list_tasks(limit: int = 100) -> list[TaskRecord]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    return await state.list_tasks(limit)


@app.post("/tasks/{task_id}/plan", dependencies=[Depends(require_device)])
async def plan_task(task_id: str) -> dict:
    task = await state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    await state.update_task_status(task_id, "planned")
    await state.append_audit(
        "orchestrator.requested", {"task_id": task_id, "model": settings.orchestrator_model}
    )
    try:
        proposal = await orchestrator.plan(task.input, task.mode)
    except OrchestratorError as exc:
        await state.update_task_status(task_id, "failed")
        await state.append_audit("orchestrator.failed", {"task_id": task_id, "error": str(exc)})
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await state.append_audit(
        "orchestrator.proposed",
        {"task_id": task_id, "tool_name": proposal["tool_name"], "model": settings.orchestrator_model},
    )
    await broadcast({"type": "orchestrator.proposed", "payload": {"task_id": task_id, **proposal}})

    if proposal["tool_name"] == "none":
        updated = await state.update_task_status(task_id, "completed")
        payload = {"task_id": task_id, "proposal": proposal, "task": updated.model_dump(mode="json") if updated else None}
        await broadcast({"type": "task.updated", "payload": payload["task"]})
        return payload

    summary = proposal["summary"].strip() or f"Run {proposal['tool_name']}"
    return await handle_tool_proposal(
        task_id,
        ToolProposal(
            tool_name=proposal["tool_name"],
            arguments=proposal["arguments"],
            summary=summary,
        ),
    )


@app.post("/tasks/{task_id}/tool-calls", dependencies=[Depends(require_device)])
async def propose_tool_call(task_id: str, request: ToolProposal) -> dict:
    return await handle_tool_proposal(task_id, request)


@app.get("/tasks/{task_id}/tool-calls", dependencies=[Depends(require_device)])
async def list_tool_calls(task_id: str) -> list[dict]:
    return await executor.list_for_task(task_id)


@app.get("/sync/bootstrap", dependencies=[Depends(require_device)])
async def sync_bootstrap() -> dict:
    return await state.bootstrap()


@app.get("/approvals", dependencies=[Depends(require_device)])
async def approvals() -> list[dict]:
    return await gateway.list_pending()


@app.post("/approvals/request", dependencies=[Depends(require_device)])
async def request_approval(request: ApprovalRequest) -> dict:
    record = await gateway.request(request.task_id, request.action, request.summary, request.risk)
    await broadcast({"type": "approval.requested", "payload": record})
    return record


@app.post("/approvals/{approval_id}/decision", dependencies=[Depends(require_device)])
async def decide_approval(approval_id: str, request: ApprovalDecision) -> dict:
    if request.decision not in {"approve", "deny"}:
        raise HTTPException(status_code=400, detail="decision must be approve or deny")
    record = await gateway.decide(approval_id, request.decision)
    if not record:
        raise HTTPException(status_code=404, detail="approval not found")
    await broadcast({"type": "approval.decided", "payload": record})

    tool_call = await executor.get_by_approval(approval_id)
    if not tool_call:
        return record

    if request.decision == "deny":
        denied = await executor.mark_denied(tool_call["id"])
        await state.append_audit(
            "tool.denied", {"tool_call_id": tool_call["id"], "task_id": tool_call["task_id"]}
        )
        await broadcast({"type": "tool.denied", "payload": denied or tool_call})
        return {"approval": record, "tool_call": denied or tool_call}

    result = await run_tool_call(tool_call["id"], tool_call["task_id"])
    return {"approval": record, "tool_call": result}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    token = ws.query_params.get("token", "")
    if not await auth.validate_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    websockets.add(ws)
    try:
        await ws.send_json({"type": "connected", "payload": {"version": "0.4.0"}})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        websockets.discard(ws)
