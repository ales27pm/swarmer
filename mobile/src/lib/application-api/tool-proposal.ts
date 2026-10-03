import { createLocalToolSubmissionSession, type LocalMemoryContext, type Task, type ToolCall, type ToolProposalInput } from "@/lib/api/client";
import { symbolicPromptBudget } from "@/lib/api/local-memory-context";
import { buildLocalProposalPrompt } from "@/lib/local-inference";
import { ApplicationApiError } from "./schema";

export type LocalToolPreparation = {
  intent: string;
  session: Awaited<ReturnType<typeof createLocalToolSubmissionSession>>;
  context: LocalMemoryContext;
};

/** Keep the exact reviewed JSON independent of mutable HTTP/test response objects. */
export function freezeLocalMemoryContext(context: LocalMemoryContext): LocalMemoryContext {
  const copy: LocalMemoryContext = JSON.parse(JSON.stringify(context));
  const freeze = (value: unknown): void => {
    if (value !== null && typeof value === "object") {
      Object.values(value).forEach(freeze);
      Object.freeze(value);
    }
  };
  freeze(copy);
  return copy;
}

export function assertLocalMemoryContextFresh(context: LocalMemoryContext): void {
  if (context.enabled && (!context.receipt || !Number.isFinite(Date.parse(context.receipt.expires_at))
      || Date.parse(context.receipt.expires_at) <= Date.now())) {
    throw new ApplicationApiError("stale_context", "Le contexte mémoire relu a expiré. Génère une nouvelle proposition avant de la soumettre.");
  }
}

/** Capture pairing and server evidence before inference, including explicitly disabled context. */
export async function prepareLocalToolProposal(intent: string): Promise<LocalToolPreparation> {
  const basePrompt = buildLocalProposalPrompt(intent);
  const session = await createLocalToolSubmissionSession();
  await session.assertCurrent();
  const context = freezeLocalMemoryContext(await session.localContext(intent, symbolicPromptBudget(basePrompt)));
  await session.assertCurrent();
  if (context.purpose !== "tool_proposal" || context.goal_id !== null || context.project_id !== null) {
    throw new ApplicationApiError("invalid_context", "Le contexte reçu ne correspond pas à cette proposition locale.");
  }
  assertLocalMemoryContextFresh(context);
  return Object.freeze({ intent, session, context });
}

/** The same two-stage, non-retrying submission is used by the screen and opaque API review. */
export async function submitReviewedToolProposal(
  intent: string, proposal: ToolProposalInput, onTaskCreated?: (task: Task) => void, assertReviewCurrent?: () => void,
  preparation?: LocalToolPreparation,
): Promise<{ task: Task; toolCall: ToolCall }> {
  if (!preparation || preparation.intent !== intent) {
    throw new ApplicationApiError("stale_context", "La proposition doit être générée avec le contexte de cette demande avant soumission.");
  }
  const { session, context } = preparation;
  const assertCurrent = () => {
    assertLocalMemoryContextFresh(context);
    assertReviewCurrent?.();
  };
  await session.assertCurrent();
  assertCurrent();
  const chat = await session.createTask(intent, onTaskCreated, assertCurrent);
  if (!chat.task) throw new Error("Le serveur n’a pas créé la tâche demandée.");
  await session.assertCurrent();
  assertCurrent();
  const boundProposal: ToolProposalInput = { ...proposal };
  delete boundProposal.local_context_receipt;
  if (context.receipt) boundProposal.local_context_receipt = {
    id: context.receipt.id, context_sha256: context.receipt.context_sha256,
  };
  const toolCall = await session.submit(chat.task.id, boundProposal, assertCurrent);
  return { task: chat.task, toolCall };
}
