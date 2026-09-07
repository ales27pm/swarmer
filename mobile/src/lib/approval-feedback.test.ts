import { describe, expect, it } from "@jest/globals";

import { approvalDecisionFeedback } from "@/lib/approval-feedback";
import type { ApprovalDecisionResult } from "@/lib/api/types";

describe("approvalDecisionFeedback", () => {
  it("fails closed when a malformed completed call has no verified result", () => {
    const malformedResult = {
      approval: {},
      tool_call: {
        status: "completed",
        result: null,
        error: null,
      },
    } as unknown as ApprovalDecisionResult;

    expect(approvalDecisionFeedback(malformedResult, "approve")).toEqual({
      notice: "Autorisation unique consommée; aucun résultat d’exécution vérifié n’a été retourné.",
      error: "L’appel lié annonce un statut terminé sans résultat vérifié.",
    });
  });

  it("retains the verified notice for a completed call with an object result", () => {
    const completedResult = {
      approval: {},
      tool_call: {
        status: "completed",
        result: { bytes: 12 },
        error: null,
      },
    } as unknown as ApprovalDecisionResult;

    expect(approvalDecisionFeedback(completedResult, "approve")).toEqual({
      notice: "Autorisation unique consommée; un résultat d’exécution vérifié a été enregistré.",
      error: null,
    });
  });
});
