import { createLocalToolSubmissionSession, type Task, type ToolCall, type ToolProposalInput } from "@/lib/api/client";

/** The same two-stage, non-retrying submission is used by the screen and opaque API review. */
export async function submitReviewedToolProposal(
  intent: string, proposal: ToolProposalInput, onTaskCreated?: (task: Task) => void, assertReviewCurrent?: () => void,
): Promise<{ task: Task; toolCall: ToolCall }> {
  const session = await createLocalToolSubmissionSession();
  const chat = await session.createTask(intent, onTaskCreated, assertReviewCurrent);
  if (!chat.task) throw new Error("Le serveur n’a pas créé la tâche demandée.");
  const toolCall = await session.submit(chat.task.id, proposal, assertReviewCurrent);
  return { task: chat.task, toolCall };
}
