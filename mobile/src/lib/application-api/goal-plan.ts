import { ApiError, ConnectionChangedError, createLocalGoalPlanSession, type GoalDetail, type GoalMemoryContext } from "@/lib/api/client";
import { buildLocalSwarmPlanPrompt, parseLocalSwarmPlan, type LocalSwarmPlanContext } from "@/lib/local-swarm-plan";
import type { LocalGenerationResult } from "@/lib/local-inference";
import { ApplicationApiError } from "./schema";

export type GoalPlanSession = Awaited<ReturnType<typeof createLocalGoalPlanSession>>;
export type GoalPlanSnapshot = { detail: GoalDetail; context: LocalSwarmPlanContext & { memory: GoalMemoryContext }; fingerprint: string };

export async function readInitialGoal(session: GoalPlanSession, goalId: string): Promise<GoalPlanSnapshot> {
  const [before, bootstrap] = await Promise.all([session.getGoal(goalId), session.bootstrapSync()]);
  await session.assertCurrent();
  const structurallyInitial = (detail: GoalDetail) => detail.goal.id === goalId
    && detail.goal.status === "planning" && !detail.goal.started_at
    && detail.goal.step_count === 0 && detail.goal.replan_count === 0
    && !detail.nodes.length && !detail.result;
  if (!structurallyInitial(before)) {
    throw new ApplicationApiError("invalid_state", "Ce but a déjà démarré ou changé. Consulte son état avant de préparer un plan initial.");
  }
  const memory = await session.memoryContext(goalId, before.goal.updated_at);
  // Retrieval can reserve one model call. Only the server's traced planning credits allow it.
  const detail = await session.getGoal(goalId);
  await session.assertCurrent();
  const goal = detail.goal;
  const logicalGoal = (value: GoalDetail["goal"]) => ({ ...value, updated_at: null, model_call_count: 0 });
  if (!structurallyInitial(detail) || !memory.local_planning_eligible
      || goal.model_call_count !== memory.planning_embedding_call_count
      || before.goal.model_call_count > goal.model_call_count
      || JSON.stringify(logicalGoal(before.goal)) !== JSON.stringify(logicalGoal(goal))
      || (before.goal.model_call_count === goal.model_call_count && before.goal.updated_at !== goal.updated_at)) {
    throw new ApplicationApiError("stale_context", "Le but a changé pendant la lecture mémoire ou n’est plus admissible à un plan initial. Consulte son état.");
  }
  const agents = bootstrap.agents.map((agent) => ({
    id: agent.id, status: agent.status, skills: [...agent.skills].sort(), model_id: agent.model_id,
    runtime: agent.runtime, supported_protocol_version: agent.supported_protocol_version,
  })).sort((left, right) => left.id.localeCompare(right.id));
  const context = { goal: {
    objective: goal.objective, completion_criteria: goal.completion_criteria,
    max_steps: goal.max_steps, step_count: goal.step_count, max_parallelism: goal.max_parallelism,
    max_model_calls: goal.max_model_calls, model_call_count: goal.model_call_count,
  }, agents, memory };
  return { detail, context, fingerprint: JSON.stringify({ goal, agents, memory: memory.context_fingerprint,
    provider: memory.provider_fingerprint }) };
}


export function assertGoalPlanSnapshotCurrent(current: GoalPlanSnapshot, reviewed: GoalPlanSnapshot): void {
  if (current.fingerprint !== reviewed.fingerprint) {
    throw new ApplicationApiError("stale_context", "Le but, la mémoire ou les capacités ont changé depuis la génération. Génère un nouveau plan avant de démarrer.");
  }
}

/** Only for stages that cannot send startGoal. Memory lookup may still reserve its own credit. */
export function localPlanPreparationError(cause: unknown, fallback: "context_unavailable" | "generation_failed"): ApplicationApiError {
  if (cause instanceof ApplicationApiError) return cause;
  if (cause instanceof ConnectionChangedError) {
    return new ApplicationApiError("connection_changed", "La connexion jumelée a changé pendant la préparation du plan. Rechargez son contexte.");
  }
  if (cause instanceof ApiError && cause.status < 500) {
    return new ApplicationApiError(cause.status === 401 ? "not_paired" : cause.status === 409 ? "conflict" : "server_rejected",
      "Le serveur a refusé la préparation du contexte du plan.", cause.status);
  }
  return new ApplicationApiError(fallback, fallback === "context_unavailable"
    ? "Le contexte du plan n’a pas pu être chargé. Aucun démarrage du but n’a été envoyé."
    : "La génération locale n’a pas produit de plan utilisable. Aucun démarrage du but n’a été envoyé.");
}

export function buildLocalGoalPlanPrompt(snapshot: GoalPlanSnapshot): string {
  try { return buildLocalSwarmPlanPrompt(snapshot.context); }
  catch { throw new ApplicationApiError("invalid_context", "Le contexte du but ne permet pas de construire un plan local valide. Actualisez le but et ses capacités."); }
}

function parseReviewedLocalGoalPlan(text: string, snapshot: GoalPlanSnapshot) {
  try { return parseLocalSwarmPlan(text, snapshot.context); }
  catch { throw new ApplicationApiError("invalid_plan", "La réponse du modèle est vide ou ne respecte pas le contrat JSON du plan local. Aucun plan n’a été accepté ni démarré."); }
}

export function parseCompletedLocalGoalPlan(result: LocalGenerationResult, snapshot: GoalPlanSnapshot) {
  if (result.finishReason !== "stop") {
    throw new ApplicationApiError(result.finishReason === "cancelled" ? "cancelled" : "generation_truncated",
      "La génération n’est pas complète ; aucun plan ne peut être soumis.");
  }
  return parseReviewedLocalGoalPlan(result.text, snapshot);
}

/** Shared UI/network start boundary. The caller owns its lifecycle and records attempted before POST. */
export async function startReviewedLocalGoalPlan(
  session: GoalPlanSession, goalId: string, reviewed: GoalPlanSnapshot, rawText: string,
  options: { shouldAccept?: () => boolean; assertReviewCurrent?: () => void; onAttempt?: () => void } = {},
): Promise<GoalDetail | null> {
  const snapshot = await readInitialGoal(session, goalId);
  if (options.shouldAccept && !options.shouldAccept()) return null;
  assertGoalPlanSnapshotCurrent(snapshot, reviewed);
  const plan = parseReviewedLocalGoalPlan(rawText, snapshot);
  await session.assertCurrent();
  if (options.shouldAccept && !options.shouldAccept()) return null;
  options.assertReviewCurrent?.();
  options.onAttempt?.();
  const detail = await session.startGoal(goalId, { plan_proposal: plan, planner_source: "iphone_local",
    memory_context_fingerprint: snapshot.context.memory.context_fingerprint });
  if (detail.goal.id !== goalId || detail.goal.planner_source !== "iphone_local") {
    throw new ApplicationApiError("outcome_unknown", "Le serveur n’a pas confirmé ce plan initial iPhone.");
  }
  return detail;
}
