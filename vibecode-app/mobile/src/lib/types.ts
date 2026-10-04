// API contract — mirrors backend/src/types.ts
export type TaskMode = "normal" | "commandant" | "review" | "autonome";
export type TaskStatus =
  | "queued"
  | "running"
  | "waiting_permission"
  | "completed"
  | "failed"
  | "cancelled";

export interface Task {
  id: string;
  conversation_id: string | null;
  title: string | null;
  input: string;
  mode: TaskMode;
  status: TaskStatus;
  priority: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  error_json: string | null;
}

export interface Message {
  id: string;
  conversation_id: string;
  task_id: string | null;
  role: "user" | "agent" | "system";
  agent_id: string | null;
  content: string;
  metadata_json: string | null;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_message?: string | null;
}

export interface Agent {
  id: string;
  name: string;
  version: string;
  endpoint: string;
  model_id: string | null;
  status: "online" | "offline" | "busy";
  skills_json: string;
  last_heartbeat_at: string | null;
  created_at: string;
}

export type RiskLevel = "low" | "medium" | "high";

export interface Approval {
  id: string;
  task_id: string;
  agent_id: string | null;
  action_type: string;
  target: string | null;
  reason: string | null;
  risk: RiskLevel;
  status: "pending" | "allowed" | "denied";
  request_json: string;
  decision_json: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface MemoryItem {
  id: string;
  scope: string;
  kind: string;
  content: string;
  summary: string | null;
  sensitivity: string;
  confidence: number;
  pinned: number;
  created_at: string;
  updated_at: string;
  score?: number;
}

export interface AuditEvent {
  id: string;
  trace_id: string;
  event_type: string;
  actor_type: string;
  actor_id: string;
  task_id: string | null;
  payload_json: string;
  hash: string | null;
  created_at: string;
}

export interface Bootstrap {
  server_time: string;
  counts: {
    tasks: number;
    messages: number;
    agents: number;
    approvals_pending: number;
    memory_items: number;
    audit_events: number;
  };
  agents: Agent[];
  pinned_memory: MemoryItem[];
}

export interface TaskDetail {
  task: Task;
  messages: Message[];
  approvals: Approval[];
}
