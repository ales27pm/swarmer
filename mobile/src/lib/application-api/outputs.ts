/** Versioned response references. A TypeScript transport type is not a runtime DTO validator. */
export type CommandOutputDescriptor = {
  contractVersion: "1.0";
  envelope: "ApplicationResult";
  dataType: string;
  definition: string;
  validation: "existing_parser" | "bounded_json" | "unavailable";
};

const types = {
  "memory.status": "JsonValue", "context.inspect": "JsonValue", "context.compact": "JsonValue", "context.source": "JsonValue",
  "embeddings.status": "JsonValue", "embeddings.load": "JsonValue", "embeddings.generate": "JsonValue", "embeddings.unload": "null",
  "app.status": "AppStatus", "connection.status": "ConnectionStatus", "sync.refresh": "SyncSummary",
  "connection.pair": "PairingSummary", "connection.origin": "string", "sync.status": "ApplicationSyncState",
  "goals.list": "GoalRecord[]", "goals.get": "GoalDetail", "goals.nodes": "PlanNode[]", "goals.result": "GoalResult|null",
  "goals.writing-draft": "GoalWritingDraft",
  "goals.create": "GoalDetail", "goals.start": "GoalDetail", "goals.replan": "GoalDetail", "goals.cancel": "GoalDetail",
  "goals.messages": "GoalConversation", "goals.feedback": "JsonValue", "goals.conversation.open": "ConversationHandle",
  "goals.reply.prepare": "ReplyHandle", "goals.reply.send": "GoalDetail", "goals.plan.prepare": "LocalPlanContextHandle",
  "goals.plan.generate": "GeneratedLocalPlan", "goals.plan.start": "GoalDetail",
  "tasks.list": "Task[]", "tasks.get": "TaskDetail", "tasks.create": "Task", "tasks.plan": "ToolCall|PlanningResult", "tasks.cancel": "Task",
  "chat.messages": "Message[]", "chat.send": "ChatReceipt", "approvals.list": "Approval[]",
  "approvals.decide": "ApprovalDecisionReceipt",
  "memory.list": "MemoryItem[]", "memory.search": "MemoryItem[]", "memory.create": "MemoryItem", "memory.update": "MemoryItem", "memory.delete": "null",
  "agents.list": "Agent[]", "activities.catalog": "ActivityCatalog", "feedback.create": "FeedbackReceipt",
  "audit.summary": "AuditSummary[]", "cache.summary": "CacheSummary",
  "outbox.status": "OutboxSummary", "outbox.drain": "MutationDrainResult", "models.capabilities": "LocalInferenceCapabilities",
  "models.list": "LocalModel[]", "models.status": "LocalInferenceStatus", "models.presets": "Record<LocalInferenceRuntime,LocalModelPreset>",
  "models.load": "LocalInferenceStatus", "models.unload": "null", "models.download": "LocalModel", "models.download.cancel": "null",
  "models.import": "LocalModel", "iphone.requests.list": "CapabilityRequestPreview[]", "iphone.requests.get": "CapabilityRequestDetail",
  "iphone.requests.decide": "CapabilityDecisionSummary", "iphone.requests.execute": "CapabilityResult",
  "inference.generate": "LocalGenerationResult", "inference.cancel": "null", "settings.local.read": "LocalModelSettings|null", "settings.local.update": "null",
  "inference.proposal.generate": "GeneratedToolProposal", "tasks.proposal.submit": "ToolProposalReceipt", "tasks.proposal.status": "ToolProposalStatus",
  "code.review": "CodeReviewHandle", "code.prepareApproval": "CodeProposalApplication", "project.review": "ProjectReviewHandle", "project.prepareApproval": "CodeProposalApplication",
} as const;

const parsed = new Set(["activities.catalog", "goals.writing-draft", "goals.messages", "goals.conversation.open", "models.capabilities", "models.list", "models.status", "models.load", "models.download", "models.import", "inference.generate", "code.review", "project.review", "code.prepareApproval", "project.prepareApproval", "settings.local.read", "goals.plan.generate", "iphone.requests.list", "iphone.requests.get", "iphone.requests.decide", "iphone.requests.execute"]);
export function outputDescriptor(command: string, available = true): CommandOutputDescriptor {
  return {
    contractVersion: "1.0", envelope: "ApplicationResult", dataType: Object.hasOwn(types, command) ? types[command as keyof typeof types] : "unavailable",
    definition: "mobile/src/lib/application-api/outputs.ts#ApplicationDataByCommand",
    validation: available ? parsed.has(command) ? "existing_parser" : "bounded_json" : "unavailable",
  };
}

// Custom projections supplement the existing API/native domain DTOs referenced above.
export type AppStatus = {
  appVersion: string | null; appVersionSource: "expo_config"; nativeBuildNumber: string | null; configuredBuildNumber: string | null;
  platform: import("react-native").PlatformOSType; appState: import("react-native").AppStateStatus;
  pairedCredentialStored: boolean; localInferenceAvailable: boolean; localInference?: import("@/lib/local-inference").LocalInferenceStatus;
};
export type AuditSummary = Pick<import("@/lib/api/types").AuditEvent, "id" | "event_type" | "created_at" | "hash">;
export type CacheSummary = {
  available: boolean; cursor: string | null; counts: { goals: number; nodes: number; results: number; agents: number } | null;
  authorizesSensitiveActions: false;
};
export type ConnectionStatus = { credentialStored: boolean };
export type SyncSummary = Pick<import("@/lib/api/types").Bootstrap, "counts" | "cursor">;
export type ConversationHandle = { handle: string; conversation: import("@/lib/api/project").GoalConversation };
export type ReplyHandle = { handle: string; clientMessageId: string };
export type CodeReviewHandle = { handle: string; proposal: import("@/lib/api/code-proposal").GoalCodeProposal };
export type ProjectReviewHandle = { handle: string; project: import("@/lib/api/project").ProjectPreview };
export type LocalPlanContextHandle = { handle: string; goal: import("@/lib/api/types").GoalRecord; memory: import("@/lib/api/types").GoalMemoryContext; agents: import("./goal-plan").GoalPlanSnapshot["context"]["agents"] };
export type GeneratedLocalPlan = { handle: string; plan: import("@/lib/api/types").SwarmPlanProposal; tokenCount: number; finishReason: "stop"; model: Pick<import("@/lib/local-inference").LocalInferenceStatus, "runtime" | "modelId" | "revision"> };
export type ChatReceipt = Awaited<ReturnType<typeof import("@/lib/api/client").sendChat>>;
export type PlanningResult = Awaited<ReturnType<typeof import("@/lib/api/client").planTask>>;
export type FeedbackReceipt = { id: string };
export type OutboxSummary = { pending: number };

type OutputTypes = {
  AppStatus: AppStatus; ConnectionStatus: ConnectionStatus; SyncSummary: SyncSummary;
  "AuditSummary[]": AuditSummary[]; CacheSummary: CacheSummary;
  PairingSummary: SyncSummary & { serverUrl: string };
  ApplicationSyncState: ReturnType<typeof import("./sync-state").readApplicationSyncState>;
  ConversationHandle: ConversationHandle; ReplyHandle: ReplyHandle; CodeReviewHandle: CodeReviewHandle;
  ProjectReviewHandle: ProjectReviewHandle; LocalPlanContextHandle: LocalPlanContextHandle; GeneratedLocalPlan: GeneratedLocalPlan;
  ChatReceipt: ChatReceipt; FeedbackReceipt: FeedbackReceipt; OutboxSummary: OutboxSummary;
  "GoalRecord[]": import("@/lib/api/types").GoalRecord[];
  GoalDetail: import("@/lib/api/types").GoalDetail;
  GoalWritingDraft: import("@/lib/api/writing-draft").GoalWritingDraft;
  "PlanNode[]": import("@/lib/api/types").PlanNode[];
  "GoalResult|null": import("@/lib/api/types").GoalResult | null;
  GoalConversation: import("@/lib/api/project").GoalConversation;
  "Task[]": import("@/lib/api/types").Task[];
  Task: import("@/lib/api/types").Task;
  TaskDetail: import("@/lib/api/types").TaskDetail;
  "ToolCall|PlanningResult": PlanningResult;
  "Message[]": import("@/lib/api/types").Message[];
  "Approval[]": import("@/lib/api/types").Approval[];
  ApprovalDecisionReceipt: import("@/lib/api/client").ApprovalDecisionReceipt;
  "CapabilityRequestPreview[]": import("@/lib/iphone-capabilities/types").CapabilityRequestPreview[];
  CapabilityRequestDetail: import("@/lib/iphone-capabilities/types").CapabilityRequestDetail;
  CapabilityDecisionSummary: { status: import("@/lib/iphone-capabilities/types").CapabilityAuthorizationResponse["status"] };
  CapabilityResult: import("@/lib/iphone-capabilities/types").CapabilityResult;
  "MemoryItem[]": import("@/lib/api/types").MemoryItem[];
  MemoryItem: import("@/lib/api/types").MemoryItem;
  "Agent[]": import("@/lib/api/types").Agent[];
  ActivityCatalog: import("@/lib/api/activity-catalog").ActivityCatalog;
  MutationDrainResult: import("@/lib/state/mutation-outbox").MutationDrainResult;
  LocalInferenceCapabilities: import("@/lib/local-inference").LocalInferenceCapabilities;
  "LocalModel[]": import("@/lib/local-inference").LocalModel[];
  LocalModel: import("@/lib/local-inference").LocalModel;
  LocalInferenceStatus: import("@/lib/local-inference").LocalInferenceStatus;
  LocalGenerationResult: import("@/lib/local-inference").LocalGenerationResult;
  "Record<LocalInferenceRuntime,LocalModelPreset>": Record<import("@/lib/local-inference").LocalInferenceRuntime, import("@/lib/local-model-presets").LocalModelPreset>;
  "LocalModelSettings|null": import("@/lib/local-model-settings").LocalModelSettings | null;
  CodeProposalApplication: import("@/lib/api/code-proposal").CodeProposalApplication;
  GeneratedToolProposal: { handle: string | null; proposal: import("@/lib/local-inference").LocalToolProposal; tokenCount: number; finishReason: "stop" };
  ToolProposalReceipt: Awaited<ReturnType<typeof import("./tool-proposal").submitReviewedToolProposal>>;
  ToolProposalStatus: { state: "review" | "sending" | "submitted" | "uncertain"; taskId: string | null };
  JsonValue: import("./schema").JsonValue;
  string: string;
  null: null;
};

export type ApplicationDataByCommand = { [Name in keyof typeof types]: OutputTypes[(typeof types)[Name]] };
