import type { ApprovalDecisionResult } from "@/lib/api/types";

export type ApprovalDecisionFeedback = {
  notice: string;
  error: string | null;
};

export function approvalDecisionFeedback(
  result: ApprovalDecisionResult,
  decision: "approve" | "deny",
): ApprovalDecisionFeedback {
  if (decision === "deny") {
    return {
      notice: "Autorisation refusée; l’appel lié ne sera pas exécuté.",
      error: null,
    };
  }
  if (!("tool_call" in result)) {
    return {
      notice: "Autorisation enregistrée; aucun résultat d’exécution lié n’a été retourné.",
      error: null,
    };
  }

  const toolCall = result.tool_call;
  if (["failed", "rejected", "denied", "cancelled"].includes(toolCall.status)) {
    return {
      notice: "Autorisation unique consommée; l’exécution n’a pas réussi.",
      error: toolCall.error
        ? `Échec de l’appel lié: ${toolCall.error}`
        : `L’appel lié s’est terminé avec le statut ${toolCall.status}.`,
    };
  }
  if (toolCall.status === "completed") {
    const publicResult: unknown = toolCall.result;
    if (
      publicResult === null ||
      typeof publicResult !== "object" ||
      Array.isArray(publicResult)
    ) {
      return {
        notice: "Autorisation unique consommée; aucun résultat d’exécution vérifié n’a été retourné.",
        error: "L’appel lié annonce un statut terminé sans résultat vérifié.",
      };
    }
    return {
      notice: "Autorisation unique consommée; un résultat d’exécution vérifié a été enregistré.",
      error: null,
    };
  }
  return {
    notice: `Autorisation unique consommée; état actuel de l’appel lié: ${toolCall.status}.`,
    error: null,
  };
}
