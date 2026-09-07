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
  search_kind?: "lexical";
};

export type Agent = {
  id: string;
  name: string;
  version: string;
  endpoint: string;
  model_id: string | null;
  status: "online" | "offline" | "busy" | "unverified";
  skills: string[];
  last_heartbeat_at: string | null;
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
  agents: Agent[];
  pinned_memory: MemoryItem[];
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
