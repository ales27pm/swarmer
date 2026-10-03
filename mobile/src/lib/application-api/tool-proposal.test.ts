import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { createLocalToolSubmissionSession, type LocalMemoryContext, type Task, type ToolProposalInput } from "@/lib/api/client";
import { prepareLocalToolProposal, submitReviewedToolProposal } from "./tool-proposal";

jest.mock("@/lib/api/client", () => ({ createLocalToolSubmissionSession: jest.fn() }));
jest.mock("@/lib/local-inference", () => ({ buildLocalProposalPrompt: (intent: string) => `Mandatory requirements: ${intent}` }));

type Session = Awaited<ReturnType<typeof createLocalToolSubmissionSession>>;
const task = { id: "task_local" } as Task;
const proposal: ToolProposalInput = { tool_name: "workspace.read_text", arguments: { path: "README.md" }, summary: "Lire" };
const context = (): LocalMemoryContext => ({ schema_version: "local-context-v1", enabled: true,
  purpose: "tool_proposal", goal_id: null, goal_updated_at: null, project_id: null, input_sha256: "a".repeat(64),
  symbolic_context: { schema_version: "symbolic-context-v1", status: "available", evidence: [], grants_authority: false },
  receipt: { id: "context_reviewed", context_sha256: "b".repeat(64), expires_at: "2099-01-01T00:00:00Z" },
});

describe("captured local tool preparation", () => {
  let session: { [Key in keyof Session]: jest.Mock<Session[Key]> };
  beforeEach(() => {
    jest.clearAllMocks();
    session = {
      assertCurrent: jest.fn<Session["assertCurrent"]>().mockResolvedValue(),
      localContext: jest.fn<Session["localContext"]>().mockResolvedValue(context()),
      createTask: jest.fn<Session["createTask"]>().mockImplementation(async (_intent, onTaskCreated) => {
        onTaskCreated?.(task); return { task, conversation_id: "conv_local" };
      }),
      submit: jest.fn<Session["submit"]>().mockResolvedValue({ id: "tool_local" } as never),
    };
    jest.mocked(createLocalToolSubmissionSession).mockResolvedValue(session);
  });

  it("prepares once and submits the reviewed receipt on the same captured connection", async () => {
    const preparation = await prepareLocalToolProposal("Inspecter sans modifier");
    expect(session.localContext).toHaveBeenCalledWith("Inspecter sans modifier", 16_384);
    expect(session.createTask).not.toHaveBeenCalled();
    // A fresh connection would be wrong even if it happened to reach the same server.
    jest.mocked(createLocalToolSubmissionSession).mockRejectedValue(new Error("must not recapture"));
    await submitReviewedToolProposal("Inspecter sans modifier", {
      ...proposal, local_context_receipt: { id: "unreviewed", context_sha256: "c".repeat(64) },
    }, undefined, undefined, preparation);
    expect(createLocalToolSubmissionSession).toHaveBeenCalledTimes(1);
    expect(session.submit).toHaveBeenCalledWith(task.id, { ...proposal,
      local_context_receipt: { id: "context_reviewed", context_sha256: "b".repeat(64) },
    }, expect.any(Function));
  });

  it("requires preparation and refuses an edited intent before creating a task", async () => {
    await expect(submitReviewedToolProposal("Inspecter", proposal)).rejects.toMatchObject({ code: "stale_context" });
    const preparation = await prepareLocalToolProposal("Inspecter");
    await expect(submitReviewedToolProposal("Modifier", proposal, undefined, undefined, preparation))
      .rejects.toMatchObject({ code: "stale_context" });
    expect(session.createTask).not.toHaveBeenCalled();
    expect(session.submit).not.toHaveBeenCalled();
  });

  it("does not silently treat unavailable context as disabled", async () => {
    session.localContext.mockRejectedValue(new Error("API outdated"));
    await expect(prepareLocalToolProposal("Inspecter")).rejects.toThrow("API outdated");
    expect(session.createTask).not.toHaveBeenCalled();
  });

  it("keeps explicit disabled context paired without inventing a receipt", async () => {
    session.localContext.mockResolvedValue({ ...context(), enabled: false, symbolic_context: null, receipt: null });
    const preparation = await prepareLocalToolProposal("Inspecter");
    await submitReviewedToolProposal("Inspecter", { ...proposal,
      local_context_receipt: { id: "injected", context_sha256: "c".repeat(64) },
    }, undefined, undefined, preparation);
    expect(session.submit).toHaveBeenCalledWith(task.id, proposal, expect.any(Function));
  });

  it("refuses expired reviewed evidence before creating a task", async () => {
    const preparation = await prepareLocalToolProposal("Inspecter");
    const now = jest.spyOn(Date, "now").mockReturnValue(Date.parse("2100-01-01T00:00:00Z"));
    try {
      await expect(submitReviewedToolProposal("Inspecter", proposal, undefined, undefined, preparation))
        .rejects.toMatchObject({ code: "stale_context" });
      expect(session.createTask).not.toHaveBeenCalled();
    } finally { now.mockRestore(); }
  });

  it("retains the known task when context expires during creation and never sends the proposal", async () => {
    const preparation = await prepareLocalToolProposal("Inspecter");
    const known = jest.fn();
    const now = jest.spyOn(Date, "now");
    session.createTask.mockImplementation(async (_intent, onTaskCreated) => {
      now.mockReturnValue(Date.parse("2100-01-01T00:00:00Z"));
      onTaskCreated?.(task); return { task, conversation_id: "conv_local" };
    });
    try {
      await expect(submitReviewedToolProposal("Inspecter", proposal, known, undefined, preparation))
        .rejects.toMatchObject({ code: "stale_context" });
      expect(known).toHaveBeenCalledWith(task);
      expect(session.submit).not.toHaveBeenCalled();
    } finally { now.mockRestore(); }
  });

  it("isolates and freezes the reviewed context instead of retaining mutable response references", async () => {
    const received = context();
    session.localContext.mockResolvedValue(received);
    const preparation = await prepareLocalToolProposal("Inspecter");
    received.receipt!.id = "replaced_after_prepare";
    expect(preparation.context.receipt!.id).toBe("context_reviewed");
    expect(Object.isFrozen(preparation)).toBe(true);
    expect(Object.isFrozen(preparation.context.symbolic_context!.evidence)).toBe(true);
    expect(Object.isFrozen(preparation.context.receipt)).toBe(true);
  });

  it("refuses a changed pairing during task creation without a second submit", async () => {
    const preparation = await prepareLocalToolProposal("Inspecter");
    session.createTask.mockImplementation(async () => {
      session.assertCurrent.mockRejectedValue(new Error("pairing changed"));
      return { task, conversation_id: "conv_local" };
    });
    await expect(submitReviewedToolProposal("Inspecter", proposal, undefined, undefined, preparation)).rejects.toThrow("pairing changed");
    expect(session.submit).not.toHaveBeenCalled();
  });
});
