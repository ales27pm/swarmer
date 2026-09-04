from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, status

from app.models import HealthResponse, TaskCreate, TaskRecord
from app.services.state_service import StateService
from app.settings import get_settings

settings = get_settings()
state = StateService(settings.db_path)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await state.initialize()
    yield


app = FastAPI(
    title="monGARS Control Plane",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="mongars-control-plane", version="0.1.0")


@app.post("/tasks", response_model=TaskRecord, status_code=status.HTTP_201_CREATED)
async def create_task(request: TaskCreate) -> TaskRecord:
    task = TaskRecord.new(request)
    return await state.create_task(task)


@app.get("/tasks", response_model=list[TaskRecord])
async def list_tasks(limit: int = 100) -> list[TaskRecord]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    return await state.list_tasks(limit)
