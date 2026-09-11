from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from app.models import (
    AgentCreate,
    FeedbackCreate,
    MemoryCreate,
    MemorySearch,
    MemoryUpdate,
    TaskCreate,
    TaskMode,
    TaskRecord,
)
from app.services.agent_card import public_agent_card, validate_agent_registration
from app.services.approval_binding import (
    PUBLIC_PROCESS_ERROR,
    ApprovalBindingError,
    canonical_action_digest,
    public_tool_call,
)
from app.services.audit_log import append_audit_event
from app.services.distributed_state import (
    AgentJobStateMachine,
    DistributedStateConflict,
    TaskStateMachine,
)
from app.services.embedding_service import EmbeddingService, EmbeddingServiceError
from app.services.goal_state import public_goal, public_goal_result, public_plan_node
from app.services.iphone_capability_binding import (
    CapabilityRequestBindingError,
    canonical_capability_request_fingerprint,
)
from app.services.maintenance_lease import MaintenanceLeaseGuard
from app.services.outbox import OutboxService
from app.services.permission_policy import PermissionPolicy, PermissionPolicyError
from app.services.worker_skill_policy import WorkerSkillPolicyStore

SCHEMA_VERSION = 23
PUBLIC_ERROR_AUDIT_EVENTS = frozenset({"tool.failed", "tool.execution_rejected"})

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"planned", "failed"}),
    "planned": frozenset({"planned", "failed"}),
    "waiting_permission": frozenset(),
    "queued": frozenset(),
    "running": frozenset(),
    "blocked": frozenset(),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    input TEXT NOT NULL,
    mode TEXT NOT NULL,
    source TEXT NOT NULL,
    conversation_id TEXT,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    error_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    request_audit_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decision_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_id TEXT,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_task_id ON tool_calls(task_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_approval_id ON tool_calls(approval_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_calls_unique_approval
    ON tool_calls(approval_id) WHERE approval_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS pairing_codes (
    code TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    attempts_remaining INTEGER NOT NULL DEFAULT 10,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    last_pairing_id TEXT,
    websocket_connection_id TEXT
);
CREATE TABLE IF NOT EXISTS pairing_candidates (
    pairing_id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    device_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    finalized_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pairing_candidates_device
    ON pairing_candidates(device_id);
CREATE TABLE IF NOT EXISTS websocket_tickets (
    ticket_hash TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS websocket_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    device_id TEXT,
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_websocket_notifications_id
    ON websocket_notifications(id);
CREATE TABLE IF NOT EXISTS websocket_notification_checkpoints (
    instance_id TEXT PRIMARY KEY,
    last_notification_id INTEGER NOT NULL CHECK(last_notification_id >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    task_id TEXT,
    role TEXT NOT NULL,
    agent_id TEXT,
    content TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, created_at);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    model_id TEXT,
    status TEXT NOT NULL,
    skills_json TEXT NOT NULL,
    auth_token_hash TEXT,
    last_heartbeat_at TEXT,
    last_seen_at TEXT,
    max_concurrency INTEGER NOT NULL DEFAULT 1,
    capacity_json TEXT NOT NULL DEFAULT '{}',
    runtime TEXT NOT NULL DEFAULT 'python',
    supported_protocol_version TEXT NOT NULL DEFAULT 'mongars-worker-v0.9',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    confidence REAL NOT NULL DEFAULT 1.0,
    pinned INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_scope
    ON memory_items(scope, kind, pinned, updated_at);
CREATE TABLE IF NOT EXISTS feedback_events (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    agent_id TEXT,
    type TEXT NOT NULL,
    label TEXT,
    score REAL,
    notes TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_task ON feedback_events(task_id, created_at);
CREATE TABLE IF NOT EXISTS idempotency_receipts (
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY(actor_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_receipts_created
    ON idempotency_receipts(created_at);
CREATE TABLE IF NOT EXISTS message_board_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    agent_id TEXT,
    task_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dedupe_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_message_board_topic
    ON message_board_events(topic, id);
CREATE TABLE IF NOT EXISTS outbox_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    message_id TEXT NOT NULL,
    task_id TEXT,
    agent_id TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    publishing_owner TEXT,
    publishing_started_at TEXT,
    publishing_lease_expires_at TEXT,
    publish_generation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox_events(published_at, id);
CREATE TABLE IF NOT EXISTS outbox_operational_metrics (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    claim_expirations INTEGER NOT NULL DEFAULT 0 CHECK(claim_expirations >= 0),
    duplicate_publications INTEGER NOT NULL DEFAULT 0 CHECK(duplicate_publications >= 0),
    publish_latency_ms_count INTEGER NOT NULL DEFAULT 0 CHECK(publish_latency_ms_count >= 0),
    publish_latency_ms_total INTEGER NOT NULL DEFAULT 0 CHECK(publish_latency_ms_total >= 0),
    publish_latency_ms_max INTEGER NOT NULL DEFAULT 0 CHECK(publish_latency_ms_max >= 0),
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_job_operational_metrics (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    lease_expirations INTEGER NOT NULL DEFAULT 0 CHECK(lease_expirations >= 0),
    retries INTEGER NOT NULL DEFAULT 0 CHECK(retries >= 0),
    dead_letter_events INTEGER NOT NULL DEFAULT 0 CHECK(dead_letter_events >= 0),
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS control_plane_instances (
    instance_id TEXT PRIMARY KEY,
    hostname_label TEXT NOT NULL,
    version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    stopped_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_control_plane_instances_heartbeat
    ON control_plane_instances(stopped_at, heartbeat_at);
CREATE TABLE IF NOT EXISTS maintenance_leases (
    name TEXT PRIMARY KEY,
    owner_instance_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 1),
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(owner_instance_id) REFERENCES control_plane_instances(instance_id)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_leases_expiry
    ON maintenance_leases(expires_at, name);
CREATE TABLE IF NOT EXISTS worker_skill_policy_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    epoch INTEGER NOT NULL CHECK(epoch >= 1),
    rules_json TEXT NOT NULL,
    rules_digest TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_consumer_deliveries (
    consumer_group TEXT NOT NULL,
    event_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    source_cursor TEXT,
    envelope_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','processing','completed','dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    claim_owner TEXT,
    claim_generation INTEGER NOT NULL DEFAULT 0 CHECK(claim_generation >= 0),
    claim_started_at TEXT,
    claim_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    dead_lettered_at TEXT,
    PRIMARY KEY(consumer_group, event_id),
    UNIQUE(consumer_group, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_message_consumer_claimable
    ON message_consumer_deliveries(
        consumer_group,status,claim_expires_at,created_at,event_id
    );
CREATE TABLE IF NOT EXISTS message_consumer_checkpoints (
    consumer_group TEXT PRIMARY KEY,
    last_event_id TEXT NOT NULL,
    last_source_cursor TEXT,
    completed_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_jobs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    required_skill TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT,
    claim_token TEXT,
    lease_id TEXT,
    lease_token_hash TEXT,
    lease_expires_at TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claimed_at TEXT,
    heartbeat_at TEXT,
    completed_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_agent_id TEXT,
    last_failure_reason TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(claimed_by) REFERENCES agents(id)
);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_queue
    ON agent_jobs(status, required_skill, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_agent
    ON agent_jobs(claimed_by, updated_at);
CREATE TABLE IF NOT EXISTS iphone_capability_requests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    requesting_agent_id TEXT NOT NULL,
    requesting_job_id TEXT NOT NULL,
    lease_generation INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    capability_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_id TEXT NOT NULL UNIQUE,
    request_audit_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    delivered_at TEXT,
    completed_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(requesting_agent_id) REFERENCES agents(id),
    FOREIGN KEY(requesting_job_id) REFERENCES agent_jobs(id),
    FOREIGN KEY(device_id) REFERENCES devices(id)
);
CREATE INDEX IF NOT EXISTS idx_iphone_capability_device
    ON iphone_capability_requests(device_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_iphone_capability_job
    ON iphone_capability_requests(requesting_job_id, lease_generation, created_at);
CREATE TABLE IF NOT EXISTS iphone_capability_grants (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL,
    capability_name TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    action_digest TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    FOREIGN KEY(request_id) REFERENCES iphone_capability_requests(id),
    FOREIGN KEY(device_id) REFERENCES devices(id)
);
CREATE TABLE IF NOT EXISTS iphone_capability_results (
    request_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES iphone_capability_requests(id)
);
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, provider),
    FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS eval_examples (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    corrected_behavior TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_scores (
    agent_id TEXT PRIMARY KEY,
    sample_count INTEGER NOT NULL,
    score REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_score_snapshots (
    agent_id TEXT PRIMARY KEY,
    completed_jobs INTEGER NOT NULL CHECK(completed_jobs >= 0),
    failed_jobs INTEGER NOT NULL CHECK(failed_jobs >= 0),
    terminal_jobs INTEGER NOT NULL CHECK(terminal_jobs >= 0),
    lease_expiry_count INTEGER NOT NULL CHECK(lease_expiry_count >= 0),
    observed_outcomes INTEGER NOT NULL CHECK(observed_outcomes >= 0),
    completion_rate REAL NOT NULL,
    failure_rate REAL NOT NULL,
    timeout_rate REAL NOT NULL,
    feedback_count INTEGER NOT NULL CHECK(feedback_count >= 0),
    feedback_average REAL,
    average_latency_seconds REAL,
    composite_score REAL NOT NULL,
    formula_version TEXT NOT NULL,
    rebuilt_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_score_snapshots_rank
    ON agent_score_snapshots(composite_score DESC,agent_id ASC);
CREATE TABLE IF NOT EXISTS scheduler_decisions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    selected_agent_id TEXT NOT NULL,
    scoring_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES agent_jobs(id),
    FOREIGN KEY(selected_agent_id) REFERENCES agents(id)
);
CREATE INDEX IF NOT EXISTS idx_scheduler_decisions_job
    ON scheduler_decisions(job_id, created_at);
CREATE TABLE IF NOT EXISTS goal_runs (
    id TEXT PRIMARY KEY,
    root_task_id TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'planning','running','waiting_permission','completed','failed',
        'cancelled','budget_exhausted'
    )),
    autonomy_profile TEXT NOT NULL CHECK(autonomy_profile IN (
        'manual','assisted','autonomous'
    )),
    planner_source TEXT NOT NULL CHECK(planner_source IN (
        'iphone_local','ubuntu_local','manual','test'
    )),
    max_steps INTEGER NOT NULL CHECK(max_steps > 0),
    max_parallelism INTEGER NOT NULL CHECK(max_parallelism > 0),
    max_replans INTEGER NOT NULL CHECK(max_replans >= 0),
    max_runtime_seconds INTEGER NOT NULL CHECK(max_runtime_seconds > 0),
    max_model_calls INTEGER NOT NULL CHECK(max_model_calls > 0),
    paused_at TEXT,
    paused_seconds REAL NOT NULL DEFAULT 0,
    conversation_revision INTEGER NOT NULL DEFAULT 0,
    pending_message_revision INTEGER NOT NULL DEFAULT 0,
    reply_dispatch_credit INTEGER NOT NULL DEFAULT 0,
    step_count INTEGER NOT NULL DEFAULT 0 CHECK(step_count >= 0),
    replan_count INTEGER NOT NULL DEFAULT 0 CHECK(replan_count >= 0),
    model_call_count INTEGER NOT NULL DEFAULT 0 CHECK(model_call_count >= 0),
    completion_criteria_json TEXT NOT NULL DEFAULT '[]',
    current_phase TEXT NOT NULL DEFAULT 'planning',
    evaluator_status TEXT,
    evaluator_summary TEXT,
    plan_fingerprint TEXT,
    evaluation_fingerprint TEXT,
    last_state_fingerprint TEXT,
    repeated_evaluation_count INTEGER NOT NULL DEFAULT 0
        CHECK(repeated_evaluation_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    failure_reason TEXT,
    FOREIGN KEY(root_task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_runs_status
    ON goal_runs(status, updated_at, id);
CREATE TABLE IF NOT EXISTS plan_nodes (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    parent_node_id TEXT,
    task_id TEXT UNIQUE,
    node_type TEXT NOT NULL CHECK(node_type IN ('worker','synthesis')),
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    required_skill TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'planned','ready','dispatched','running','waiting_permission',
        'waiting_capability','completed','failed','blocked','cancelled','skipped'
    )),
    priority INTEGER NOT NULL DEFAULT 0,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    assigned_agent_id TEXT,
    worker_job_id TEXT UNIQUE,
    expected_output TEXT NOT NULL,
    result_summary TEXT,
    error_summary TEXT,
    planner_metadata_json TEXT NOT NULL DEFAULT '{}',
    conversation_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_node_id) REFERENCES plan_nodes(id),
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(assigned_agent_id) REFERENCES agents(id),
    FOREIGN KEY(worker_job_id) REFERENCES agent_jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_plan_nodes_goal_status
    ON plan_nodes(goal_run_id, status, priority DESC, created_at, id);
CREATE TABLE IF NOT EXISTS goal_code_proposals (
    node_id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    worker_job_id TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    content TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    summary TEXT NOT NULL,
    apply_task_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(node_id) REFERENCES plan_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(worker_job_id) REFERENCES agent_jobs(id),
    FOREIGN KEY(apply_task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_code_proposals_goal
    ON goal_code_proposals(goal_run_id,node_id);
CREATE TABLE IF NOT EXISTS coding_projects (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_project_links (
    goal_run_id TEXT PRIMARY KEY REFERENCES goal_runs(id),
    project_id TEXT NOT NULL REFERENCES coding_projects(id)
);
CREATE TABLE IF NOT EXISTS project_revisions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES coding_projects(id),
    goal_run_id TEXT NOT NULL REFERENCES goal_runs(id),
    node_id TEXT NOT NULL UNIQUE REFERENCES plan_nodes(id),
    worker_job_id TEXT NOT NULL UNIQUE REFERENCES agent_jobs(id),
    revision INTEGER NOT NULL, snapshot_json TEXT NOT NULL, sha256 TEXT NOT NULL,
    apply_task_id TEXT REFERENCES tasks(id), created_at TEXT NOT NULL,
    UNIQUE(project_id,revision)
);
CREATE TABLE IF NOT EXISTS project_memory_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES coding_projects(id),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('message','plan')),
    source_id TEXT NOT NULL,
    source_goal_id TEXT NOT NULL REFERENCES goal_runs(id),
    source_revision_id TEXT REFERENCES project_revisions(id),
    source_order INTEGER NOT NULL,
    summary TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    embedding_identity TEXT,
    dimensions INTEGER,
    vector_json TEXT,
    embedding_fingerprint TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id,source_kind,source_id)
);
CREATE INDEX IF NOT EXISTS idx_project_memory_items_project
    ON project_memory_items(project_id,source_order,id);
CREATE TABLE IF NOT EXISTS project_memory_queries (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES coding_projects(id),
    goal_run_id TEXT NOT NULL REFERENCES goal_runs(id),
    node_id TEXT NOT NULL REFERENCES plan_nodes(id),
    conversation_revision INTEGER NOT NULL,
    base_revision_id TEXT REFERENCES project_revisions(id),
    provider_identity TEXT NOT NULL,
    query_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('started','completed','failed')),
    query_dimensions INTEGER,
    query_vector_json TEXT,
    query_vector_fingerprint TEXT,
    error_category TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_project_memory_queries_goal ON project_memory_queries(goal_run_id,node_id);
CREATE TABLE IF NOT EXISTS goal_conversations (
    id TEXT PRIMARY KEY, active_goal_id TEXT NOT NULL REFERENCES goal_runs(id)
);
CREATE TABLE IF NOT EXISTS goal_conversation_links (
    goal_run_id TEXT PRIMARY KEY REFERENCES goal_runs(id),
    conversation_id TEXT NOT NULL REFERENCES goal_conversations(id),
    parent_goal_id TEXT REFERENCES goal_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_conversation_links ON goal_conversation_links(conversation_id);
CREATE TABLE IF NOT EXISTS goal_messages (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES goal_conversations(id),
    goal_run_id TEXT NOT NULL REFERENCES goal_runs(id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
    actor_id TEXT, client_message_id TEXT, reply_to_message_id TEXT,
    is_question INTEGER NOT NULL DEFAULT 0, answered_by_message_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id,actor_id,client_message_id)
);
CREATE INDEX IF NOT EXISTS idx_goal_messages_conversation ON goal_messages(conversation_id,created_at,id);
CREATE TABLE IF NOT EXISTS plan_edges (
    goal_run_id TEXT NOT NULL,
    from_node_id TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    dependency_type TEXT NOT NULL CHECK(dependency_type IN ('hard','optional')),
    PRIMARY KEY(from_node_id, to_node_id),
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(from_node_id) REFERENCES plan_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY(to_node_id) REFERENCES plan_nodes(id) ON DELETE CASCADE,
    CHECK(from_node_id <> to_node_id)
);
CREATE INDEX IF NOT EXISTS idx_plan_edges_goal_to
    ON plan_edges(goal_run_id, to_node_id);
CREATE TABLE IF NOT EXISTS goal_evaluations (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    status TEXT NOT NULL CHECK(status IN ('continue','replan','done','failed','needs_user')),
    reason_summary TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    decision_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(goal_run_id, sequence),
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_goal_evaluations_goal
    ON goal_evaluations(goal_run_id, sequence);
CREATE TABLE IF NOT EXISTS goal_results (
    goal_run_id TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS goal_feedback (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    score REAL NOT NULL CHECK(score >= 0 AND score <= 5),
    note TEXT,
    corrected_final_answer TEXT,
    corrected_plan_summary TEXT,
    reviewed INTEGER NOT NULL DEFAULT 0 CHECK(reviewed IN (0,1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_goal_feedback_goal
    ON goal_feedback(goal_run_id, created_at);
CREATE TABLE IF NOT EXISTS goal_model_calls (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    node_id TEXT,
    conversation_revision INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL CHECK(role IN ('planner','evaluator','summarizer','synthesizer')),
    provider_source TEXT NOT NULL,
    model_id TEXT,
    context_id TEXT,
    input_digest TEXT NOT NULL,
    output_digest TEXT,
    status TEXT NOT NULL CHECK(status IN ('started','completed','failed')),
    latency_ms INTEGER CHECK(latency_ms IS NULL OR latency_ms >= 0),
    error_category TEXT,
    owner_instance_id TEXT,
    lease_expires_at TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(node_id) REFERENCES plan_nodes(id),
    FOREIGN KEY(context_id) REFERENCES goal_contexts(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_model_calls_goal
    ON goal_model_calls(goal_run_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_model_calls_one_active
    ON goal_model_calls(goal_run_id) WHERE status='started';
CREATE TABLE IF NOT EXISTS goal_contexts (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL,
    root_task_id TEXT NOT NULL,
    node_id TEXT,
    purpose TEXT NOT NULL,
    context_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    approx_token_count INTEGER NOT NULL CHECK(approx_token_count >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(root_task_id) REFERENCES tasks(id),
    FOREIGN KEY(node_id) REFERENCES plan_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_contexts_goal
    ON goal_contexts(goal_run_id, created_at);
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    goal_run_id TEXT NOT NULL UNIQUE,
    root_task_id TEXT NOT NULL,
    objective_summary TEXT NOT NULL,
    plan_summary TEXT NOT NULL,
    outcome TEXT NOT NULL,
    score REAL,
    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
    worker_types_json TEXT NOT NULL,
    failure_tags_json TEXT NOT NULL,
    user_feedback_score REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(goal_run_id) REFERENCES goal_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(root_task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_episodes_outcome_created
    ON episodes(outcome, created_at, id);
CREATE TABLE IF NOT EXISTS episode_steps (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    node_type TEXT NOT NULL,
    skill TEXT,
    agent_id TEXT,
    input_summary TEXT NOT NULL,
    output_summary TEXT NOT NULL,
    result_status TEXT NOT NULL,
    latency_ms INTEGER CHECK(latency_ms IS NULL OR latency_ms >= 0),
    UNIQUE(episode_id, sequence),
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS episode_embeddings (
    episode_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK(dimensions > 0),
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(episode_id, provider),
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    event_type TEXT NOT NULL,
    actor_type TEXT,
    actor_id TEXT,
    task_id TEXT,
    payload_json TEXT NOT NULL,
    prev_hash TEXT,
    hash TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class StateConflict(RuntimeError):
    pass


class StateService:
    """Authoritative domain state and additive migrations for the local database."""

    def __init__(
        self,
        db_path: Path,
        embedding_service: EmbeddingService | None = None,
        *,
        permission_policy: PermissionPolicy | None = None,
    ) -> None:
        self.db_path = db_path
        self.embedding_service = embedding_service
        self.worker_skill_policy = WorkerSkillPolicyStore(db_path, permission_policy)

    async def initialize(self) -> None:
        parent_existed = self.db_path.parent.exists()
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not parent_existed:
            self.db_path.parent.chmod(0o700)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.db_path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError("state database must be a safe local file") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("state database must be a regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

        interrupted: list[tuple[str, str, str]] = []
        async with aiosqlite.connect(self.db_path) as db:
            version_row = await (await db.execute("PRAGMA user_version")).fetchone()
            if version_row is None:
                raise RuntimeError("SQLite did not return a schema version")
            version = int(version_row[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError("state database schema is newer than this control plane")
            await db.executescript(SCHEMA)
            await db.execute("BEGIN IMMEDIATE")
            await self._migrate_legacy_schema(db)
            if version < 22:
                await db.execute("""UPDATE goal_runs SET paused_at=updated_at
                    WHERE status='waiting_permission' AND current_phase IN
                    ('needs_user','code_proposal_ready','project_ready') AND paused_at IS NULL""")
            await db.execute("""INSERT OR IGNORE INTO goal_conversations(id,active_goal_id)
                SELECT 'gconv_' || id,id FROM goal_runs
                WHERE id NOT IN (SELECT goal_run_id FROM goal_conversation_links)""")
            await db.execute("""INSERT OR IGNORE INTO goal_conversation_links(goal_run_id,conversation_id)
                SELECT id,'gconv_' || id FROM goal_runs
                WHERE id NOT IN (SELECT goal_run_id FROM goal_conversation_links)""")
            await db.execute("""INSERT OR IGNORE INTO goal_messages(
                id,conversation_id,goal_run_id,role,content,created_at)
                SELECT 'gmsg_initial_' || g.id,l.conversation_id,g.id,'user',g.objective,g.created_at
                FROM goal_runs g JOIN goal_conversation_links l ON l.goal_run_id=g.id
                WHERE NOT EXISTS (SELECT 1 FROM goal_messages m WHERE m.goal_run_id=g.id)""")
            await db.execute(
                "INSERT OR IGNORE INTO outbox_operational_metrics(singleton_id) VALUES(1)"
            )
            agent_metrics = await (
                await db.execute("SELECT 1 FROM agent_job_operational_metrics WHERE singleton_id=1")
            ).fetchone()
            seed_agent_metrics = agent_metrics is None
            if seed_agent_metrics:
                # Install the singleton before reconciliation so any future
                # migration helper that uses the normal counter update path can
                # do so safely. Exact historical totals are seeded after all
                # reconciliation below, in this same writer transaction.
                await db.execute(
                    """
                    INSERT INTO agent_job_operational_metrics(
                        singleton_id,lease_expirations,retries,dead_letter_events,updated_at
                    ) VALUES(1,0,0,0,?)
                    """,
                    (datetime.now(UTC).isoformat(),),
                )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbox_claimable
                ON outbox_events(published_at, publishing_lease_expires_at, id)
                """
            )
            await self._reconcile_agent_jobs_locked(db)
            await self._reconcile_capability_requests_locked(db)
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_message_board_dedupe
                ON message_board_events(dedupe_key)
                """
            )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_request_audit
                ON approvals(request_audit_id) WHERE request_audit_id IS NOT NULL
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_events(trace_id, created_at)"
            )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_last_pairing
                ON devices(last_pairing_id) WHERE last_pairing_id IS NOT NULL
                """
            )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_one_active_task
                ON agent_jobs(task_id)
                WHERE status IN ('queued','claimed','running')
                """
            )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_iphone_capability_one_nonterminal_fingerprint
                ON iphone_capability_requests(request_fingerprint)
                WHERE status NOT IN ('completed','denied','failed','cancelled','expired')
                """
            )
            if version < SCHEMA_VERSION:
                await db.execute("DELETE FROM pairing_codes")
                await db.execute("DELETE FROM pairing_candidates")
                await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            interrupted = [
                (str(row[0]), str(row[1]), "running")
                for row in await (
                    await db.execute("SELECT id,task_id FROM tool_calls WHERE status='running'")
                ).fetchall()
            ]
            if interrupted:
                now = datetime.now(UTC).isoformat()
                message = "control plane restarted during execution; outcome uncertain; not retried"
                await db.execute(
                    """
                    UPDATE tool_calls SET status='failed',error=?,updated_at=?
                    WHERE status='running'
                    """,
                    (message, now),
                )
                for task_id in {task_id for _, task_id, _ in interrupted}:
                    await db.execute(
                        """
                        UPDATE tasks SET status='failed',updated_at=?,completed_at=?,error_json=?
                        WHERE id=? AND status='running'
                        """,
                        (now, now, json.dumps({"message": message}), task_id),
                    )
            queued_before_claim = [
                (str(row[0]), str(row[1]), str(row[2]))
                for row in await (
                    await db.execute(
                        """
                        SELECT c.id,c.task_id,
                            CASE
                                WHEN a.status='approved' THEN 'approved_before_claim'
                                ELSE 'queued_before_claim'
                            END
                        FROM tool_calls AS c
                        JOIN tasks AS t ON t.id=c.task_id
                        LEFT JOIN approvals AS a ON a.id=c.approval_id
                        WHERE c.status='queued' AND t.status='queued'
                        """
                    )
                ).fetchall()
            ]
            if queued_before_claim:
                now = datetime.now(UTC).isoformat()
                for tool_call_id, task_id, stage in queued_before_claim:
                    if stage == "approved_before_claim":
                        message = (
                            "control plane restarted after approval but before execution claim; "
                            "execution not started; not retried"
                        )
                    else:
                        message = (
                            "control plane restarted after a tool call was queued but before "
                            "execution claim; execution not started; not retried"
                        )
                    await db.execute(
                        """
                        UPDATE tool_calls SET status='failed',error=?,updated_at=?
                        WHERE id=? AND task_id=? AND status='queued'
                        """,
                        (message, now, tool_call_id, task_id),
                    )
                    await db.execute(
                        """
                        UPDATE tasks SET status='failed',updated_at=?,completed_at=?,error_json=?
                        WHERE id=? AND status='queued'
                        """,
                        (now, now, json.dumps({"message": message}), task_id),
                    )
                interrupted.extend(queued_before_claim)
            for tool_call_id, task_id, stage in interrupted:
                running = stage == "running"
                await append_audit_event(
                    db,
                    "execution.interrupted" if running else "execution.not_started",
                    {
                        "tool_call_id": tool_call_id,
                        "stage": stage,
                        "outcome": "uncertain" if running else "not_started",
                        "retry": False,
                    },
                    task_id=task_id,
                    trace_id=task_id,
                )
            # Local restart recovery can make a task terminal after the first
            # agent-job pass. Reconcile again in the same transaction so no
            # queued or leased remote job survives for that terminal parent.
            await self._reconcile_agent_jobs_locked(db)
            if seed_agent_metrics:
                # This unbounded migration scan is paid exactly once. It runs
                # after reconciliation because migration can itself append
                # requeue/dead-letter events. Normal /status reads only this
                # singleton and never scans the audit history.
                historical = await (
                    await db.execute(
                        """
                        SELECT
                          SUM(CASE WHEN event_type='agent.job.lease_expired' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN event_type='agent.job.requeued' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN event_type='agent.job.dead_lettered' THEN 1 ELSE 0 END)
                        FROM audit_events
                        """
                    )
                ).fetchone()
                updated = await db.execute(
                    """
                    UPDATE agent_job_operational_metrics
                    SET lease_expirations=?,retries=?,dead_letter_events=?,updated_at=?
                    WHERE singleton_id=1
                    """,
                    (
                        int(historical[0] or 0) if historical else 0,
                        int(historical[1] or 0) if historical else 0,
                        int(historical[2] or 0) if historical else 0,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("agent job operational metrics are unavailable")
            await db.commit()
        for suffix in ("", "-wal", "-shm"):
            database_file = Path(f"{self.db_path}{suffix}")
            if database_file.exists():
                database_file.chmod(0o600)

    async def _migrate_legacy_schema(self, db: aiosqlite.Connection) -> None:
        additions = {
            "tasks": {
                "title": "TEXT NOT NULL DEFAULT ''",
                "priority": "INTEGER NOT NULL DEFAULT 0",
                "completed_at": "TEXT",
                "error_json": "TEXT",
            },
            "approvals": {
                "tool_call_id": "TEXT",
                "action_digest": "TEXT",
                "request_audit_id": "INTEGER",
                "expires_at": "TEXT",
                "decision_json": "TEXT",
            },
            "pairing_codes": {
                "attempts_remaining": "INTEGER NOT NULL DEFAULT 10",
                "created_at": "TEXT",
            },
            "devices": {
                "last_seen_at": "TEXT",
                "last_pairing_id": "TEXT",
                "websocket_connection_id": "TEXT",
            },
            "agents": {"auth_token_hash": "TEXT"},  # nosec B105 - SQLite column type
            "message_board_events": {
                "schema_version": "TEXT",
                "event_id": "TEXT",
                "aggregate_type": "TEXT",
                "aggregate_id": "TEXT",
                "dedupe_key": "TEXT",
            },
            "outbox_events": {
                "event_id": "TEXT",
                "publishing_owner": "TEXT",
                "publishing_started_at": "TEXT",
                "publishing_lease_expires_at": "TEXT",
                "publish_generation": "INTEGER NOT NULL DEFAULT 0",
            },
            "agent_jobs": {
                "lease_id": "TEXT",
                "lease_token_hash": "TEXT",  # nosec B105 - SQLite column type
                "lease_expires_at": "TEXT",
                "lease_generation": "INTEGER NOT NULL DEFAULT 0",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "max_attempts": "INTEGER NOT NULL DEFAULT 3",
                "last_agent_id": "TEXT",
                "last_failure_reason": "TEXT",
            },
            "goal_runs": {
                "paused_at": "TEXT",
                "paused_seconds": "REAL NOT NULL DEFAULT 0",
                "conversation_revision": "INTEGER NOT NULL DEFAULT 0",
                "pending_message_revision": "INTEGER NOT NULL DEFAULT 0",
                "reply_dispatch_credit": "INTEGER NOT NULL DEFAULT 0",
            },
            "plan_nodes": {"conversation_revision": "INTEGER NOT NULL DEFAULT 0"},
            "goal_model_calls": {
                "conversation_revision": "INTEGER NOT NULL DEFAULT 0",
                "owner_instance_id": "TEXT",
                "lease_expires_at": "TEXT",
                "lease_generation": "INTEGER NOT NULL DEFAULT 0",
            },
            "iphone_capability_requests": {
                "request_audit_id": "INTEGER",
                "request_fingerprint": "TEXT",
            },
            "audit_events": {
                "trace_id": "TEXT",
                "actor_type": "TEXT",
                "actor_id": "TEXT",
                "task_id": "TEXT",
                "prev_hash": "TEXT",
                "hash": "TEXT",
            },
        }
        for table, columns in additions.items():
            rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
            existing = {str(row[1]) for row in rows}
            for column, declaration in columns.items():
                if column not in existing:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        await db.execute(
            """UPDATE outbox_events SET event_id='evt_outbox_' || id
            WHERE event_id IS NULL OR event_id=''"""
        )
        await db.execute(
            """UPDATE message_board_events SET
                schema_version=COALESCE(NULLIF(schema_version,''), '1.0'),
                event_id=COALESCE(NULLIF(event_id,''), 'evt_board_' || id),
                aggregate_type=COALESCE(NULLIF(aggregate_type,''), 'message'),
                aggregate_id=COALESCE(NULLIF(aggregate_id,''), message_id)
            WHERE schema_version IS NULL OR schema_version=''
               OR event_id IS NULL OR event_id=''
               OR aggregate_type IS NULL OR aggregate_type=''
               OR aggregate_id IS NULL OR aggregate_id=''"""
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_event_id ON outbox_events(event_id)"
        )
        await db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_message_board_event_id
            ON message_board_events(event_id)"""
        )
        agent_columns = {
            str(row[1]) for row in await (await db.execute("PRAGMA table_info(agents)")).fetchall()
        }
        for column, declaration in {
            "last_seen_at": "TEXT",
            "max_concurrency": "INTEGER NOT NULL DEFAULT 1",
            "capacity_json": "TEXT NOT NULL DEFAULT '{}'",
            "runtime": "TEXT NOT NULL DEFAULT 'python'",
            "supported_protocol_version": ("TEXT NOT NULL DEFAULT 'mongars-worker-v0.9'"),
        }.items():
            if column not in agent_columns:
                await db.execute(f"ALTER TABLE agents ADD COLUMN {column} {declaration}")
        await db.execute(
            "UPDATE agents SET last_seen_at=last_heartbeat_at WHERE last_seen_at IS NULL"
        )
        legacy_agents = await (
            await db.execute(
                """SELECT id,name,version,model_id,skills_json,max_concurrency,
                          capacity_json,runtime,supported_protocol_version
                FROM agents"""
            )
        ).fetchall()
        for legacy_agent in legacy_agents:
            agent_id = str(legacy_agent[0])
            try:
                public_agent_card(
                    {
                        "id": agent_id,
                        "name": str(legacy_agent[1]),
                        "version": str(legacy_agent[2]),
                        "model_id": legacy_agent[3],
                        "skills": json.loads(str(legacy_agent[4])),
                        "max_concurrency": int(legacy_agent[5]),
                        "capacity": json.loads(str(legacy_agent[6])),
                        "runtime": str(legacy_agent[7]),
                        "supported_protocol_version": str(legacy_agent[8]),
                    }
                )
            except (TypeError, ValueError):
                await db.execute(
                    """UPDATE agents SET status='unverified',skills_json='[]',
                              capacity_json='{}',max_concurrency=1,runtime='python',
                              supported_protocol_version='mongars-worker-v0.9'
                    WHERE id=?""",
                    (agent_id,),
                )
                await append_audit_event(
                    db,
                    "agent.card.quarantined",
                    {
                        "agent_id": agent_id,
                        "reason": "legacy registration outside v0.10 policy",
                    },
                    actor_type="control-plane",
                    actor_id="migration",
                    created_at=datetime.now(UTC).isoformat(),
                )
        await db.execute(
            "UPDATE tasks SET title=substr(input, 1, 80) WHERE title='' OR title IS NULL"
        )
        await db.execute(
            "UPDATE approvals SET expires_at=created_at WHERE expires_at IS NULL OR expires_at=''"
        )
        approval_rows = await (
            await db.execute(
                """
                SELECT a.id,a.action,a.tool_call_id,a.action_digest,
                       c.id,c.tool_name,c.arguments_json
                FROM approvals AS a
                LEFT JOIN tool_calls AS c ON c.approval_id=a.id
                """
            )
        ).fetchall()
        for (
            approval_id,
            approval_action,
            stored_call_id,
            stored_digest,
            linked_call_id,
            linked_tool_name,
            arguments_json,
        ) in approval_rows:
            if stored_call_id and stored_digest:
                continue
            binding_call_id = str(stored_call_id or linked_call_id or f"unbound:{approval_id}")
            binding_tool_name = str(linked_tool_name or approval_action or "unbound")
            try:
                arguments = json.loads(str(arguments_json)) if arguments_json is not None else {}
                if not isinstance(arguments, dict):
                    raise ApprovalBindingError("legacy tool arguments are not an object")
                digest = canonical_action_digest(
                    tool_call_id=binding_call_id,
                    tool_name=binding_tool_name,
                    arguments=arguments,
                )
            except (ApprovalBindingError, json.JSONDecodeError):
                binding_call_id = f"unbound:{approval_id}"
                digest = canonical_action_digest(
                    tool_call_id=binding_call_id,
                    tool_name=str(approval_action or "unbound"),
                    arguments={},
                )
            await db.execute(
                """
                UPDATE approvals SET tool_call_id=?,action_digest=?
                WHERE id=? AND (tool_call_id IS NULL OR action_digest IS NULL)
                """,
                (binding_call_id, digest, approval_id),
            )
        legacy_pending = await (
            await db.execute(
                """
                SELECT id,task_id,tool_call_id FROM approvals
                WHERE status='pending' AND request_audit_id IS NULL
                """
            )
        ).fetchall()
        if legacy_pending:
            now = datetime.now(UTC).isoformat()
            for approval_id, task_id, tool_call_id in legacy_pending:
                await db.execute(
                    """
                    UPDATE approvals SET status='cancelled',decided_at=?
                    WHERE id=? AND status='pending' AND request_audit_id IS NULL
                    """,
                    (now, approval_id),
                )
                await db.execute(
                    """
                    UPDATE tool_calls SET status='cancelled',updated_at=?
                    WHERE approval_id=? AND status='waiting_permission'
                    """,
                    (now, approval_id),
                )
                await db.execute(
                    """
                    UPDATE tasks SET status='blocked',updated_at=?
                    WHERE id=? AND status='waiting_permission'
                    """,
                    (now, task_id),
                )
                await append_audit_event(
                    db,
                    "approval.invalidated.migration",
                    {
                        "approval_id": str(approval_id),
                        "tool_call_id": str(tool_call_id),
                        "reason": "legacy approval has no authenticated consent context",
                    },
                    task_id=str(task_id),
                    trace_id=str(task_id),
                    created_at=now,
                )
        device_rows = await (await db.execute("SELECT id,token FROM devices")).fetchall()
        for device_id, token in device_rows:
            stored = str(token)
            if not stored.startswith("sha256:"):
                digest = hashlib.sha256(stored.encode("utf-8")).hexdigest()
                await db.execute(
                    "UPDATE devices SET token=? WHERE id=?", (f"sha256:{digest}", device_id)
                )

    async def _reconcile_agent_jobs_locked(self, db: aiosqlite.Connection) -> None:
        """Fence legacy leases and collapse pre-v0.9 duplicate active jobs safely."""

        db.row_factory = aiosqlite.Row
        rows = list(
            await (
                await db.execute(
                    """
                    SELECT j.*,t.status AS task_status
                    FROM agent_jobs AS j JOIN tasks AS t ON t.id=j.task_id
                    WHERE j.status IN ('queued','claimed','running')
                    ORDER BY j.task_id ASC,
                             CASE j.status WHEN 'running' THEN 0 WHEN 'claimed' THEN 1 ELSE 2 END,
                             j.created_at ASC,j.id ASC
                    """
                )
            ).fetchall()
        )
        grouped: dict[str, list[aiosqlite.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["task_id"]), []).append(row)

        now = datetime.now(UTC).isoformat()
        terminal_tasks = {"completed", "failed", "cancelled"}
        recoverable_tasks = {"created", "planned", "queued", "running"}
        safe_retry_skills = {"workspace.list_dir", "workspace.read_text"}
        for task_id, jobs in grouped.items():
            task_status = str(jobs[0]["task_status"])
            if task_status in terminal_tasks:
                survivor = None
            elif task_status == "queued":
                survivor = next((job for job in jobs if str(job["status"]) == "queued"), jobs[0])
            else:
                survivor = jobs[0]
            duplicates = (
                [job for job in jobs if str(job["id"]) != str(survivor["id"])]
                if survivor is not None
                else jobs
            )
            for duplicate in duplicates:
                await self._cancel_agent_job_for_migration_locked(
                    db,
                    duplicate,
                    now=now,
                    reason="duplicate active job fenced during v0.9 migration",
                    dedupe_suffix="migration-fence",
                )

            if survivor is None:
                continue
            job_id = str(survivor["id"])
            status = str(survivor["status"])
            if status == "queued":
                if task_status in {"created", "planned"}:
                    await db.execute(
                        "UPDATE tasks SET status='queued',updated_at=? WHERE id=? AND status=?",
                        (now, task_id, task_status),
                    )
                elif task_status != "queued":
                    await self._cancel_agent_job_for_migration_locked(
                        db,
                        survivor,
                        now=now,
                        reason=f"queued job is incompatible with task status {task_status}",
                        dedupe_suffix="incompatible-task",
                    )
                continue

            missing_lease = status in {"claimed", "running"} and (
                survivor["claim_token"] is not None
                or survivor["lease_id"] is None
                or survivor["lease_token_hash"] is None
                or survivor["lease_expires_at"] is None
            )
            if not missing_lease:
                await db.execute("UPDATE agent_jobs SET claim_token=NULL WHERE id=?", (job_id,))
                if task_status != "running":
                    await self._cancel_agent_job_for_migration_locked(
                        db,
                        survivor,
                        now=now,
                        reason=f"leased job is incompatible with task status {task_status}",
                        dedupe_suffix="incompatible-task",
                    )
                continue

            if task_status not in recoverable_tasks:
                await self._cancel_agent_job_for_migration_locked(
                    db,
                    survivor,
                    now=now,
                    reason=f"legacy lease is incompatible with task status {task_status}",
                    dedupe_suffix="incompatible-task",
                )
                continue

            local_execution = await (
                await db.execute(
                    "SELECT 1 FROM tool_calls WHERE task_id=? AND status='running' LIMIT 1",
                    (task_id,),
                )
            ).fetchone()
            can_requeue = (
                str(survivor["required_skill"]) in safe_retry_skills and local_execution is None
            )
            target = "queued" if can_requeue else "failed"
            reason = "legacy plaintext claim invalidated during v0.9 migration"
            capabilities = list(
                await (
                    await db.execute(
                        """
                        SELECT * FROM iphone_capability_requests
                        WHERE requesting_job_id=? AND lease_generation=?
                          AND status NOT IN ('completed','denied','failed','cancelled','expired')
                        """,
                        (job_id, int(survivor["lease_generation"])),
                    )
                ).fetchall()
            )
            for capability in capabilities:
                await self._cancel_capability_request_for_migration_locked(
                    db,
                    capability,
                    now=now,
                    reason=reason,
                    dedupe_suffix="migration-legacy-lease",
                )
            await db.execute(
                """
                UPDATE agent_jobs
                SET status=?,claimed_by=NULL,claim_token=NULL,lease_id=NULL,
                    lease_token_hash=NULL,lease_expires_at=NULL,
                    lease_generation=lease_generation+1,updated_at=?,completed_at=?,
                    last_failure_reason=?,error=?
                WHERE id=? AND status IN ('claimed','running')
                """,
                (
                    target,
                    now,
                    None if can_requeue else now,
                    reason,
                    None if can_requeue else "legacy worker lease was invalidated",
                    job_id,
                ),
            )
            if can_requeue and task_status in {"created", "planned", "running"}:
                await db.execute(
                    """
                    UPDATE tasks SET status='queued',updated_at=?,completed_at=NULL,error_json=NULL
                    WHERE id=? AND status IN ('created','planned','running')
                    """,
                    (now, task_id),
                )
            elif (
                not can_requeue and task_status in {"queued", "running"} and local_execution is None
            ):
                await db.execute(
                    """
                    UPDATE tasks SET status='failed',updated_at=?,completed_at=?,error_json=?
                    WHERE id=? AND status IN ('queued','running')
                    """,
                    (
                        now,
                        now,
                        json.dumps({"message": "legacy remote lease could not be recovered"}),
                        task_id,
                    ),
                )
            event_type = "requeued" if can_requeue else "dead_lettered"
            await append_audit_event(
                db,
                f"agent.job.{event_type}",
                {"job_id": job_id, "reason": "legacy lease invalidated"},
                actor_type="control-plane",
                actor_id="migration",
                task_id=task_id,
                trace_id=task_id,
                created_at=now,
            )
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="agent_job",
                aggregate_id=job_id,
                topic="tasks.inbox" if can_requeue else "tasks.status",
                event_type="published" if can_requeue else "failed",
                payload={"job_id": job_id, "reason": "legacy_lease_invalidated"},
                task_id=task_id,
                message_id=job_id,
                dedupe_key=f"agent-job:{job_id}:migration-{event_type}",
                created_at=now,
            )

        orphan_reason = "active task has no executable local or remote work after reconciliation"
        # Older startup sweeps mistook abstract goal containers for executable
        # leaf tasks. Recover only that proven projection error, not ordinary
        # terminal tasks. Publication acknowledgements do not change task state.
        recoverable_roots = await (
            await db.execute(
                """
                SELECT t.id,g.id AS goal_id,a.id AS failure_audit_id
                FROM tasks AS t JOIN goal_runs AS g ON g.root_task_id=t.id
                JOIN audit_events AS a ON a.task_id=t.id
                WHERE t.status='failed' AND t.error_json=?
                  AND g.status IN ('running','waiting_permission')
                  AND g.started_at IS NOT NULL AND g.completed_at IS NULL
                  AND a.event_type='task.failed.migration'
                  AND a.actor_type='control-plane' AND a.actor_id='migration'
                  AND a.trace_id=t.id AND a.payload_json=? AND a.hash IS NOT NULL
                  AND a.created_at=t.updated_at AND a.created_at=t.completed_at
                  AND a.id=(
                      SELECT MAX(latest.id) FROM audit_events AS latest
                      WHERE latest.task_id=t.id
                        AND latest.event_type<>'outbox.publication.acknowledged'
                  )
                  AND NOT EXISTS (SELECT 1 FROM agent_jobs WHERE task_id=t.id)
                  AND NOT EXISTS (SELECT 1 FROM tool_calls WHERE task_id=t.id)
                ORDER BY t.id
                """,
                (
                    json.dumps({"message": orphan_reason}),
                    json.dumps({"reason": orphan_reason}, separators=(",", ":"), sort_keys=True),
                ),
            )
        ).fetchall()
        for root in recoverable_roots:
            task_id = str(root["id"])
            failure_audit_id = int(root["failure_audit_id"])
            # This is an audited repair of one historical projection bug. The
            # normal task state machine still forbids all terminal transitions.
            restored = await db.execute(
                """UPDATE tasks SET status='running',updated_at=?,completed_at=NULL,error_json=NULL
                WHERE id=? AND status='failed'""",
                (now, task_id),
            )
            if restored.rowcount != 1:
                raise RuntimeError("goal root changed during startup recovery")
            await append_audit_event(
                db,
                "task.goal_root.recovered",
                {
                    "goal_run_id": str(root["goal_id"]),
                    "migration_failure_audit_id": failure_audit_id,
                    "previous_status": "failed",
                    "status": "running",
                },
                actor_type="control-plane",
                actor_id="migration",
                task_id=task_id,
                trace_id=task_id,
                created_at=now,
            )
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="task",
                aggregate_id=task_id,
                topic="tasks.status",
                event_type="running",
                payload={
                    "task_id": task_id,
                    "status": "running",
                    "reason": "active_goal_root_recovered",
                },
                task_id=task_id,
                message_id=task_id,
                dedupe_key=f"task:{task_id}:goal-root-recovered:{failure_audit_id}",
                created_at=now,
            )

        orphaned_tasks = list(
            await (
                await db.execute(
                    """
                    SELECT t.id,t.status FROM tasks AS t
                    WHERE t.status IN ('queued','running')
                      AND NOT EXISTS (
                          SELECT 1 FROM goal_runs AS g WHERE g.root_task_id=t.id
                            AND g.status IN ('planning','running','waiting_permission')
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM agent_jobs AS j
                          WHERE j.task_id=t.id
                            AND j.status IN ('queued','claimed','running')
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM tool_calls AS c
                          WHERE c.task_id=t.id AND c.status IN ('queued','running')
                      )
                    ORDER BY t.id ASC
                    """
                )
            ).fetchall()
        )
        for orphan in orphaned_tasks:
            task_id = str(orphan["id"])
            task_status = str(orphan["status"])
            reason = orphan_reason
            await TaskStateMachine.transition_locked(
                db,
                task_id=task_id,
                current=task_status,
                target="failed",
                now=now,
                error=reason,
            )
            await append_audit_event(
                db,
                "task.failed.migration",
                {"reason": reason},
                actor_type="control-plane",
                actor_id="migration",
                task_id=task_id,
                trace_id=task_id,
                created_at=now,
            )
            await OutboxService.enqueue_locked(
                db,
                aggregate_type="task",
                aggregate_id=task_id,
                topic="tasks.status",
                event_type="failed",
                payload={"task_id": task_id, "status": "failed", "reason": "orphaned_work"},
                task_id=task_id,
                message_id=task_id,
                dedupe_key=f"task:{task_id}:migration-orphaned",
                created_at=now,
            )

    async def _reconcile_capability_requests_locked(self, db: aiosqlite.Connection) -> None:
        """Backfill stable request fingerprints and cancel ambiguous active duplicates."""

        db.row_factory = aiosqlite.Row
        rows = list(
            await (
                await db.execute(
                    "SELECT * FROM iphone_capability_requests ORDER BY created_at ASC,id ASC"
                )
            ).fetchall()
        )
        now = datetime.now(UTC).isoformat()
        for row in rows:
            request_id = str(row["id"])
            try:
                arguments = json.loads(str(row["arguments_json"]))
                if not isinstance(arguments, dict):
                    raise CapabilityRequestBindingError(
                        "capability request arguments are not an object"
                    )
                fingerprint = canonical_capability_request_fingerprint(
                    job_id=str(row["requesting_job_id"]),
                    lease_generation=int(row["lease_generation"]),
                    capability_name=str(row["capability_name"]),
                    arguments=arguments,
                )
            except (
                CapabilityRequestBindingError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                fingerprint = (
                    "sha256:"
                    + hashlib.sha256(
                        f"invalid-capability-request:{request_id}".encode()
                    ).hexdigest()
                )
                await db.execute(
                    "UPDATE iphone_capability_requests SET request_fingerprint=? WHERE id=?",
                    (fingerprint, request_id),
                )
                await self._cancel_capability_request_for_migration_locked(
                    db,
                    row,
                    now=now,
                    reason="capability request has invalid canonical arguments",
                    dedupe_suffix="migration-invalid-binding",
                )
                continue
            await db.execute(
                "UPDATE iphone_capability_requests SET request_fingerprint=? WHERE id=?",
                (fingerprint, request_id),
            )

        active_rows = list(
            await (
                await db.execute(
                    """
                    SELECT * FROM iphone_capability_requests
                    WHERE status NOT IN ('completed','denied','failed','cancelled','expired')
                    ORDER BY request_fingerprint ASC,
                             CASE status
                                 WHEN 'consumed' THEN 0
                                 WHEN 'approved' THEN 1
                                 WHEN 'waiting_approval' THEN 2
                                 ELSE 3
                             END,
                             created_at ASC,id ASC
                    """
                )
            ).fetchall()
        )
        grouped: dict[str, list[aiosqlite.Row]] = {}
        for row in active_rows:
            grouped.setdefault(str(row["request_fingerprint"]), []).append(row)
        for duplicates in grouped.values():
            for duplicate in duplicates[1:]:
                await self._cancel_capability_request_for_migration_locked(
                    db,
                    duplicate,
                    now=now,
                    reason="duplicate nonterminal capability request reconciled",
                    dedupe_suffix="migration-duplicate",
                )

    @classmethod
    async def _cancel_agent_job_for_migration_locked(
        cls,
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
        *,
        now: str,
        reason: str,
        dedupe_suffix: str,
    ) -> None:
        job_id = str(row["id"])
        task_id = str(row["task_id"])
        capabilities = list(
            await (
                await db.execute(
                    """
                    SELECT * FROM iphone_capability_requests
                    WHERE requesting_job_id=?
                      AND status NOT IN ('completed','denied','failed','cancelled','expired')
                    """,
                    (job_id,),
                )
            ).fetchall()
        )
        for capability in capabilities:
            await cls._cancel_capability_request_for_migration_locked(
                db,
                capability,
                now=now,
                reason=reason,
                dedupe_suffix=f"{dedupe_suffix}-job",
            )
        cursor = await db.execute(
            """
            UPDATE agent_jobs
            SET status='cancelled',claimed_by=NULL,claim_token=NULL,lease_id=NULL,
                lease_token_hash=NULL,lease_expires_at=NULL,
                lease_generation=lease_generation+1,updated_at=?,completed_at=?,
                last_failure_reason=?
            WHERE id=? AND status IN ('queued','claimed','running')
            """,
            (now, now, reason, job_id),
        )
        if cursor.rowcount != 1:
            return
        await append_audit_event(
            db,
            "agent.job.cancelled.migration",
            {"job_id": job_id, "reason": reason},
            actor_type="control-plane",
            actor_id="migration",
            task_id=task_id,
            trace_id=task_id,
            created_at=now,
        )
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="agent_job",
            aggregate_id=job_id,
            topic="tasks.status",
            event_type="cancelled",
            payload={"job_id": job_id, "reason": "migration_fence"},
            task_id=task_id,
            message_id=job_id,
            dedupe_key=f"agent-job:{job_id}:{dedupe_suffix}",
            created_at=now,
        )

    @staticmethod
    async def _cancel_capability_request_for_migration_locked(
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
        *,
        now: str,
        reason: str,
        dedupe_suffix: str,
    ) -> None:
        request_id = str(row["id"])
        cursor = await db.execute(
            """
            UPDATE iphone_capability_requests SET status='cancelled',completed_at=?
            WHERE id=? AND status NOT IN ('completed','denied','failed','cancelled','expired')
            """,
            (now, request_id),
        )
        if cursor.rowcount != 1:
            return
        await db.execute(
            "UPDATE iphone_capability_grants SET expires_at=? WHERE request_id=?",
            (now, request_id),
        )
        await append_audit_event(
            db,
            "iphone.capability.cancelled",
            {"request_id": request_id, "reason": reason},
            actor_type="control-plane",
            actor_id="migration",
            task_id=str(row["task_id"]),
            trace_id=str(row["task_id"]),
            created_at=now,
        )
        await OutboxService.enqueue_locked(
            db,
            aggregate_type="iphone_capability_request",
            aggregate_id=request_id,
            topic="agent.job.capability.result",
            event_type="capability_result",
            payload={"request_id": request_id, "status": "cancelled"},
            task_id=str(row["task_id"]),
            agent_id=str(row["requesting_agent_id"]),
            message_id=request_id,
            dedupe_key=f"iphone-capability:{request_id}:{dedupe_suffix}",
            created_at=now,
        )

    async def create_task(self, task: TaskRecord) -> TaskRecord:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._insert_task(db, task)
            await append_audit_event(
                db,
                "task.created",
                {"task_id": task.id, "source": task.source, "mode": task.mode.value},
                actor_type="device",
                actor_id=task.source,
                task_id=task.id,
                trace_id=task.id,
            )
            await db.commit()
        return task

    @staticmethod
    async def _insert_task(db: aiosqlite.Connection, task: TaskRecord) -> None:
        await db.execute(
            """
            INSERT INTO tasks(
                id,title,input,mode,source,conversation_id,status,priority,
                created_at,updated_at,completed_at,error_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task.id,
                task.title,
                task.input,
                task.mode.value,
                task.source,
                task.conversation_id,
                task.status.value,
                task.priority,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
                None,
                None,
            ),
        )

    async def get_task(self, task_id: str) -> TaskRecord | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))).fetchone()
        return self._task_from_row(row) if row else None

    async def list_tasks(self, limit: int = 100, status: str | None = None) -> list[TaskRecord]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if status:
                rows = await (
                    await db.execute(
                        "SELECT * FROM tasks WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                        (status, limit),
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
                    )
                ).fetchall()
        return [self._task_from_row(row) for row in rows]

    @staticmethod
    def _task_from_row(row: aiosqlite.Row) -> TaskRecord:
        value = dict(row)
        raw_error = value.get("error_json")
        value["error_json"] = json.loads(raw_error) if raw_error else None
        return TaskRecord.model_validate(value)

    async def update_task_status(
        self, task_id: str, status: str, *, error: str | None = None
    ) -> TaskRecord | None:
        now = datetime.now(UTC).isoformat()
        terminal = status in {"completed", "failed", "cancelled"}
        error_json = json.dumps({"message": error}) if error else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
            ).fetchone()
            if row is None:
                await db.rollback()
                return None
            current = str(row[0])
            if status not in TASK_TRANSITIONS.get(current, frozenset()):
                await db.rollback()
                raise StateConflict(f"task cannot transition from {current} to {status}")
            cursor = await db.execute(
                """
                UPDATE tasks SET status=?, updated_at=?, completed_at=?, error_json=?
                WHERE id=? AND status=?
                """,
                (status, now, now if terminal else None, error_json, task_id, current),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise StateConflict("task status changed during transition")
            await db.commit()
        return await self.get_task(task_id)

    async def cancel_task(
        self,
        task_id: str,
        *,
        actor_id: str,
        maintenance_guard: MaintenanceLeaseGuard | None = None,
    ) -> TaskRecord | None:
        now = datetime.now(UTC).isoformat()
        ordinarily_cancellable = {"created", "planned", "waiting_permission", "queued", "blocked"}
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            row = await (
                await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
            ).fetchone()
            if not row:
                await db.rollback()
                return None
            current = str(row["status"])
            remote_jobs = list(
                await (
                    await db.execute(
                        """
                        SELECT id,status,lease_generation FROM agent_jobs
                        WHERE task_id=? AND status IN ('queued','claimed','running')
                        """,
                        (task_id,),
                    )
                ).fetchall()
            )
            local_running = await (
                await db.execute(
                    "SELECT 1 FROM tool_calls WHERE task_id=? AND status='running' LIMIT 1",
                    (task_id,),
                )
            ).fetchone()
            remote_running_is_cancellable = (
                current == "running" and bool(remote_jobs) and local_running is None
            )
            if current not in ordinarily_cancellable and not remote_running_is_cancellable:
                await db.rollback()
                raise StateConflict(f"task cannot be cancelled from status {current}")
            try:
                await TaskStateMachine.transition_locked(
                    db,
                    task_id=task_id,
                    current=current,
                    target="cancelled",
                    now=now,
                )
            except DistributedStateConflict as exc:
                await db.rollback()
                raise StateConflict("task status changed while cancellation was requested") from exc
            await db.execute(
                "UPDATE approvals SET status='cancelled', decided_at=? WHERE task_id=? AND status='pending'",
                (now, task_id),
            )
            await db.execute(
                """
                UPDATE tool_calls SET status='cancelled', updated_at=?
                WHERE task_id=? AND status IN ('proposed','waiting_permission','queued')
                """,
                (now, task_id),
            )
            for job in remote_jobs:
                job_id = str(job["id"])
                job_status = str(job["status"])
                generation = int(job["lease_generation"])
                fenced_generation = generation + 1
                capability_requests = list(
                    await (
                        await db.execute(
                            """
                            SELECT id FROM iphone_capability_requests
                            WHERE requesting_job_id=? AND lease_generation=?
                              AND status NOT IN ('completed','denied','failed','cancelled','expired')
                            """,
                            (job_id, generation),
                        )
                    ).fetchall()
                )
                for capability_request in capability_requests:
                    request_id = str(capability_request["id"])
                    await db.execute(
                        """
                        UPDATE iphone_capability_requests SET status='cancelled',completed_at=?
                        WHERE id=?
                        """,
                        (now, request_id),
                    )
                    await db.execute(
                        "UPDATE iphone_capability_grants SET expires_at=? WHERE request_id=?",
                        (now, request_id),
                    )
                    await append_audit_event(
                        db,
                        "iphone.capability.cancelled",
                        {"request_id": request_id, "reason": "parent task cancelled"},
                        actor_type="device",
                        actor_id=actor_id,
                        task_id=task_id,
                        trace_id=task_id,
                        created_at=now,
                    )
                    await OutboxService.enqueue_locked(
                        db,
                        aggregate_type="iphone_capability_request",
                        aggregate_id=request_id,
                        topic="agent.job.capability.result",
                        event_type="capability_result",
                        payload={"request_id": request_id, "status": "cancelled"},
                        task_id=task_id,
                        message_id=request_id,
                        dedupe_key=f"iphone-capability:{request_id}:task-cancelled",
                        created_at=now,
                    )
                try:
                    await AgentJobStateMachine.transition_locked(
                        db,
                        job_id=job_id,
                        current=job_status,
                        target="cancelled",
                        now=now,
                        updates={
                            "claimed_by": None,
                            "claim_token": None,  # nosec B105 - revoke cancelled lease proof
                            "lease_id": None,
                            "lease_token_hash": None,  # nosec B105 - revoke cancelled lease proof
                            "lease_expires_at": None,
                            "lease_generation": fenced_generation,
                            "completed_at": now,
                            "last_failure_reason": "parent task cancelled",
                        },
                        extra_where=" AND task_id=? AND lease_generation=?",
                        where_values=(task_id, generation),
                    )
                except DistributedStateConflict as exc:
                    await db.rollback()
                    raise StateConflict("remote job changed while task was cancelled") from exc
                await append_audit_event(
                    db,
                    "agent.job.cancelled",
                    {"job_id": job_id, "lease_generation": fenced_generation},
                    actor_type="device",
                    actor_id=actor_id,
                    task_id=task_id,
                    trace_id=task_id,
                    created_at=now,
                )
                await OutboxService.enqueue_locked(
                    db,
                    aggregate_type="agent_job",
                    aggregate_id=job_id,
                    topic="tasks.status",
                    event_type="cancelled",
                    payload={
                        "job_id": job_id,
                        "status": "cancelled",
                        "lease_generation": fenced_generation,
                    },
                    task_id=task_id,
                    message_id=job_id,
                    dedupe_key=f"agent-job:{job_id}:cancelled:{fenced_generation}",
                    created_at=now,
                )
            await append_audit_event(
                db,
                "task.cancelled",
                {},
                actor_type="device",
                actor_id=actor_id,
                task_id=task_id,
                trace_id=task_id,
                created_at=now,
            )
            if maintenance_guard is not None:
                await maintenance_guard.require_current_locked(db)
            await db.commit()
        return await self.get_task(task_id)

    async def create_chat_task(
        self, content: str, conversation_id: str | None, mode: str, actor_id: str
    ) -> tuple[str, TaskRecord]:
        now = datetime.now(UTC).isoformat()
        conversation_id = conversation_id or f"cnv_{uuid4().hex}"
        request = TaskCreate(
            input=content,
            mode=TaskMode(mode),
            conversation_id=conversation_id,
        )
        task = TaskRecord.new(request, source=actor_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            conversation = await (
                await db.execute("SELECT id FROM conversations WHERE id=?", (conversation_id,))
            ).fetchone()
            if not conversation:
                await db.execute(
                    "INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                    (conversation_id, content.strip()[:80], now, now),
                )
            else:
                await db.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
                )
            await db.execute(
                """
                INSERT INTO messages(id,conversation_id,task_id,role,agent_id,content,metadata_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (f"msg_{uuid4().hex}", conversation_id, task.id, "user", None, content, None, now),
            )
            await self._insert_task(db, task)
            await append_audit_event(
                db,
                "task.created",
                {"source": "chat", "mode": mode},
                actor_type="device",
                actor_id=actor_id,
                task_id=task.id,
                trace_id=task.id,
                created_at=now,
            )
            await db.commit()
        return conversation_id, task

    async def append_chat_user_message(
        self, content: str, conversation_id: str | None, actor_id: str
    ) -> tuple[str, dict[str, Any]]:
        now = datetime.now(UTC).isoformat()
        conversation_id = conversation_id or f"cnv_{uuid4().hex}"
        record = {
            "id": f"msg_{uuid4().hex}",
            "conversation_id": conversation_id,
            "task_id": None,
            "role": "user",
            "agent_id": None,
            "content": content,
            "metadata": None,
            "created_at": now,
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute("SELECT id FROM conversations WHERE id=?", (conversation_id,))
            ).fetchone()
            if existing:
                await db.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
                )
            else:
                await db.execute(
                    "INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                    (conversation_id, content.strip()[:80], now, now),
                )
            await db.execute(
                """
                INSERT INTO messages(id,conversation_id,task_id,role,agent_id,content,metadata_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (record["id"], conversation_id, None, "user", None, content, None, now),
            )
            await append_audit_event(
                db,
                "chat.message.created",
                {},
                actor_type="device",
                actor_id=actor_id,
                trace_id=conversation_id,
                created_at=now,
            )
            await db.commit()
        return conversation_id, record

    async def append_conversation_message(
        self, conversation_id: str, role: str, content: str, *, agent_id: str | None = None
    ) -> dict[str, Any]:
        if role not in {"agent", "system"}:
            raise ValueError("unsupported conversation role")
        now = datetime.now(UTC).isoformat()
        record = {
            "id": f"msg_{uuid4().hex}",
            "conversation_id": conversation_id,
            "task_id": None,
            "role": role,
            "agent_id": agent_id,
            "content": content,
            "metadata": {"verified_status": "conversation_only"},
            "created_at": now,
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO messages(id,conversation_id,task_id,role,agent_id,content,metadata_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    record["id"],
                    conversation_id,
                    None,
                    role,
                    agent_id,
                    content,
                    json.dumps(record["metadata"]),
                    now,
                ),
            )
            await db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
            )
            await db.commit()
        return record

    async def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT c.*,
                      (SELECT content FROM messages m WHERE m.conversation_id=c.id
                       ORDER BY m.created_at DESC LIMIT 1) AS last_message
                    FROM conversations c ORDER BY c.updated_at DESC LIMIT ?
                    """,
                    (limit,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_messages(self, conversation_id: str, limit: int = 500) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """SELECT * FROM (SELECT * FROM messages WHERE conversation_id=?
                    ORDER BY created_at DESC,rowid DESC LIMIT ?) ORDER BY created_at ASC,id ASC""",
                    (conversation_id, limit),
                )
            ).fetchall()
        return [self._decode_json_fields(dict(row), ("metadata_json",)) for row in rows]

    async def list_messages_for_task(self, task_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM messages WHERE task_id=? ORDER BY created_at ASC", (task_id,)
                )
            ).fetchall()
        return [self._decode_json_fields(dict(row), ("metadata_json",)) for row in rows]

    async def list_recent_messages(self, limit: int = 500) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            ).fetchall()
        return [self._decode_json_fields(dict(row), ("metadata_json",)) for row in rows]

    async def append_task_message(
        self,
        task_id: str,
        role: str,
        content: str,
        *,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        task = await self.get_task(task_id)
        if not task or not task.conversation_id:
            return None
        now = datetime.now(UTC).isoformat()
        record = {
            "id": f"msg_{uuid4().hex}",
            "conversation_id": task.conversation_id,
            "task_id": task_id,
            "role": role,
            "agent_id": agent_id,
            "content": content,
            "metadata": metadata,
            "created_at": now,
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO messages(id,conversation_id,task_id,role,agent_id,content,metadata_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    record["id"],
                    task.conversation_id,
                    task_id,
                    role,
                    agent_id,
                    content,
                    json.dumps(metadata) if metadata is not None else None,
                    now,
                ),
            )
            await db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (now, task.conversation_id)
            )
            await db.commit()
        return record

    async def list_memory(self, limit: int = 200) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM memory_items ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                    (limit,),
                )
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    async def create_memory(self, request: MemoryCreate, actor_id: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        memory_id = f"mem_{uuid4().hex}"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO memory_items(
                    id,scope,kind,content,summary,sensitivity,confidence,pinned,
                    metadata_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    memory_id,
                    request.scope,
                    request.kind,
                    request.content,
                    request.summary,
                    request.sensitivity,
                    request.confidence,
                    int(request.pinned),
                    None,
                    now,
                    now,
                ),
            )
            await append_audit_event(
                db,
                "memory.remembered",
                {"memory_id": memory_id, "scope": request.scope},
                actor_type="device",
                actor_id=actor_id,
                created_at=now,
            )
            await db.commit()
        if self.embedding_service is not None:
            try:
                await self.index_memory(memory_id)
            except EmbeddingServiceError:
                pass
        record = await self.get_memory(memory_id)
        if record is None:
            raise RuntimeError("memory disappeared after creation")
        return record

    async def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM memory_items WHERE id=?", (memory_id,))
            ).fetchone()
        return self._memory_from_row(row) if row else None

    @staticmethod
    def _memory_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        value = StateService._decode_json_fields(dict(row), ("metadata_json",))
        value["pinned"] = bool(value["pinned"])
        return value

    async def search_memory(self, request: MemorySearch) -> list[dict[str, Any]]:
        terms = tuple({term.casefold() for term in request.query.split() if term.strip()})
        items = await self.list_memory(500)
        scored: list[dict[str, Any]] = []
        for item in items:
            if request.scope and item["scope"] != request.scope:
                continue
            if request.kind and item["kind"] != request.kind:
                continue
            haystack = f"{item['content']} {item.get('summary') or ''}".casefold()
            matches = sum(term in haystack for term in terms)
            if matches:
                scored.append({**item, "score": matches / len(terms), "search_kind": "lexical"})
        lexical = sorted(scored, key=lambda item: (-item["score"], not item["pinned"]))[:50]
        if self.embedding_service is None:
            return lexical
        try:
            vectors = await self.embedding_service.embed([request.query])
        except EmbeddingServiceError:
            return lexical
        if not vectors:
            return lexical
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (
                await db.execute(
                    "SELECT memory_id,vector_json FROM memory_embeddings WHERE provider=?",
                    (self.embedding_service.provider_name,),
                )
            ).fetchall()
        if not rows:
            return lexical
        query_vector = vectors[0]
        lexical_scores = {str(item["id"]): float(item["score"]) for item in lexical}
        item_by_id = {str(item["id"]): item for item in items}
        combined: list[dict[str, Any]] = []
        for memory_id, encoded in rows:
            candidate = item_by_id.get(str(memory_id))
            if (
                candidate is None
                or (request.scope and candidate["scope"] != request.scope)
                or (request.kind and candidate["kind"] != request.kind)
            ):
                continue
            vector = json.loads(str(encoded))
            dot = sum(a * b for a, b in zip(query_vector, vector, strict=False))
            qnorm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
            vnorm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vector_score = max(0.0, dot / (qnorm * vnorm))
            lexical_score = lexical_scores.get(str(memory_id), 0.0)
            combined.append(
                {
                    **candidate,
                    "score": 0.65 * vector_score + 0.35 * lexical_score,
                    "search_kind": "hybrid",
                }
            )
        return sorted(combined, key=lambda item: (-item["score"], not item["pinned"]))[:50]

    async def index_memory(self, memory_id: str) -> None:
        if self.embedding_service is None:
            return
        item = await self.get_memory(memory_id)
        if item is None:
            return
        vectors = await self.embedding_service.embed(
            [f"{item['content']} {item.get('summary') or ''}"]
        )
        if len(vectors) != 1 or not vectors[0]:
            raise ValueError("embedding provider returned an invalid vector")
        now = datetime.now(UTC).isoformat()
        await self._store_embedding(memory_id, vectors[0], now)

    async def _store_embedding(self, memory_id: str, vector: list[float], updated_at: str) -> None:
        if self.embedding_service is None:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO memory_embeddings(
                    memory_id,provider,dimensions,vector_json,updated_at
                ) VALUES(?,?,?,?,?)""",
                (
                    memory_id,
                    self.embedding_service.provider_name,
                    len(vector),
                    json.dumps(vector),
                    updated_at,
                ),
            )
            await db.commit()

    async def update_memory(
        self, memory_id: str, request: MemoryUpdate, actor_id: str
    ) -> dict[str, Any] | None:
        existing = await self.get_memory(memory_id)
        if not existing:
            return None
        now = datetime.now(UTC).isoformat()
        content = request.content if request.content is not None else existing["content"]
        summary = request.summary if request.summary is not None else existing["summary"]
        pinned = request.pinned if request.pinned is not None else existing["pinned"]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE memory_items SET content=?, summary=?, pinned=?, updated_at=? WHERE id=?",
                (content, summary, int(pinned), now, memory_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await append_audit_event(
                db,
                "memory.updated",
                {"memory_id": memory_id},
                actor_type="device",
                actor_id=actor_id,
                created_at=now,
            )
            await db.commit()
        if self.embedding_service is not None and request.content is not None:
            try:
                await self.index_memory(memory_id)
            except EmbeddingServiceError:
                pass
        return await self.get_memory(memory_id)

    async def delete_memory(self, memory_id: str, actor_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("DELETE FROM memory_items WHERE id=?", (memory_id,))
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await append_audit_event(
                db,
                "memory.deleted",
                {"memory_id": memory_id},
                actor_type="device",
                actor_id=actor_id,
                created_at=now,
            )
            await db.commit()
        return True

    async def list_agents(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT a.*,
                           (SELECT COUNT(*) FROM agent_jobs j
                            WHERE j.claimed_by=a.id
                              AND j.status IN ('claimed','running')) AS active_jobs,
                           COALESCE(s.score,0.0) AS historical_score
                    FROM agents a LEFT JOIN agent_scores s ON s.agent_id=a.id
                    ORDER BY a.created_at ASC
                    """
                )
            ).fetchall()
        return [self._agent_from_row(row) for row in rows]

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT a.*,
                           (SELECT COUNT(*) FROM agent_jobs j
                            WHERE j.claimed_by=a.id
                              AND j.status IN ('claimed','running')) AS active_jobs,
                           COALESCE(s.score,0.0) AS historical_score
                    FROM agents a LEFT JOIN agent_scores s ON s.agent_id=a.id
                    WHERE a.id=?
                    """,
                    (agent_id,),
                )
            ).fetchone()
        return self._agent_from_row(row) if row else None

    @staticmethod
    def _agent_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        value = dict(row)
        value["skills"] = json.loads(str(value.pop("skills_json")))
        value["capacity"] = json.loads(str(value.pop("capacity_json", "{}")))
        value.pop("auth_token_hash", None)
        value["agent_card"] = public_agent_card(value).model_dump(mode="json")
        return value

    async def register_agent(
        self,
        request: AgentCreate,
        actor_id: str,
        *,
        actor_type: Literal["device", "operator"] = "device",
    ) -> dict[str, Any]:
        if actor_type not in {"device", "operator"} or not actor_id:
            raise ValueError("worker registration requires an identified device or operator")
        policy = validate_agent_registration(request)
        agent_id = f"agt_{uuid4().hex}"
        credential = secrets.token_urlsafe(32)
        credential_hash = f"sha256:{hashlib.sha256(credential.encode('utf-8')).hexdigest()}"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat()
            try:
                policy_snapshot = await self.worker_skill_policy.load_locked(db, now=now)
            except PermissionPolicyError:
                await db.rollback()
                raise
            if policy_snapshot is not None and any(
                not policy_snapshot.is_allowed(skill) for skill in policy.skills
            ):
                await db.rollback()
                raise PermissionPolicyError("remote worker skill is denied by policy")
            await db.execute(
                """
                INSERT INTO agents(
                    id,name,version,endpoint,model_id,status,skills_json,
                    auth_token_hash,last_heartbeat_at,last_seen_at,max_concurrency,
                    capacity_json,runtime,supported_protocol_version,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    agent_id,
                    request.name,
                    request.version,
                    str(request.endpoint),
                    request.model_id,
                    "unverified",
                    json.dumps(policy.skills),
                    credential_hash,
                    None,
                    None,
                    request.max_concurrency,
                    json.dumps(
                        dict(policy.capability_metadata), separators=(",", ":"), sort_keys=True
                    ),
                    request.runtime,
                    policy.protocol,
                    now,
                    now,
                ),
            )
            await append_audit_event(
                db,
                "agent.registered",
                {"agent_id": agent_id},
                actor_type=actor_type,
                actor_id=actor_id,
                created_at=now,
            )
            await db.commit()
        record = await self.get_agent(agent_id)
        if record is None:
            raise RuntimeError("agent disappeared after registration")
        return {**record, "credential": credential}

    async def heartbeat_agent(
        self, agent_id: str, status: str, credential: str
    ) -> dict[str, Any] | None:
        if not credential:
            return None
        now = datetime.now(UTC).isoformat()
        credential_hash = f"sha256:{hashlib.sha256(credential.encode('utf-8')).hexdigest()}"
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE agents
                SET status=?,last_heartbeat_at=?,last_seen_at=?,updated_at=?
                WHERE id=? AND auth_token_hash=?
                """,
                (status, now, now, now, agent_id, credential_hash),
            )
            await db.commit()
        return await self.get_agent(agent_id) if cursor.rowcount == 1 else None

    async def create_feedback(self, request: FeedbackCreate, actor_id: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        feedback_id = f"fbk_{uuid4().hex}"
        record = {
            "id": feedback_id,
            **request.model_dump(),
            "payload": {},
            "created_at": now,
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO feedback_events(
                    id,task_id,agent_id,type,label,score,notes,payload_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    feedback_id,
                    request.task_id,
                    request.agent_id,
                    request.type,
                    request.label,
                    request.score,
                    request.notes,
                    "{}",
                    now,
                ),
            )
            if request.agent_id is not None and request.score is not None:
                await db.execute(
                    """INSERT INTO agent_scores(agent_id,sample_count,score,updated_at)
                    VALUES(?,1,?,?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                      score=((agent_scores.score * agent_scores.sample_count) + excluded.score)
                            / (agent_scores.sample_count + 1),
                      sample_count=agent_scores.sample_count + 1,
                      updated_at=excluded.updated_at""",
                    (request.agent_id, request.score, now),
                )
            await append_audit_event(
                db,
                "feedback.created",
                {"feedback_id": feedback_id, "score": request.score},
                actor_type="device",
                actor_id=actor_id,
                task_id=request.task_id,
                trace_id=request.task_id,
                created_at=now,
            )
            await db.commit()
        return record

    async def append_audit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor_type: str = "system",
        actor_id: str = "control-plane",
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            event = await append_audit_event(
                db,
                event_type,
                payload,
                actor_type=actor_type,
                actor_id=actor_id,
                task_id=task_id,
                trace_id=trace_id,
            )
            await db.commit()
        return event

    async def list_audit(
        self, limit: int = 50, after_id: int | None = None
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if after_id is None:
                rows = await (
                    await db.execute(
                        "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        "SELECT * FROM audit_events WHERE id>? ORDER BY id ASC LIMIT ?",
                        (after_id, limit),
                    )
                ).fetchall()
            events = [self._audit_from_row(row) for row in rows]
            tool_call_ids = list(
                dict.fromkeys(
                    tool_call_id
                    for event in events
                    if event.get("event_type") in PUBLIC_ERROR_AUDIT_EVENTS
                    for payload in [event.get("payload")]
                    if isinstance(payload, dict)
                    for tool_call_id in [payload.get("tool_call_id")]
                    if isinstance(tool_call_id, str) and tool_call_id
                )
            )
            process_tool_call_ids: set[str] = set()
            if tool_call_ids:
                placeholders = ",".join("?" for _ in tool_call_ids)
                process_rows = await (
                    await db.execute(
                        f"""
                        SELECT id FROM tool_calls
                        WHERE tool_name='process.run' AND id IN ({placeholders})
                        """,  # nosec B608
                        tool_call_ids,
                    )
                ).fetchall()
                process_tool_call_ids = {str(row[0]) for row in process_rows}
        return [self._public_audit_event(event, process_tool_call_ids) for event in events]

    @staticmethod
    def _audit_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    @staticmethod
    def _public_audit_event(
        event: dict[str, Any], process_tool_call_ids: set[str]
    ) -> dict[str, Any]:
        projected = dict(event)
        raw_payload = projected.get("payload")
        if not isinstance(raw_payload, dict):
            return projected
        payload = dict(raw_payload)
        tool_call_id = payload.get("tool_call_id")
        if (
            projected.get("event_type") in PUBLIC_ERROR_AUDIT_EVENTS
            and isinstance(tool_call_id, str)
            and tool_call_id in process_tool_call_ids
            and "error" in payload
        ):
            payload["error"] = PUBLIC_PROCESS_ERROR
        projected["payload"] = payload
        return projected

    async def bootstrap(self) -> dict[str, Any]:
        # Bootstrap is itself an authoritative read. Materialize lazy approval
        # expiry first so its task, approval, tool-call, and count snapshots agree.
        from app.services.approval_gateway import ApprovalGateway

        approval_gateway = ApprovalGateway(self.db_path)
        approvals = await approval_gateway.list_all(500)
        tasks = [task.model_dump(mode="json") for task in await self.list_tasks(500)]
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            calls = [
                dict(row)
                for row in await (
                    await db.execute("SELECT * FROM tool_calls ORDER BY created_at DESC LIMIT 500")
                ).fetchall()
            ]
            for call in calls:
                call["arguments"] = json.loads(call.pop("arguments_json"))
                raw_result = call.pop("result_json")
                call["result"] = json.loads(raw_result) if raw_result else None
            calls = [public_tool_call(call) for call in calls]
            goal_rows = await (
                await db.execute(
                    "SELECT * FROM goal_runs ORDER BY updated_at DESC,id ASC LIMIT 500"
                )
            ).fetchall()
            decoded_goals = [self._decode_goal_row(row) for row in goal_rows]
            goals = [public_goal(goal) for goal in decoded_goals]
            goal_by_id = {str(goal["id"]): goal for goal in decoded_goals}
            node_rows = await (
                await db.execute(
                    "SELECT * FROM plan_nodes ORDER BY updated_at DESC,id ASC LIMIT 1000"
                )
            ).fetchall()
            plan_nodes = [public_plan_node(self._decode_plan_node_row(row)) for row in node_rows]
            result_rows = await (
                await db.execute(
                    "SELECT goal_run_id,result_json FROM goal_results ORDER BY updated_at DESC LIMIT 500"
                )
            ).fetchall()
            goal_results: list[dict[str, Any]] = []
            for result_row in result_rows:
                goal = goal_by_id.get(str(result_row["goal_run_id"]))
                decoded = json.loads(str(result_row["result_json"]))
                if goal is not None and isinstance(decoded, dict):
                    goal_results.append(public_goal_result(decoded, goal=goal))
            cursor_row = await (
                await db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_events")
            ).fetchone()
            if cursor_row is None:
                raise RuntimeError("SQLite did not return the audit cursor")
            counts: dict[str, int] = {}
            count_queries = {
                "tasks": "SELECT COUNT(*) FROM tasks",
                "messages": "SELECT COUNT(*) FROM messages",
                "agents": "SELECT COUNT(*) FROM agents",
                "memory_items": "SELECT COUNT(*) FROM memory_items",
                "audit_events": "SELECT COUNT(*) FROM audit_events",
                "approvals_pending": "SELECT COUNT(*) FROM approvals WHERE status='pending'",
                "goals": "SELECT COUNT(*) FROM goal_runs",
            }
            for name, query in count_queries.items():
                count_row = await (await db.execute(query)).fetchone()
                if count_row is None:
                    raise RuntimeError(f"SQLite did not return the {name} count")
                counts[name] = int(count_row[0])
        return {
            "server_time": datetime.now(UTC).isoformat(),
            "tasks": tasks,
            "approvals": approvals,
            "tool_calls": calls,
            "conversations": await self.list_conversations(50),
            "messages": await self.list_recent_messages(500),
            "agents": await self.list_agents(),
            "pinned_memory": [item for item in await self.list_memory(200) if item["pinned"]],
            "goals": goals,
            "plan_nodes": plan_nodes,
            "goal_results": goal_results,
            "counts": counts,
            "cursor": str(cursor_row[0]),
        }

    @staticmethod
    def _decode_goal_row(row: aiosqlite.Row) -> dict[str, Any]:
        record = dict(row)
        record["completion_criteria"] = json.loads(str(record.pop("completion_criteria_json")))
        return record

    @staticmethod
    def _decode_plan_node_row(row: aiosqlite.Row) -> dict[str, Any]:
        record = dict(row)
        record["depends_on"] = json.loads(str(record.pop("depends_on_json")))
        record.pop("planner_metadata_json", None)
        return record

    @staticmethod
    def _decode_json_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        for field in fields:
            raw = value.pop(field, None)
            value[field.removesuffix("_json")] = json.loads(raw) if raw else None
        return value
