import asyncio
import ipaddress
import logging
import secrets
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
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
    AgentCapabilityPoll,
    AgentCapabilityRequest,
    AgentCreate,
    AgentHeartbeat,
    AgentJobClaim,
    AgentJobDispatch,
    AgentJobHeartbeat,
    AgentJobResult,
    ChatCreate,
    FeedbackCorrection,
    FeedbackCreate,
    HealthResponse,
    IPhoneCapabilityDecision,
    IPhoneCapabilityExecute,
    IPhoneCapabilityResultSubmit,
    MemoryCreate,
    MemorySearch,
    MemoryUpdate,
    TaskCreate,
    TaskRecord,
    TaskStatus,
)
from app.services.agent_card import AgentCardPolicyError, validate_agent_registration
from app.services.agent_dispatcher import AgentDispatchConflict, AgentDispatcher
from app.services.agent_lease_reaper import AgentLeaseReaper
from app.services.agent_liveness import agent_counts_as_active
from app.services.agent_scoring import AgentScoringService
from app.services.approval_binding import (
    public_tool_arguments,
    public_tool_error,
    public_tool_summary,
)
from app.services.approval_gateway import ApprovalConflict, ApprovalGateway
from app.services.auth_service import AuthService, PairingConflict, PairingRateLimited
from app.services.context_builder import ContextBuilder
from app.services.control_plane_instance import ControlPlaneInstanceService
from app.services.embedding_service import HttpEmbeddingService
from app.services.episode_memory import EpisodeMemoryService
from app.services.evaluator_provider import UbuntuEvaluatorProvider
from app.services.event_privacy import safe_websocket_event
from app.services.execution_engine import (
    AuthenticatedRequester,
    ExecutionConflict,
    ExecutionEngine,
    ExecutionError,
    ExecutionOutcomeUncertain,
)
from app.services.feedback_dataset import FeedbackDatasetService
from app.services.goal_manager import GoalManager, GoalManagerConflict
from app.services.idempotency import (
    IdempotencyConflict,
    IdempotencyService,
    SafeMutationRequest,
)
from app.services.iphone_capability_service import (
    IPhoneCapabilityConflict,
    IPhoneCapabilityService,
)
from app.services.maintenance_lease import (
    MaintenanceLeaseGuard,
    MaintenanceLeaseRunner,
    MaintenanceLeaseService,
)
from app.services.message_board import (
    MessageBoard,
    RedisStreamsMessageBoard,
    SQLiteMessageBoard,
)
from app.services.message_consumer import ConsumerCheckpointStore
from app.services.model_router import ModelRouter
from app.services.orchestrator_service import OrchestratorError, OrchestratorService
from app.services.permission_policy import PermissionPolicy, PermissionPolicyError
from app.services.planner_provider import UbuntuLLMPlannerProvider, UbuntuSwarmPlannerProvider
from app.services.remote_job_policy import RemoteJobPolicyError, validate_remote_job
from app.services.result_aggregator import ResultAggregator
from app.services.state_service import StateConflict, StateService
from app.services.strategy_retrieval import StrategyRetrieval
from app.services.swarm_contracts import (
    GoalCancelRequest,
    GoalCreateRequest,
    GoalDetail,
    GoalFeedbackRecord,
    GoalFeedbackRequest,
    GoalRecord,
    GoalReplanRequest,
    GoalResult,
    GoalStartRequest,
    ModelRole,
    ModelRoleConfig,
    PlannerSource,
    PlanNode,
)
from app.services.vector_index import FaissVectorIndex, VectorIndexError
from app.services.websocket_notifications import WebSocketNotificationService
from app.settings import Settings, get_settings

API_VERSION = "0.12.0"
logger = logging.getLogger(__name__)

_MAINTENANCE_OPERATION_ERRORS = (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error)


async def _run_isolated_maintenance_operation(
    name: str,
    operation: Callable[[], Awaitable[Any]],
) -> bool:
    """Run one recurring maintenance unit without coupling unrelated units to it."""

    try:
        await operation()
    except _MAINTENANCE_OPERATION_ERRORS:
        logger.exception("distributed runtime maintenance operation failed: %s", name)
        return False
    return True


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
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "allow_once", "deny"]
    user_note: str | None = Field(default=None, max_length=2_000)


class ToolProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any]
    planner_source: Literal["iphone_local", "ubuntu_local", "manual", "test"] = "manual"
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
        "vector_index_path": settings.vector_index_path,
    }.items():
        resolved = protected.resolve()
        if resolved == workspace or workspace in resolved.parents:
            raise RuntimeError(f"{name} must be outside workspace_root")


def create_app(config: Settings | None = None) -> FastAPI:
    settings = config or get_settings()
    _validate_runtime_boundaries(settings)
    embedding_service = (
        HttpEmbeddingService(settings.embedding_base_url, settings.embedding_model)
        if settings.embedding_base_url and settings.embedding_model
        else None
    )
    permission_policy = PermissionPolicy.from_yaml(settings.permissions_path)
    state_service = StateService(
        settings.db_path,
        embedding_service,
        permission_policy=permission_policy,
    )
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
    execution_engine = ExecutionEngine(settings.db_path, settings.workspace_root, permission_policy)
    orchestrator_service = OrchestratorService(settings.llm_base_url, settings.orchestrator_model)
    planner_provider = UbuntuLLMPlannerProvider(orchestrator_service)
    message_board: MessageBoard
    if settings.message_board_backend == "redis":
        message_board = RedisStreamsMessageBoard(
            redis_url=settings.redis_url.get_secret_value(),
            stream_prefix=settings.redis_stream_prefix,
            operation_timeout_seconds=settings.redis_operation_timeout_seconds,
            stream_max_length=settings.redis_stream_maxlen,
            stream_retention_seconds=settings.redis_stream_retention_seconds,
        )
    else:
        message_board = SQLiteMessageBoard(settings.db_path)
    control_plane_instance = ControlPlaneInstanceService(
        settings.db_path,
        version=API_VERSION,
    )
    maintenance_leases = MaintenanceLeaseService(
        settings.db_path,
        lease_seconds=settings.maintenance_lease_seconds,
    )
    maintenance_runner = MaintenanceLeaseRunner(
        maintenance_leases,
        owner_instance_id=control_plane_instance.instance_id,
    )
    agent_dispatcher = AgentDispatcher(
        settings.db_path,
        message_board,
        lease_seconds=settings.agent_lease_seconds,
        max_attempts=settings.agent_job_max_attempts,
        outbox_instance_id=f"{control_plane_instance.instance_id}:dispatcher",
        outbox_publication_lease_seconds=settings.outbox_publication_lease_seconds,
        agent_offline_timeout_seconds=settings.agent_offline_timeout_seconds,
        permission_policy=permission_policy,
    )
    agent_lease_reaper = AgentLeaseReaper(
        settings.db_path,
        message_board,
        maintenance_leases=maintenance_leases,
        owner_instance_id=control_plane_instance.instance_id,
        outbox_instance_id=f"{control_plane_instance.instance_id}:lease-reaper",
        outbox_publication_lease_seconds=settings.outbox_publication_lease_seconds,
        permission_policy=permission_policy,
    )
    iphone_capability_service = IPhoneCapabilityService(
        settings.db_path,
        message_board,
        permission_policy,
        grant_ttl_seconds=settings.iphone_capability_grant_ttl_seconds,
        maintenance_leases=maintenance_leases,
        owner_instance_id=control_plane_instance.instance_id,
        outbox_instance_id=f"{control_plane_instance.instance_id}:iphone",
        outbox_publication_lease_seconds=settings.outbox_publication_lease_seconds,
    )
    feedback_dataset = FeedbackDatasetService(settings.db_path)
    context_builder = ContextBuilder(
        settings.db_path,
        max_tokens=settings.goal_context_max_tokens,
        max_memory_items=settings.goal_context_max_memory_items,
        max_episode_items=settings.goal_context_max_episode_items,
        max_agent_cards=settings.goal_context_max_agent_cards,
        max_upstream_results=settings.goal_context_max_upstream_results,
        max_result_chars_per_node=settings.goal_context_max_result_chars_per_node,
    )
    episode_memory = EpisodeMemoryService(settings.db_path, embedding_service)
    strategy_retrieval = StrategyRetrieval(settings.db_path, episode_memory)
    model_router = ModelRouter(
        [
            ModelRoleConfig(
                role=ModelRole.PLANNER,
                source=PlannerSource.UBUNTU_LOCAL,
                model_id=settings.planner_model or settings.orchestrator_model,
            ),
            ModelRoleConfig(
                role=ModelRole.EVALUATOR,
                source=PlannerSource.UBUNTU_LOCAL,
                model_id=settings.evaluator_model or settings.orchestrator_model,
            ),
            ModelRoleConfig(
                role=ModelRole.SUMMARIZER,
                source=PlannerSource.UBUNTU_LOCAL,
                model_id=settings.summarizer_model or settings.orchestrator_model,
            ),
            ModelRoleConfig(
                role=ModelRole.SYNTHESIZER,
                source=PlannerSource.UBUNTU_LOCAL,
                model_id=settings.synthesizer_model or settings.orchestrator_model,
            ),
        ]
    )
    swarm_planner = UbuntuSwarmPlannerProvider(
        base_url=settings.llm_base_url,
        model=model_router.route_for(ModelRole.PLANNER).model_id,
    )
    evaluator = UbuntuEvaluatorProvider(
        base_url=settings.llm_base_url,
        model=model_router.route_for(ModelRole.EVALUATOR).model_id,
        policy=permission_policy,
    )
    goal_manager = GoalManager(
        settings.db_path,
        state_service=state_service,
        agent_dispatcher=agent_dispatcher,
        planner=swarm_planner,
        evaluator=evaluator,
        permission_policy=permission_policy,
        context_builder=context_builder,
        strategy_retrieval=strategy_retrieval,
        episode_memory=episode_memory,
        result_aggregator=ResultAggregator(settings.db_path),
        default_max_steps=settings.goal_max_steps,
        default_max_parallelism=settings.goal_max_parallelism,
        default_max_replans=settings.goal_max_replans,
        default_max_runtime_seconds=settings.goal_max_runtime_seconds,
        default_max_model_calls=settings.goal_max_model_calls,
        instance_id=control_plane_instance.instance_id,
        model_call_lease_seconds=settings.goal_model_call_lease_seconds,
    )
    idempotency_service = IdempotencyService(settings.db_path)
    consumer_checkpoints = ConsumerCheckpointStore(settings.db_path)
    agent_scoring = AgentScoringService(
        settings.db_path,
        maintenance_leases=maintenance_leases,
        owner_instance_id=control_plane_instance.instance_id,
    )
    vector_projection = (
        FaissVectorIndex(
            settings.vector_index_path,
            generations_to_keep=settings.vector_index_generations_to_keep,
        )
        if settings.vector_backend == "faiss"
        else None
    )
    websocket_notifications = WebSocketNotificationService(
        settings.db_path,
        instance_id=control_plane_instance.instance_id,
    )
    websockets: dict[str, tuple[WebSocket, str, str]] = {}
    pending_websocket_attempts: dict[str, str] = {}

    async def run_agent_lease_reaper(guard: MaintenanceLeaseGuard) -> dict[str, int]:
        return await agent_lease_reaper.reap_expired(maintenance_guard=guard)

    async def run_capability_expirer(guard: MaintenanceLeaseGuard) -> int:
        return await iphone_capability_service.expire_requests(maintenance_guard=guard)

    async def run_outbox_recovery(guard: MaintenanceLeaseGuard) -> int:
        return await agent_dispatcher.outbox.recover_expired_claims(maintenance_guard=guard)

    async def run_outbox_drain(guard: MaintenanceLeaseGuard) -> dict[str, int]:
        return await agent_dispatcher.outbox.drain(maintenance_guard=guard)

    async def run_agent_scoring(guard: MaintenanceLeaseGuard) -> list[Any]:
        return await agent_scoring.rebuild(maintenance_guard=guard)

    async def reload_policy_and_quarantine() -> None:
        await agent_dispatcher.reload_worker_skill_policy(settings.permissions_path)

    async def cleanup_websocket_notifications() -> dict[str, int]:
        return await websocket_notifications.cleanup(
            stale_instance_seconds=settings.websocket_notification_instance_stale_seconds
        )

    async def reconcile_goal_runs(guard: MaintenanceLeaseGuard) -> int:
        return await goal_manager.reconcile(maintenance_guard=guard)

    async def run_distributed_runtime_maintenance_cycle(*, refresh_scores: bool) -> bool:
        """Run one recurring cycle; a known failure cannot starve independent work."""

        await _run_isolated_maintenance_operation(
            "control-plane-heartbeat",
            control_plane_instance.heartbeat,
        )
        await _run_isolated_maintenance_operation(
            "worker-policy-reload",
            reload_policy_and_quarantine,
        )
        await _run_isolated_maintenance_operation(
            "websocket-notification-cleanup",
            cleanup_websocket_notifications,
        )
        await _run_isolated_maintenance_operation(
            "agent-lease-reaper",
            lambda: maintenance_runner.run("agent-lease-reaper", run_agent_lease_reaper),
        )
        await _run_isolated_maintenance_operation(
            "capability-expirer",
            lambda: maintenance_runner.run("capability-expirer", run_capability_expirer),
        )
        await _run_isolated_maintenance_operation(
            "outbox-recovery",
            lambda: maintenance_runner.run("outbox-maintenance", run_outbox_recovery),
        )
        await _run_isolated_maintenance_operation(
            "outbox-drain",
            lambda: maintenance_runner.run("outbox-maintenance", run_outbox_drain),
        )
        await _run_isolated_maintenance_operation(
            "goal-runtime",
            lambda: maintenance_runner.run("goal-runtime", reconcile_goal_runs),
        )
        if not refresh_scores:
            return False
        return await _run_isolated_maintenance_operation(
            "agent-scoring",
            lambda: maintenance_runner.run("feedback-maintenance", run_agent_scoring),
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        maintenance: asyncio.Task[None] | None = None
        websocket_notification_pump: asyncio.Task[None] | None = None
        instance_started = False
        try:
            await state_service.initialize()
            await goal_manager.initialize()
            await consumer_checkpoints.initialize()
            await agent_scoring.initialize()
            await control_plane_instance.start()
            instance_started = True
            await websocket_notifications.initialize()
            await cleanup_websocket_notifications()
            settings.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            await agent_dispatcher.reload_worker_skill_policy(settings.permissions_path)
            await maintenance_runner.run(
                "agent-lease-reaper",
                run_agent_lease_reaper,
            )
            await maintenance_runner.run("capability-expirer", run_capability_expirer)
            await maintenance_runner.run("outbox-maintenance", run_outbox_recovery)
            await maintenance_runner.run("outbox-maintenance", run_outbox_drain)
            await maintenance_runner.run("feedback-maintenance", run_agent_scoring)
            await maintenance_runner.run("goal-runtime", reconcile_goal_runs)

            async def maintain_distributed_runtime() -> None:
                last_score_refresh = asyncio.get_running_loop().time()
                while True:
                    await asyncio.sleep(
                        min(
                            settings.agent_heartbeat_seconds,
                            settings.control_plane_heartbeat_seconds,
                        )
                    )
                    current_time = asyncio.get_running_loop().time()
                    refresh_scores = (
                        current_time - last_score_refresh >= settings.agent_score_refresh_seconds
                    )
                    if await run_distributed_runtime_maintenance_cycle(
                        refresh_scores=refresh_scores
                    ):
                        last_score_refresh = current_time

            maintenance = asyncio.create_task(
                maintain_distributed_runtime(), name="mongars-distributed-runtime-maintenance"
            )

            async def pump_websocket_notifications() -> None:
                while True:
                    await _run_isolated_maintenance_operation(
                        "websocket-notification-pump",
                        drain_websocket_notifications,
                    )
                    await asyncio.sleep(settings.websocket_notification_poll_seconds)

            websocket_notification_pump = asyncio.create_task(
                pump_websocket_notifications(),
                name="mongars-websocket-notification-pump",
            )
            yield
        finally:
            if websocket_notification_pump is not None:
                websocket_notification_pump.cancel()
                with suppress(asyncio.CancelledError):
                    await websocket_notification_pump
            if maintenance is not None:
                maintenance.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance
            await websocket_notifications.close()
            await maintenance_runner.close()
            if instance_started:
                with suppress(OSError, RuntimeError, sqlite3.Error):
                    await control_plane_instance.stop()
            await message_board.close()

    app = FastAPI(title="monGARS Control Plane", version=API_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.state_service = state_service
    app.state.auth_service = auth_service
    app.state.approval_gateway = approval_gateway
    app.state.execution_engine = execution_engine
    app.state.orchestrator_service = orchestrator_service
    app.state.planner_provider = planner_provider
    app.state.message_board = message_board
    app.state.agent_dispatcher = agent_dispatcher
    app.state.agent_lease_reaper = agent_lease_reaper
    app.state.iphone_capability_service = iphone_capability_service
    app.state.control_plane_instance = control_plane_instance
    app.state.maintenance_leases = maintenance_leases
    app.state.maintenance_lease_runner = maintenance_runner
    app.state.idempotency_service = idempotency_service
    app.state.consumer_checkpoints = consumer_checkpoints
    app.state.agent_scoring = agent_scoring
    app.state.feedback_dataset = feedback_dataset
    app.state.context_builder = context_builder
    app.state.episode_memory = episode_memory
    app.state.strategy_retrieval = strategy_retrieval
    app.state.model_router = model_router
    app.state.swarm_planner = swarm_planner
    app.state.evaluator = evaluator
    app.state.goal_manager = goal_manager
    app.state.vector_projection = vector_projection
    app.state.websocket_notifications = websocket_notifications
    app.state.run_maintenance_cycle = run_distributed_runtime_maintenance_cycle
    app.state.websockets = websockets

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

    async def require_agent(
        agent_id: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        await require_secure_transport(request)
        principal = await agent_dispatcher.authenticate(
            agent_id, _bearer_token(authorization) or ""
        )
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid agent credential")
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

    async def close_websocket_bounded(websocket: WebSocket, *, code: int) -> None:
        try:
            await asyncio.wait_for(
                websocket.close(code=code),
                timeout=settings.websocket_io_timeout_seconds,
            )
        except (TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
            # Registry eviction is authoritative. Closing a broken peer remains
            # best-effort and must never hold credential cutover indefinitely.
            return

    async def clear_websocket_owner(
        device_id: str,
        session_id: str,
        connection_id: str,
    ) -> None:
        async with auth_service.serialize_device_session(device_id):
            await auth_service.clear_websocket_connection(
                device_id,
                session_id,
                connection_id,
            )

    def begin_websocket_attempt(device_id: str) -> str:
        attempt_id = secrets.token_urlsafe(18)
        pending_websocket_attempts[device_id] = attempt_id
        return attempt_id

    async def install_websocket(
        websocket: WebSocket,
        *,
        device_id: str,
        session_id: str,
        attempt_id: str,
    ) -> bool:
        close_after: list[tuple[WebSocket, int]] = []
        installed = False
        try:
            async with auth_service.serialize_device_session(device_id):
                if pending_websocket_attempts.get(device_id) != attempt_id:
                    close_after.append((websocket, 4401))
                else:
                    pending_websocket_attempts.pop(device_id, None)
                    if not await auth_service.activate_websocket_connection(
                        device_id,
                        session_id,
                        attempt_id,
                    ):
                        close_after.append((websocket, 4401))
                    else:
                        previous = websockets.get(device_id)
                        if previous is not None and previous[0] is not websocket:
                            close_after.append((previous[0], 1000))
                        websockets[device_id] = (websocket, session_id, attempt_id)
                        try:
                            await asyncio.wait_for(
                                websocket.send_json(
                                    {
                                        "type": "connected",
                                        "payload": {
                                            "version": API_VERSION,
                                            "device_id": device_id,
                                        },
                                    }
                                ),
                                timeout=settings.websocket_io_timeout_seconds,
                            )
                            pending_capabilities = (
                                await iphone_capability_service.pending_delivery_previews(device_id)
                            )
                            for preview in pending_capabilities:
                                await asyncio.wait_for(
                                    websocket.send_json(
                                        safe_websocket_event(
                                            {
                                                "type": "iphone.capability.requested",
                                                "payload": preview,
                                            }
                                        )
                                    ),
                                    timeout=settings.websocket_io_timeout_seconds,
                                )
                        except (
                            TimeoutError,
                            OSError,
                            RuntimeError,
                            sqlite3.Error,
                            WebSocketDisconnect,
                        ):
                            await auth_service.clear_websocket_connection(
                                device_id,
                                session_id,
                                attempt_id,
                            )
                            if websockets.get(device_id) == (
                                websocket,
                                session_id,
                                attempt_id,
                            ):
                                websockets.pop(device_id, None)
                            close_after.append((websocket, 1011))
                        else:
                            installed = True
        except (OSError, RuntimeError, sqlite3.Error):
            if pending_websocket_attempts.get(device_id) == attempt_id:
                pending_websocket_attempts.pop(device_id, None)
            close_after.append((websocket, 1011))

        if close_after:
            await asyncio.gather(
                *(close_websocket_bounded(peer, code=code) for peer, code in close_after)
            )
        return installed

    app.state.begin_websocket_attempt = begin_websocket_attempt
    app.state.install_websocket = install_websocket

    async def deliver_websocket_notification_to_device(
        connected_device_id: str,
        websocket: WebSocket,
        connected_session_id: str,
        connected_connection_id: str,
        safe_event: dict[str, Any],
    ) -> None:
        expected = (websocket, connected_session_id, connected_connection_id)
        close_code: int | None = None
        try:
            async with auth_service.serialize_device_session(connected_device_id):
                if websockets.get(connected_device_id) != expected:
                    return
                if not await auth_service.is_websocket_connection_current(
                    connected_device_id,
                    connected_session_id,
                    connected_connection_id,
                ):
                    close_code = 4401
                else:
                    try:
                        await asyncio.wait_for(
                            websocket.send_json(safe_event),
                            timeout=settings.websocket_io_timeout_seconds,
                        )
                    except (TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
                        close_code = 1011
                if close_code is not None and websockets.get(connected_device_id) == expected:
                    websockets.pop(connected_device_id, None)
                    await auth_service.clear_websocket_connection(
                        connected_device_id,
                        connected_session_id,
                        connected_connection_id,
                    )
        except (OSError, RuntimeError):
            close_code = 1011
            if websockets.get(connected_device_id) == expected:
                websockets.pop(connected_device_id, None)
            with suppress(OSError, RuntimeError, sqlite3.Error):
                await clear_websocket_owner(
                    connected_device_id,
                    connected_session_id,
                    connected_connection_id,
                )
        if close_code is not None:
            await close_websocket_bounded(websocket, code=close_code)

    async def deliver_websocket_notification_locally(
        safe_event: dict[str, Any],
        device_id: str | None,
    ) -> None:
        deliveries = [
            deliver_websocket_notification_to_device(
                connected_device_id,
                websocket,
                connected_session_id,
                connected_connection_id,
                safe_event,
            )
            for connected_device_id, (
                websocket,
                connected_session_id,
                connected_connection_id,
            ) in list(websockets.items())
            if device_id is None or connected_device_id == device_id
        ]
        if deliveries:
            # Each device has an independent cross-process lock and I/O timeout.
            # Concurrent fan-out makes one stalled phone cost one timeout rather
            # than serially delaying every other connected phone.
            await asyncio.gather(*deliveries)

    async def drain_websocket_notifications() -> int:
        return await websocket_notifications.drain(deliver_websocket_notification_locally)

    async def broadcast(event: dict[str, Any], *, device_id: str | None = None) -> None:
        safe_event = safe_websocket_event(event)
        try:
            await websocket_notifications.publish(safe_event, device_id=device_id)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            # WebSocket delivery is an invalidation hint, never authoritative
            # state. Preserve the endpoint's post-commit response and attempt
            # same-process delivery when the notification log is unavailable.
            logger.exception("could not persist WebSocket invalidation")
            await deliver_websocket_notification_locally(safe_event, device_id)
            return
        try:
            await drain_websocket_notifications()
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            # The committed notification remains after this instance's
            # checkpoint. The local/background pump will retry it at least once.
            logger.exception("could not drain WebSocket invalidations")

    app.state.broadcast = broadcast
    app.state.drain_websocket_notifications = drain_websocket_notifications

    def public_job_event(job: dict[str, Any]) -> dict[str, Any]:
        return {
            key: job.get(key)
            for key in (
                "id",
                "task_id",
                "required_skill",
                "status",
                "claimed_by",
                "created_at",
                "updated_at",
                "claimed_at",
                "heartbeat_at",
                "completed_at",
                "lease_expires_at",
                "lease_generation",
                "attempt_count",
                "max_attempts",
                "last_agent_id",
                "last_failure_reason",
            )
        }

    async def broadcast_goal_detail(detail: dict[str, Any]) -> None:
        """Broadcast safe projections as invalidation/state hints for the phone replica."""

        goal = detail.get("goal")
        if isinstance(goal, dict):
            await broadcast({"type": "goal.updated", "payload": goal})
        nodes = detail.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, dict):
                    await broadcast({"type": "plan.node.updated", "payload": node})
        result = detail.get("result")
        if isinstance(result, dict):
            await broadcast({"type": "goal.result.updated", "payload": result})

    def goal_conflict_http_exception(exc: GoalManagerConflict) -> HTTPException:
        detail = str(exc)
        if detail == "goal not found":
            return HTTPException(status_code=404, detail=detail)
        if "unavailable" in detail:
            return HTTPException(status_code=503, detail=detail)
        return HTTPException(status_code=409, detail=detail)

    async def run_goal_hook(
        name: str,
        operation: Callable[[], Awaitable[dict[str, Any] | None]],
    ) -> dict[str, Any] | None:
        """Keep post-commit goal projections recoverable without falsifying worker outcomes."""

        try:
            return await operation()
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            logger.exception("goal coordination hook failed: %s", name)
            return None

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
            "planner.proposal.accepted",
            {"planner_source": request.planner_source, "tool_call_id": record["id"]},
            task_id=task_id,
            trace_id=task_id,
        )
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
            proposal = await planner_provider.plan(task.input, task.mode.value)
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
            {
                "tool_name": proposal["tool_name"],
                "model": settings.orchestrator_model,
                "planner_source": planner_provider.source,
            },
            task_id=task_id,
            trace_id=task_id,
        )
        public_proposal = {
            **proposal,
            "arguments": public_tool_arguments(proposal["tool_name"], proposal["arguments"]),
            "planner_source": planner_provider.source,
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
                planner_source="ubuntu_local",
                summary=public_tool_summary(proposal["tool_name"]),
            ),
            principal,
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", service="mongars-control-plane", version=API_VERSION)

    @app.get("/status")
    async def runtime_status(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        del principal
        job_metrics = await agent_lease_reaper.metrics()
        outbox_metrics = await agent_dispatcher.outbox.metrics()
        board_health = await message_board.health()
        agents = await state_service.list_agents()
        liveness_now = datetime.now(UTC)
        active_agents = sum(
            1
            for agent in agents
            if agent_counts_as_active(
                agent,
                now=liveness_now,
                timeout_seconds=settings.agent_offline_timeout_seconds,
            )
        )
        vector_generation_age_seconds: float | None = None
        active_vector_projection = app.state.vector_projection
        if active_vector_projection is not None:
            try:
                vector_generation_age_seconds = await asyncio.to_thread(
                    active_vector_projection.generation_age_seconds
                )
            except (OSError, VectorIndexError):
                vector_generation_age_seconds = None
        return {
            "status": "ok",
            "version": API_VERSION,
            "instance_id": control_plane_instance.instance_id,
            "message_board_backend": str(board_health.get("backend", "unknown")),
            "message_board_health": str(board_health.get("status", "degraded")),
            "last_successful_publication": board_health.get("last_successful_publication"),
            "redis_reconnect_count": int(board_health.get("reconnect_count", 0)),
            "redis_last_error_category": board_health.get("last_error_category"),
            **outbox_metrics,
            **job_metrics,
            **maintenance_runner.metrics,
            "active_agents": active_agents,
            "offline_agents": len(agents) - active_agents,
            "maintenance_lease_owner": await maintenance_leases.current_owners(),
            "pending_capability_requests": await iphone_capability_service.pending_count(),
            "vector_backend": settings.vector_backend,
            "vector_generation_age_seconds": vector_generation_age_seconds,
            **(await goal_manager.status_counts()),
        }

    @app.post(
        "/goals",
        status_code=status.HTTP_201_CREATED,
        response_model=GoalDetail,
    )
    async def create_goal(
        request: GoalCreateRequest,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        goal = await goal_manager.create_goal(request, actor_id=str(principal["id"]))
        detail = await goal_manager.get_goal(str(goal["id"]))
        if detail is None:
            raise HTTPException(status_code=500, detail="created goal is unavailable")
        await broadcast_goal_detail(detail)
        return detail

    @app.get("/goals", response_model=list[GoalRecord])
    async def list_goals(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> list[dict[str, Any]]:
        del principal
        return await goal_manager.list_goals(limit=limit)

    @app.get("/goals/{goal_id}", response_model=GoalDetail)
    async def get_goal(
        goal_id: str,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        del principal
        detail = await goal_manager.get_goal(goal_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="goal not found")
        return detail

    @app.post("/goals/{goal_id}/start", response_model=GoalDetail)
    async def start_goal(
        goal_id: str,
        request: GoalStartRequest,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        del principal
        try:
            detail = await goal_manager.start_goal(goal_id, request)
        except GoalManagerConflict as exc:
            raise goal_conflict_http_exception(exc) from exc
        await broadcast_goal_detail(detail)
        return detail

    @app.post("/goals/{goal_id}/cancel", response_model=GoalDetail)
    async def cancel_goal(
        goal_id: str,
        request: GoalCancelRequest,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        del request
        try:
            detail = await goal_manager.cancel_goal(goal_id, actor_id=str(principal["id"]))
        except GoalManagerConflict as exc:
            raise goal_conflict_http_exception(exc) from exc
        await broadcast_goal_detail(detail)
        return detail

    @app.post("/goals/{goal_id}/replan", response_model=GoalDetail)
    async def replan_goal(
        goal_id: str,
        request: GoalReplanRequest,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        del principal
        try:
            detail = await goal_manager.replan_goal(goal_id, request)
        except GoalManagerConflict as exc:
            raise goal_conflict_http_exception(exc) from exc
        await broadcast_goal_detail(detail)
        return detail

    @app.get("/goals/{goal_id}/nodes", response_model=list[PlanNode])
    async def list_goal_nodes(
        goal_id: str,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> list[dict[str, Any]]:
        del principal
        detail = await goal_manager.get_goal(goal_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="goal not found")
        nodes = detail["nodes"]
        if not isinstance(nodes, list):
            raise HTTPException(status_code=500, detail="goal node projection is invalid")
        return nodes

    @app.get("/goals/{goal_id}/result", response_model=GoalResult | None)
    async def get_goal_result(
        goal_id: str,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any] | None:
        del principal
        detail = await goal_manager.get_goal(goal_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="goal not found")
        result = detail["result"]
        return result if isinstance(result, dict) else None

    @app.post(
        "/goals/{goal_id}/feedback",
        status_code=status.HTTP_201_CREATED,
        response_model=GoalFeedbackRecord,
    )
    async def create_goal_feedback(
        goal_id: str,
        request: GoalFeedbackRequest,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        try:
            return await goal_manager.add_feedback(
                goal_id,
                request,
                actor_id=str(principal["id"]),
            )
        except GoalManagerConflict as exc:
            raise goal_conflict_http_exception(exc) from exc

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
        if request.start_task:
            conversation_id, task = await state_service.create_chat_task(
                request.content, request.conversation_id, request.mode.value, str(principal["id"])
            )
            await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
            return {"conversation_id": conversation_id, "task": task.model_dump(mode="json")}

        conversation_id, user_message = await state_service.append_chat_user_message(
            request.content, request.conversation_id, str(principal["id"])
        )
        await broadcast({"type": "message.created", "payload": user_message})
        history = await state_service.list_messages(conversation_id, 40)
        try:
            reply = await orchestrator_service.chat(
                [
                    {
                        "role": "assistant" if item["role"] in {"agent", "assistant"} else "user",
                        "content": item["content"],
                    }
                    for item in history
                    if item["role"] in {"user", "agent", "assistant"}
                ]
            )
        except OrchestratorError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        assistant_message = await state_service.append_conversation_message(
            conversation_id, "agent", reply, agent_id="local-orchestrator"
        )
        await broadcast({"type": "message.created", "payload": assistant_message})
        return {"conversation_id": conversation_id, "task": None, "message": assistant_message}

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
        try:
            validate_agent_registration(request)
        except AgentCardPolicyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            return await state_service.register_agent(request, str(principal["id"]))
        except PermissionPolicyError as exc:
            raise HTTPException(
                status_code=403, detail="remote worker skill is denied by policy"
            ) from exc

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

    @app.post("/tasks/{task_id}/dispatch", status_code=status.HTTP_201_CREATED)
    async def dispatch_agent_job(
        task_id: str,
        request: AgentJobDispatch,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        del principal
        try:
            validated_payload = validate_remote_job(request.required_skill, request.payload)
            if request.required_skill in {"workspace.list_dir", "workspace.read_text"}:
                execution_engine.validate_arguments(
                    request.required_skill,
                    {"path": validated_payload["path"]},
                )
                if permission_policy.evaluate_tool(request.required_skill).decision != "allow":
                    raise RemoteJobPolicyError("workspace worker skill is not allowed by policy")
        except (ExecutionError, RemoteJobPolicyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid remote worker payload") from exc
        try:
            job = await agent_dispatcher.queue_job(
                task_id, request.required_skill, validated_payload
            )
        except AgentDispatchConflict as exc:
            if str(exc) == "task not found":
                code = 404
            elif str(exc) == "remote worker skill is denied by policy":
                code = 403
            else:
                code = 409
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        await broadcast({"type": "agent.job.queued", "payload": public_job_event(job)})
        task = await state_service.get_task(task_id)
        if task:
            await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
        return job

    @app.post("/agents/{agent_id}/claim")
    async def claim_agent_job(
        agent_id: str,
        request: AgentJobClaim,
        principal: Annotated[dict[str, Any], Depends(require_agent)],
    ) -> dict[str, Any] | None:
        del request, principal
        try:
            job = await agent_dispatcher.claim(agent_id)
        except AgentDispatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if job:
            await broadcast({"type": "agent.job.claimed", "payload": public_job_event(job)})
            goal_detail = await run_goal_hook(
                "job-claimed", lambda: goal_manager.on_job_claimed(job)
            )
            if goal_detail is not None:
                await broadcast_goal_detail(goal_detail)
        return job

    @app.post("/agents/{agent_id}/jobs/{job_id}/heartbeat")
    async def heartbeat_agent_job(
        agent_id: str,
        job_id: str,
        request: AgentJobHeartbeat,
        principal: Annotated[dict[str, Any], Depends(require_agent)],
    ) -> dict[str, Any]:
        del principal
        try:
            return await agent_dispatcher.heartbeat(
                agent_id,
                job_id,
                request.claim_token,
                lease_id=request.lease_id,
                lease_generation=request.lease_generation,
            )
        except AgentDispatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/agents/{agent_id}/jobs/{job_id}/result")
    async def submit_agent_job_result(
        agent_id: str,
        job_id: str,
        request: AgentJobResult,
        principal: Annotated[dict[str, Any], Depends(require_agent)],
    ) -> dict[str, Any]:
        del principal
        try:
            job, changed = await agent_dispatcher.submit_result(
                agent_id,
                job_id,
                request.claim_token,
                status=request.status,
                result=request.result,
                error=request.error,
                lease_id=request.lease_id,
                lease_generation=request.lease_generation,
            )
        except AgentDispatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if changed:
            await broadcast(
                {"type": f"agent.job.{request.status}", "payload": public_job_event(job)}
            )
            task = await state_service.get_task(str(job["task_id"]))
            if task:
                await broadcast({"type": "task.updated", "payload": task.model_dump(mode="json")})
            goal_detail = await run_goal_hook("job-result", lambda: goal_manager.on_job_result(job))
            if goal_detail is not None:
                await broadcast_goal_detail(goal_detail)
        return {**job, "idempotent_replay": not changed}

    @app.get("/agents/{agent_id}/jobs")
    async def list_agent_jobs(
        agent_id: str,
        principal: Annotated[dict[str, Any], Depends(require_agent)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        del principal
        return await agent_dispatcher.list_jobs(agent_id, limit=limit)

    @app.post(
        "/agents/{agent_id}/jobs/{job_id}/capability-requests",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_agent_capability_request(
        agent_id: str,
        job_id: str,
        request: AgentCapabilityRequest,
        principal: Annotated[dict[str, Any], Depends(require_agent)],
    ) -> dict[str, Any]:
        del principal
        try:
            record = await iphone_capability_service.create_request(
                agent_id=agent_id,
                job_id=job_id,
                claim_token=request.claim_token,
                lease_id=request.lease_id,
                lease_generation=request.lease_generation,
                capability_name=request.capability_name,
                arguments=request.arguments,
            )
        except IPhoneCapabilityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        goal_detail = await run_goal_hook(
            "capability-requested", lambda: goal_manager.on_capability_requested(job_id)
        )
        if goal_detail is not None:
            await broadcast_goal_detail(goal_detail)
        device_id = await iphone_capability_service.device_for_request(str(record["request_id"]))
        if device_id is not None:
            await broadcast(
                {
                    "type": "iphone.capability.requested",
                    "payload": {
                        "request_id": record["request_id"],
                        "capability_name": record["capability"],
                        "expires_at": record["expires_at"],
                        "preview": {"arguments_redacted": True},
                    },
                },
                device_id=device_id,
            )
        return record

    @app.post("/agents/{agent_id}/jobs/{job_id}/capability-requests/{request_id}/poll")
    async def poll_agent_capability_request(
        agent_id: str,
        job_id: str,
        request_id: str,
        request: AgentCapabilityPoll,
        principal: Annotated[dict[str, Any], Depends(require_agent)],
    ) -> dict[str, Any]:
        del principal
        try:
            return await iphone_capability_service.poll_for_worker(
                request_id,
                agent_id=agent_id,
                job_id=job_id,
                claim_token=request.claim_token,
                lease_id=request.lease_id,
                lease_generation=request.lease_generation,
            )
        except IPhoneCapabilityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/iphone/capabilities/requests")
    async def list_iphone_capability_requests(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> list[dict[str, Any]]:
        return await iphone_capability_service.list_for_device(str(principal["id"]), limit=limit)

    @app.get("/iphone/capabilities/requests/{request_id}")
    async def get_iphone_capability_request(
        request_id: str,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        record = await iphone_capability_service.get_request_for_device(
            request_id, str(principal["id"])
        )
        if record is None:
            raise HTTPException(status_code=404, detail="capability request not found")
        return record

    @app.post("/iphone/capabilities/requests/{request_id}/authorize")
    async def authorize_iphone_capability_request(
        request_id: str,
        request: IPhoneCapabilityDecision,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        try:
            record = await iphone_capability_service.authorize(
                request_id,
                str(principal["id"]),
                decision=request.decision,
                user_note=request.user_note,
            )
        except IPhoneCapabilityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await broadcast(
            {
                "type": "iphone.capability.updated",
                "payload": {"request_id": request_id},
            },
            device_id=str(principal["id"]),
        )
        return record

    @app.post("/iphone/capabilities/requests/{request_id}/execute")
    async def consume_iphone_capability_grant(
        request_id: str,
        request: IPhoneCapabilityExecute,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        try:
            return await iphone_capability_service.consume(
                request_id,
                str(principal["id"]),
                grant_id=request.grant_id,
                action_digest=request.action_digest,
            )
        except IPhoneCapabilityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/iphone/capabilities/requests/{request_id}/result")
    async def submit_iphone_capability_result(
        request_id: str,
        request: IPhoneCapabilityResultSubmit,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        job_id = await iphone_capability_service.job_for_request(request_id)
        try:
            receipt = await iphone_capability_service.submit_result(
                request_id,
                str(principal["id"]),
                grant_id=request.grant_id,
                action_digest=request.action_digest,
                result=request.result.model_dump(
                    mode="json",
                    exclude={"reason"} if request.result.reason is None else set(),
                ),
            )
        except IPhoneCapabilityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await broadcast(
            {
                "type": "iphone.capability.updated",
                "payload": {"request_id": request_id},
            },
            device_id=str(principal["id"]),
        )
        if job_id is not None:
            goal_detail = await run_goal_hook(
                "capability-resolved", lambda: goal_manager.on_capability_resolved(job_id)
            )
            if goal_detail is not None:
                await broadcast_goal_detail(goal_detail)
        return receipt

    @app.get("/audit")
    @app.get("/sync/audit", include_in_schema=False)
    async def list_audit(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        after_id: Annotated[int | None, Query(ge=0)] = None,
    ) -> list[dict[str, Any]]:
        del principal
        return await state_service.list_audit(limit, after_id)

    @app.post("/sync/mutations", status_code=status.HTTP_201_CREATED)
    async def apply_idempotent_mobile_mutation(
        request: SafeMutationRequest,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=20, max_length=200),
        ],
    ) -> dict[str, Any]:
        try:
            receipt = await idempotency_service.apply(
                actor_id=str(principal["id"]),
                idempotency_key=idempotency_key,
                request=request,
            )
        except IdempotencyConflict as exc:
            code = (
                404
                if str(exc) in {"task not found", "agent not found", "memory not found"}
                else 409
            )
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        if not bool(receipt["idempotent_replay"]):
            result = receipt["result"]
            if request.operation == "memory.metadata.update":
                await broadcast({"type": "memory.updated", "payload": result})
            elif request.operation == "chat.message.create" and isinstance(result, dict):
                message = result.get("message")
                if isinstance(message, dict):
                    await broadcast({"type": "message.created", "payload": message})
        return receipt

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

    @app.post("/feedback/correction", status_code=status.HTTP_201_CREATED)
    async def create_feedback_correction(
        request: FeedbackCorrection,
        principal: Annotated[DevicePrincipal, Depends(require_device)],
    ) -> dict[str, Any]:
        try:
            return await feedback_dataset.add_correction(request, actor_id=str(principal["id"]))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/feedback/dataset/export")
    async def export_feedback_dataset(
        principal: Annotated[DevicePrincipal, Depends(require_device)],
        dataset: Literal["task", "planner", "evaluator", "synthesis", "routing"] = "task",
        minimum_score: Annotated[float, Query(ge=0, le=5)] = 0,
        successful_only: bool = False,
        reviewed_only: bool = False,
        planner_source: Literal["iphone_local", "ubuntu_local", "manual", "test"] | None = None,
    ) -> dict[str, str]:
        del principal
        if dataset == "task":
            data = await feedback_dataset.export_jsonl()
        else:
            data = await feedback_dataset.export_goal_jsonl(
                dataset_type=dataset,
                minimum_score=minimum_score,
                successful_only=successful_only,
                reviewed_only=reviewed_only,
                planner_source=planner_source,
            )
        return {"format": "jsonl", "dataset": dataset, "data": data}

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
        connected_device_id = str(principal["device_id"])
        connected_session_id = str(principal["session_id"])
        attempt_id = begin_websocket_attempt(connected_device_id)
        try:
            await websocket.accept()
            if not await install_websocket(
                websocket,
                device_id=connected_device_id,
                session_id=connected_session_id,
                attempt_id=attempt_id,
            ):
                return
            while True:
                await websocket.receive_text()
                async with auth_service.serialize_device_session(connected_device_id):
                    connection_is_current = await auth_service.is_websocket_connection_current(
                        connected_device_id,
                        connected_session_id,
                        attempt_id,
                    )
                if not connection_is_current:
                    if websockets.get(connected_device_id) == (
                        websocket,
                        connected_session_id,
                        attempt_id,
                    ):
                        websockets.pop(connected_device_id, None)
                    await clear_websocket_owner(
                        connected_device_id,
                        connected_session_id,
                        attempt_id,
                    )
                    await close_websocket_bounded(websocket, code=4401)
                    break
        except (TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
            if websockets.get(connected_device_id) == (
                websocket,
                connected_session_id,
                attempt_id,
            ):
                websockets.pop(connected_device_id, None)
            await clear_websocket_owner(
                connected_device_id,
                connected_session_id,
                attempt_id,
            )
            await close_websocket_bounded(websocket, code=1011)
        finally:
            if pending_websocket_attempts.get(connected_device_id) == attempt_id:
                pending_websocket_attempts.pop(connected_device_id, None)

    return app


app = create_app()
