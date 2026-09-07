import { approvalDecisionFeedback } from "@/lib/approval-feedback";
import {
  ApiError,
  decideApproval,
  type ApprovalDecisionResult,
} from "@/lib/api/client";

export type ApprovalSubmissionOutcome = {
  conflict: boolean;
  localReplicaError: string | null;
  notice: string | null;
  primaryError: string | null;
  settled: boolean;
  taskId: string | null;
};

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

function resultTaskId(result: ApprovalDecisionResult): string {
  return "approval" in result ? result.approval.task_id : result.task_id;
}

export async function submitApprovalDecision(
  approvalId: string,
  decision: "approve" | "deny",
  lockApproval: (approvalId: string) => void,
  conflictMessage = "Cette autorisation a déjà été décidée, a expiré ou a été annulée.",
): Promise<ApprovalSubmissionOutcome> {
  let settled = false;
  try {
    const receipt = await decideApproval(approvalId, decision);
    lockApproval(approvalId);
    settled = true;
    const result = receipt.authoritativeResult;
    const feedback = approvalDecisionFeedback(result, decision);
    return {
      conflict: false,
      localReplicaError: receipt.localReplicaError,
      notice: feedback.notice,
      primaryError: feedback.error,
      settled,
      taskId: resultTaskId(result),
    };
  } catch (cause) {
    const conflict = cause instanceof ApiError && cause.status === 409;
    if (conflict) lockApproval(approvalId);
    return {
      conflict,
      localReplicaError: null,
      notice: null,
      primaryError: conflict ? conflictMessage : errorMessage(cause),
      settled: settled || conflict,
      taskId: null,
    };
  }
}

export function approvalDecisionError(
  outcome: ApprovalSubmissionOutcome,
  refreshError: string | null,
  labels: { refreshFailure: string; refreshed: string },
): string | null {
  const replicaError = outcome.localReplicaError
    ? `La décision a été enregistrée par le serveur, mais la copie locale n’a pas pu être actualisée : ${outcome.localReplicaError}.`
    : null;
  if (refreshError) {
    const context = [outcome.primaryError, replicaError].filter(Boolean).join(" ");
    const primary = context ? `${context} ` : "";
    const locked = outcome.settled ? " La carte reste verrouillée." : "";
    return `${primary}${labels.refreshFailure} : ${refreshError}${locked}`;
  }
  if (outcome.conflict) return `${outcome.primaryError} ${labels.refreshed}`;
  return [outcome.primaryError, replicaError].filter(Boolean).join(" ") || null;
}
