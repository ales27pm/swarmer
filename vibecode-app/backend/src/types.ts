// API contract — monGARS Swarm App control plane
// All app routes return { data: T }; errors return { error: { message, code } }.

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
  skills: string[];
  last_heartbeat_at: string | null;
  created_at: string;
}

export type RiskLevel = "low" | "medium" | "high";
export type ApprovalStatus = "pending" | "allowed" | "denied";

export interface Approval {
  id: string;
  task_id: string;
  agent_id: string | null;
  action_type: string;
  target: string | null;
  reason: string | null;
  risk: RiskLevel;
  status: ApprovalStatus;
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
}

export interface AuditEvent {
  id: string;
  trace_id: string;
  event_type: string;
  actor_type: string;
  actor_id: string;
  task_id: string | null;
  payload_json: string;
  prev_hash: string | null;
  hash: string | null;
  created_at: string;
}

export interface FeedbackEvent {
  id: string;
  task_id: string | null;
  agent_id: string | null;
  type: string;
  label: string | null;
  score: number | null;
  notes: string | null;
  created_at: string;
}

// Request bodies
export interface CreateTaskBody {
  input: string;
  mode?: TaskMode;
  source?: string;
  conversation_id?: string;
}

export interface ApprovalDecisionBody {
  decision: "allow_once" | "allow_rule" | "deny";
  user_note?: string;
}

export interface ChatBody {
  content: string;
  conversation_id?: string;
}

export interface MemorySearchBody {
  query: string;
  scope?: string;
  kind?: string;
}

export interface MemoryRememberBody {
  content: string;
  summary?: string;
  scope?: string;
  kind?: string;
  pinned?: boolean;
}
