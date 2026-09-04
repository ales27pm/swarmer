from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from app.models import HealthResponse, TaskCreate, TaskRecord
from app.services.approval_gateway import ApprovalGateway
from app.services.auth_service import AuthService
from app.services.state_service import StateService
from app.settings import get_settings

settings = get_settings()
state = StateService(settings.db_path)
auth = AuthService(settings.db_path)
gateway = ApprovalGateway(settings.db_path)
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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await state.initialize()
    yield


app = FastAPI(title="monGARS Control Plane", version="0.2.0", lifespan=lifespan)


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


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="mongars-control-plane", version="0.2.0")


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
    return record


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    token = ws.query_params.get("token", "")
    if not await auth.validate_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    websockets.add(ws)
    try:
        await ws.send_json({"type": "connected", "payload": {"version": "0.2.0"}})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        websockets.discard(ws)
