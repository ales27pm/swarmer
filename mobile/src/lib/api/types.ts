export type TaskMode = "normal" | "commandant" | "review" | "autonome";

export type TaskStatus =
  | "created"
  | "planned"
  | "waiting_permission"
  | "queued"
  | "running"
  | "blocked"
  | "completed"
  | "failed"
  | "cancelled";

export type Task = {
  id: string;
  title: string;
  input: string;
  mode: TaskMode;
  source: string;
  conversation_id: string | null;
  status: TaskStatus;
  priority: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  error_json: { message?: string } | null;
};

type ToolCallStatus =
  | "waiting_permission"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "denied"
  | "cancelled";

type ToolCallBase = {
  id: string;
  task_id: string;
  risk: "low" | "medium" | "high";
  approval_id: string | null;
  created_at: string;
  updated_at: string;
};

type RedactedByteCount = `<redacted: ${number} UTF-8 bytes>`;
type PublicWriteByteCount = RedactedByteCount | "<redacted: unknown UTF-8 bytes>";

type ToolCallState<TResult extends object, TError extends string = string> =
  | {
      status: "completed";
      result: TResult;
      error: TError | null;
    }
  | {
      status: Exclude<ToolCallStatus, "completed">;
      result: TResult | null;
      error: TError | null;
    };

type ListDirectoryToolCall = ToolCallBase &
  {
    tool_name: "workspace.list_dir";
    summary: "List a workspace directory";
    arguments: { path: string };
  } & ToolCallState<Record<string, unknown>>;

type ReadTextToolCall = ToolCallBase &
  {
    tool_name: "workspace.read_text";
    summary: "Read a workspace file";
    arguments: { path: string };
  } & ToolCallState<Record<string, unknown>>;

type WriteToolCall = ToolCallBase &
  {
    tool_name: "workspace.write_text";
    summary: "Write text to a workspace file";
    arguments: {
      path: string;
      content: PublicWriteByteCount;
      arguments_redacted: true;
    };
  } & ToolCallState<Record<string, unknown>>;

type ProcessToolCall = ToolCallBase &
  {
    tool_name: "process.run";
    summary: "Run a sandboxed process";
    arguments: {
      argv: [] | ["<redacted>"];
      argument_count: number;
      arguments_redacted: true;
    };
  } & ToolCallState<
    {
      output_redacted: true;
      returncode?: number;
      stdout?: RedactedByteCount;
      stderr?: RedactedByteCount;
      stdout_truncated?: boolean;
      stderr_truncated?: boolean;
      sandbox?: "bubblewrap";
      network?: "denied";
    },
    "sandboxed process failed; detailed error retained locally"
  >;

export type ToolCall =
  | ListDirectoryToolCall
  | ReadTextToolCall
  | WriteToolCall
  | ProcessToolCall;

export type ToolProposalInput =
  | {
      tool_name: "workspace.list_dir";
      arguments: { path: string };
      summary: string;
    }
  | {
      tool_name: "workspace.read_text";
      arguments: { path: string };
      summary: string;
    }
  | {
      tool_name: "workspace.write_text";
      arguments: { path: string; content: string };
      summary: string;
    }
  | {
      tool_name: "process.run";
      arguments: {
        argv: string[];
        cwd?: string;
        timeout_seconds?: number;
      };
      summary: string;
    };

export type PlannerSource = "iphone_local" | "ubuntu_local" | "manual" | "test";

export type GoalStatus =
  | "planning"
  | "running"
  | "waiting_permission"
  | "completed"
  | "failed"
  | "cancelled"
  | "budget_exhausted";

export type GoalNodeStatus =
  | "planned"
  | "ready"
  | "dispatched"
  | "running"
  | "waiting_permission"
  | "waiting_capability"
  | "completed"
  | "failed"
  | "blocked"
  | "cancelled"
  | "skipped";

export type GoalAutonomyProfile = "manual" | "assisted" | "autonomous";

export type GoalRecord = {
  id: string;
  root_task_id: string;
  objective: string;
  status: GoalStatus;
  autonomy_profile: GoalAutonomyProfile;
  planner_source: PlannerSource;
  max_steps: number;
  max_parallelism: number;
  max_replans: number;
  max_runtime_seconds: number;
  max_model_calls: number;
  step_count: number;
  replan_count: number;
  model_call_count: number;
  completion_criteria: string[];
  current_phase: string;
  evaluator_status?: string | null;
  evaluator_summary?: string | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  failure_reason?: string | null;
};

export type PlanNode = {
  id: string;
  goal_run_id: string;
  parent_node_id?: string | null;
  node_type: "worker" | "synthesis";
  title: string;
  objective: string;
  required_skill?: string | null;
  status: GoalNodeStatus;
  priority: number;
  depends_on: string[];
  assigned_agent_id?: string | null;
  worker_job_id?: string | null;
  task_id?: string | null;
  expected_output?: string | null;
  result_summary?: string | null;
  error_summary?: string | null;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
};

export type GoalResult = {
  goal_run_id: string;
  root_task_id: string;
  status: GoalStatus;
  answer: string;
  completed_nodes: string[];
  failed_nodes: string[];
  agents_used: string[];
  memory_ids: string[];
  episode_ids: string[];
  started_at: string;
  completed_at: string;
  limitations: string[];
};

export type GoalDetail = {
  goal: GoalRecord;
  nodes: PlanNode[];
  result: GoalResult | null;
};

export type GoalCreateInput = {
  objective: string;
  autonomy_profile: GoalAutonomyProfile;
  completion_criteria?: string[];
  max_steps?: number;
  max_parallelism?: number;
  max_replans?: number;
  max_runtime_seconds?: number;
  max_model_calls?: number;
};

export type GoalFeedbackInput = {
  score: number;
  note?: string;
  corrected_final_answer?: string;
  corrected_plan_summary?: string;
};

type ApprovalRequester = {
  type: "device";
  id: string;
  name: string;
};

type ApprovalPolicy = {
  rule_id: string;
  decision: "ask";
  reason: string;
};

type ApprovalBase = {
  id: string;
  task_id: string;
  tool_call_id: string;
  action_digest: string;
  binding_valid: boolean;
  risk: "low" | "medium" | "high";
  status: "pending" | "approved" | "denied" | "expired" | "cancelled";
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  decision: { decision: string; actor_id: string; user_note: string | null } | null;
};

type WorkspaceApprovalPreview = {
  operation: string;
  target: string;
  working_directory?: never;
  command?: never;
  details: [string, ...string[]];
  arguments_redacted: boolean;
};

type WriteApprovalPreview = Omit<WorkspaceApprovalPreview, "arguments_redacted"> & {
  arguments_redacted: true;
};

type ProcessApprovalPreview = {
  operation: string;
  target: string;
  working_directory: string;
  command: [string, ...string[]];
  details: [string, ...string[]];
  arguments_redacted: boolean;
};

type ApprovalAction =
  | {
      action: "workspace.list_dir";
      summary: "List a workspace directory";
      action_preview: WorkspaceApprovalPreview;
    }
  | {
      action: "workspace.read_text";
      summary: "Read a workspace file";
      action_preview: WorkspaceApprovalPreview;
    }
  | {
      action: "workspace.write_text";
      summary: "Write text to a workspace file";
      action_preview: WriteApprovalPreview;
    }
  | {
      action: "process.run";
      summary: "Run a sandboxed process";
      action_preview: ProcessApprovalPreview;
    };

export type Approval = ApprovalBase &
  ApprovalAction &
  (
    | {
        requester: ApprovalRequester;
        policy: ApprovalPolicy;
        affected_data_summary: string;
        audit_id: number;
        consent_context_valid: true;
      }
    | {
        requester: ApprovalRequester | null;
        policy: ApprovalPolicy | null;
        affected_data_summary: string | null;
        audit_id: number | null;
        consent_context_valid: false;
      }
  );

export type ApprovalDecisionResult =
  | Approval
  | { approval: Approval; tool_call: ToolCall };

export type Message = {
  id: string;
  conversation_id: string;
  task_id: string | null;
  role: "user" | "agent" | "system";
  agent_id: string | null;
  content: string;
  metadata: Record<string, unknown> | null;
  created_at: string;
};

export type Conversation = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_message: string | null;
};

export type MemoryItem = {
  id: string;
  scope: string;
  kind: string;
  content: string;
  summary: string | null;
  sensitivity: string;
  confidence: number;
  pinned: boolean;
  metadata: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  score?: number;
  search_kind?: "lexical" | "vector" | "hybrid";
};

export type Agent = {
  id: string;
  name: string;
  version: string;
  endpoint: string;
  model_id: string | null;
  status: "online" | "offline" | "busy" | "draining" | "unverified";
  skills: string[];
  last_heartbeat_at: string | null;
  last_seen_at: string | null;
  max_concurrency: number;
  capacity: Record<string, number>;
  runtime: "python";
  supported_protocol_version: "mongars-worker-v0.9";
  active_jobs: number;
  historical_score: number;
  agent_card: {
    agent_id: string;
    name: string;
    version: string;
    skills: string[];
    model_id: string | null;
    runtime: "python";
    max_concurrency: number;
    supported_protocol_version: "mongars-worker-v0.9";
    capabilities: Record<string, number>;
  };
  created_at: string;
  updated_at: string;
};

export type AuditEvent = {
  id: number;
  trace_id: string | null;
  event_type: string;
  actor_type: string | null;
  actor_id: string | null;
  task_id: string | null;
  payload: Record<string, unknown>;
  prev_hash: string | null;
  hash: string | null;
  created_at: string;
};

export type TaskDetail = {
  task: Task;
  messages: Message[];
  approvals: Approval[];
  tool_calls: ToolCall[];
};

export type Bootstrap = {
  server_time: string;
  tasks: Task[];
  approvals: Approval[];
  tool_calls: ToolCall[];
  conversations: Conversation[];
  messages?: Message[];
  agents: Agent[];
  pinned_memory: MemoryItem[];
  goals?: GoalRecord[];
  plan_nodes?: PlanNode[];
  goal_results?: GoalResult[];
  counts: {
    tasks: number;
    messages: number;
    agents: number;
    approvals_pending: number;
    memory_items: number;
    audit_events: number;
  };
  cursor: string;
};
