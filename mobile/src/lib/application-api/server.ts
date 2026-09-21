/** Typed UI facade. HTTP projects opaque handles from the same bound review/consent services. */
import * as adapter from "@/lib/api/client";
import { invokeApplicationCommand } from "./registry";
import type { PairApplicationConnectionInput } from "./connection";

export type {
  Agent, Approval, ApprovalDecisionResult, AuditEvent, Bootstrap, GoalAutonomyProfile, GoalCreateInput,
  GoalDetail, GoalFeedbackInput, GoalMemoryContext, GoalNodeStatus, GoalRecord, GoalResult, GoalStatus,
  GoalStartInput, MemoryItem, Message, PlanNode, SwarmPlanProposal, SwarmPlanNodeProposal,
  Task, TaskDetail, TaskMode, TaskStatus, ToolCall, ToolProposalInput,
} from "@/lib/api/types";
export type { GoalCodeProposal, GoalCodeProposalReview } from "@/lib/api/code-proposal";
export type { GoalWritingDraft } from "@/lib/api/writing-draft";
export type { GoalConversationSession, GoalReplyAttempt, ProjectReview, ProjectPreview } from "@/lib/api/project";
export type { ApprovalDecisionReceipt, PairingResult } from "@/lib/api/client";
// Private-session construction is not a raw network endpoint.
export { ApiError, ConnectionChangedError, createLocalGoalPlanSession } from "@/lib/api/client";
export const pairConnection = (input: PairApplicationConnectionInput): Promise<adapter.PairingResult> => invokeApplicationCommand("connection.pair", input);
export const getServerUrl: typeof adapter.getServerUrl = () => call("connection.origin");
export const hasDeviceToken: typeof adapter.hasDeviceToken = async () => (await call<{ credentialStored: boolean }>("connection.status")).credentialStored;

function call<T>(name: string, input: Record<string, unknown> = {}): Promise<T> {
  return invokeApplicationCommand(name, Object.fromEntries(Object.entries(input).filter(([, value]) => value !== undefined)));
}
export const createTask: typeof adapter.createTask = (input, mode) => call("tasks.create", { input, mode });
export const listTasks: typeof adapter.listTasks = (status) => call("tasks.list", { status });
export const getTask: typeof adapter.getTask = (id) => call("tasks.get", { id });
export const cancelTask: typeof adapter.cancelTask = (id) => call("tasks.cancel", { id });
export const planTask: typeof adapter.planTask = (id) => call("tasks.plan", { id });
export const createGoal: typeof adapter.createGoal = (input) => call("goals.create", input);
export const cancelGoal: typeof adapter.cancelGoal = (id) => call("goals.cancel", { id });
export const replanGoal: typeof adapter.replanGoal = (id, reason) => call("goals.replan", { id, reason });
// Optional iPhone plans stay with the original opaque-session route; remote callers cannot supply a fabricated plan.
export const startGoal: typeof adapter.startGoal = (id, input) => input ? adapter.startGoal(id, input) : call("goals.start", { id });
export const createGoalFeedback: typeof adapter.createGoalFeedback = (id, feedback) => call("goals.feedback", { id, feedback });
export const sendChat: typeof adapter.sendChat = (content, conversationId, mode, startTask) => call("chat.send", { content, conversationId, mode, startTask });
export const listApprovals: typeof adapter.listApprovals = (status) => call("approvals.list", { status });
export const decideApproval: typeof adapter.decideApproval = (id, decision) => call("approvals.decide", { id, decision, confirm: true });
export const listMemory: typeof adapter.listMemory = () => call("memory.list");
export const searchMemory: typeof adapter.searchMemory = (query) => call("memory.search", { query });
export const rememberMemory: typeof adapter.rememberMemory = (input) => call("memory.create", input);
export const updateMemory: typeof adapter.updateMemory = (id, input) => call("memory.update", { id, ...input });
export const deleteMemory: typeof adapter.deleteMemory = (id) => call("memory.delete", { id });
export const listAgents: typeof adapter.listAgents = () => call("agents.list");
export const getActivityCatalog: typeof adapter.getActivityCatalog = () => call("activities.catalog");
export const listAudit: typeof adapter.listAudit = (limit) => call("audit.summary", { limit });
export const createFeedback: typeof adapter.createFeedback = (input) => call("feedback.create", input);
export const getGoalConversation: typeof adapter.getGoalConversation = (id) => call("goals.conversation.open", { id });
export const reviewGoalCodeProposal: typeof adapter.reviewGoalCodeProposal = (id, nodeId) => call("code.review", { id, nodeId });
export const reviewGoalProject: typeof adapter.reviewGoalProject = (id) => call("project.review", { id });

// These callbacks fence React refresh epochs and must still reach the underlying service.
export const listGoals: typeof adapter.listGoals = (shouldAccept) => invokeApplicationCommand("goals.list", {}, { shouldAccept });
export const getGoal: typeof adapter.getGoal = (id, shouldAccept) => invokeApplicationCommand("goals.get", { id }, { shouldAccept });
export const listGoalNodes: typeof adapter.listGoalNodes = (id, shouldAccept) => invokeApplicationCommand("goals.nodes", { id }, { shouldAccept });
export const getGoalResult: typeof adapter.getGoalResult = (id, shouldAccept) => invokeApplicationCommand("goals.result", { id }, { shouldAccept });
export const getGoalWritingDraft: typeof adapter.getGoalWritingDraft = (goalId, nodeId, workerJobId, shouldAccept) => invokeApplicationCommand("goals.writing-draft", { goalId, nodeId, workerJobId }, { shouldAccept });
export const listMessages: typeof adapter.listMessages = (conversationId, shouldAccept) => invokeApplicationCommand("chat.messages", { conversationId }, { shouldAccept });
export const bootstrapSync: typeof adapter.bootstrapSync = (shouldAccept) => invokeApplicationCommand("sync.refresh", {}, { shouldAccept });
