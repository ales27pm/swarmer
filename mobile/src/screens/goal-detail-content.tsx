import { ProjectGraphEvidence, ProjectGraphNodeEvidence, ProjectGraphPlan, useProjectGraph } from "@/components/project-graph";
import { ProjectRequirementEvidence, useProjectEvidence } from "@/components/project-requirement-evidence";
import { ActivityTimeline } from "@/components/activity-timeline";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { Alert, Modal, Pressable, ScrollView, Text, View } from "react-native";

import { useSafeAreaInsets } from "react-native-safe-area-context";

import { ScreenShell } from "@/components/screen-shell";
import { GoalCodeProposalReview } from "@/components/goal-code-proposal";
import { GoalWritingDraft } from "@/components/goal-writing-draft";
import { GoalConversation } from "@/components/goal-conversation";
import { GoalProjectReview } from "@/components/goal-project-review";
import {
  ActionButton,
  Card,
  COLORS,
  EmptyState,
  ErrorBanner,
  SectionTitle,
} from "@/components/swarm-ui";
import {
  cancelGoal,
  createGoalFeedback,
  getGoal,
  getServerUrl,
  replanGoal,
  startGoal,
  type GoalDetail,
  type GoalNodeStatus,
  type GoalStatus,
  type PlanNode,
} from "@/lib/application-api/server";
import { subscribeConnectionChanges } from "@/lib/connection-events";
import { localGoalDetail } from "@/lib/state/replica";
import { useLiveRefresh } from "@/lib/sync/live-sync-context";

const GOAL_LABELS: Record<GoalStatus, string> = {
  planning: "Planification",
  running: "En cours",
  waiting_permission: "Permission requise",
  completed: "Terminé",
  failed: "Échoué",
  cancelled: "Annulé",
  budget_exhausted: "Budget épuisé",
};

const PLANNING_PHASES: Record<string, { label: string; description: string }> = {
  awaiting_local_plan: {
    label: "Plan iPhone attendu",
    description: "Cette suite conserve le projet et attend le plan initial de l’iPhone. Reprenez la planification locale pour lire sa mémoire et préparer les agents.",
  },
  waiting_for_workers: {
    label: "En attente d’un agent",
    description: "Aucun agent d’exécution n’est en ligne. Connectez un agent d’exécution, puis réessayez. Aucun appel modèle n’est lancé pendant cette attente.",
  },
  planner_invalid_response: {
    label: "Plan proposé invalide",
    description: "La réponse du planificateur ne permet pas de créer un plan valide. Réessayez la planification.",
  },
  planner_request_rejected: {
    label: "Demande de planification refusée",
    description: "Le service de planification a refusé la demande. Réessayez après vérification de ce service.",
  },
  planner_invalid_context: {
    label: "Contexte de planification invalide",
    description: "Le contexte nécessaire à la planification est incomplet ou invalide. Réessayez après sa correction.",
  },
  planner_unavailable: {
    label: "Planificateur indisponible",
    description: "Le service de planification est indisponible. Réessayez lorsqu’il est rétabli.",
  },
};

const RUNNING_PHASES: Record<string, { label: string; description: string }> = {
  evaluator_unavailable: {
    label: "Évaluateur indisponible",
    description: "Le service d’évaluation est indisponible.",
  },
  evaluator_request_rejected: {
    label: "Demande d’évaluation refusée",
    description: "Le service d’évaluation a refusé la demande.",
  },
  evaluator_invalid_response: {
    label: "Évaluation reçue invalide",
    description: "La réponse reçue ne permet pas de valider le résultat.",
  },
  evaluator_retrying: {
    label: "Nouvelle évaluation",
    description: "Le serveur prépare une nouvelle évaluation avec le budget restant.",
  },
  project_continue: {
    label: "Poursuite du projet",
    description: "Le projet nécessite une nouvelle étape de construction ou de vérification.",
  },
  project_building: {
    label: "Construction du projet",
    description: "L’agent construit ou vérifie le projet par étapes. Les fichiers et les résultats disponibles peuvent être consultés dans la révision du projet.",
  },
};

const EVALUATOR_COOLDOWN_PHASES = new Set([
  "evaluator_unavailable", "evaluator_request_rejected", "evaluator_invalid_response",
]);

function runningPhase(goal: GoalDetail["goal"]) {
  const phase = RUNNING_PHASES[goal.current_phase];
  if (!phase) return undefined;
  if (EVALUATOR_COOLDOWN_PHASES.has(goal.current_phase)) {
    const instruction = goal.autonomy_profile === "manual"
      ? "Utilisez « Réessayer l’évaluation » pour lancer une nouvelle tentative avec le budget restant."
      : "Une nouvelle tentative automatique devient possible après un délai d’au moins 60 secondes, dans la limite du budget restant.";
    return { ...phase, description: `${phase.description} ${instruction}` };
  }
  if (goal.current_phase === "project_continue") {
    const instruction = goal.autonomy_profile === "manual"
      ? "Utilisez « Continuer le but » pour lancer l’étape suivante avec le budget restant."
      : "Le travail peut reprendre automatiquement dans la limite du budget restant.";
    return { ...phase, description: `${phase.description} ${instruction}` };
  }
  return phase;
}

const EVALUATOR_RETRY_PHASE = {
  label: "Évaluation à réessayer",
  description: "L’évaluation est en pause. Vous pouvez la réessayer ou préciser la demande dans la conversation. Le budget déjà utilisé est conservé.",
};

const CODE_PROPOSAL_PHASE = {
  label: "Code prêt à relire",
  description: "Examinez le code proposé, puis préparez la demande d’autorisation d’écriture. Le code n’a pas été exécuté.",
};

function awaitsEvaluatorRetry(goal: GoalDetail["goal"] | null | undefined) {
  return goal?.status === "waiting_permission" && goal.current_phase === "evaluator_retry_required";
}

function hasEvaluatorRetryAction(goal: GoalDetail["goal"]) {
  return awaitsEvaluatorRetry(goal) || (
    goal.status === "running" && goal.autonomy_profile === "manual"
    && EVALUATOR_COOLDOWN_PHASES.has(goal.current_phase)
  );
}

function goalLabel(goal: GoalDetail["goal"]) {
  if (awaitsEvaluatorRetry(goal)) return EVALUATOR_RETRY_PHASE.label;
  if (goal.status === "running" && RUNNING_PHASES[goal.current_phase]) return RUNNING_PHASES[goal.current_phase].label;
  if (goal.status === "waiting_permission" && goal.current_phase === "needs_user") return "Intervention attendue";
  if (goal.status === "waiting_permission" && goal.current_phase === "project_ready") return "Projet prêt à relire";
  return goal.status === "planning" && goal.current_phase === "waiting_for_workers"
    ? PLANNING_PHASES.waiting_for_workers.label
    : GOAL_LABELS[goal.status];
}

const NODE_LABELS: Record<GoalNodeStatus, string> = {
  planned: "Planifié",
  ready: "Prêt",
  dispatched: "Distribué",
  running: "En cours",
  waiting_permission: "Permission requise",
  waiting_capability: "Capacité iPhone requise",
  completed: "Terminé",
  failed: "Échoué",
  blocked: "Bloqué",
  cancelled: "Annulé",
  skipped: "Ignoré",
};

const NODE_COLORS: Partial<Record<GoalNodeStatus, string>> = {
  blocked: COLORS.warning,
  cancelled: COLORS.danger,
  completed: COLORS.accent,
  dispatched: COLORS.info,
  failed: COLORS.danger,
  ready: COLORS.info,
  running: COLORS.info,
  waiting_capability: COLORS.warning,
  waiting_permission: COLORS.warning,
};

const ACTIVE_NODE_STATUSES = new Set<GoalNodeStatus>(["running"]);
const CANCELLABLE_GOAL_STATUSES = new Set<GoalStatus>([
  "planning",
  "running",
  "waiting_permission",
]);
const REPLANNABLE_GOAL_STATUSES = new Set<GoalStatus>(["running", "waiting_permission"]);

type DetailSource = "authoritative" | "cache" | null;

type GoalDetailState = {
  detail: GoalDetail | null;
  source: DetailSource;
  initialLoading: boolean;
  refreshing: boolean;
  busy: string | null;
  feedbackLocked: boolean;
  notice: string | null;
  error: string | null;
};

export type GoalDetailNavigation = {
  openApprovals: () => void;
  openTask: (taskId: string) => void;
  openGoal: (goalId: string) => void;
  openLocalPlan?: (goalId: string) => void;
};

export type GoalDetailController = GoalDetailState & {
  goal: GoalDetail["goal"] | null;
  nodes: PlanNode[];
  result: GoalDetail["result"];
  completedCount: number;
  blockedCount: number;
  runningAgents: string[];
  online: boolean;
  refresh: (clearError?: boolean) => Promise<void>;
  confirmCancel: () => void;
  start: () => Promise<void>;
  replan: () => Promise<void>;
  submitFeedback: (score: number) => Promise<void>;
};

type LoadResult = Pick<GoalDetailState, "detail" | "source"> & { error?: string };

const INITIAL_STATE: GoalDetailState = {
  detail: null,
  source: null,
  initialLoading: true,
  refreshing: false,
  busy: null,
  feedbackLocked: false,
  notice: null,
  error: null,
};

function mergeState(state: GoalDetailState, patch: Partial<GoalDetailState>): GoalDetailState {
  return { ...state, ...patch };
}

function messageFor(cause: unknown) {
  return cause instanceof Error ? cause.message : String(cause);
}

async function loadCachedDetail(
  goalId: string,
  message: string,
  isCurrent: () => boolean,
): Promise<LoadResult | null> {
  const scope = await getServerUrl();
  const cached = await localGoalDetail(scope, goalId);
  const currentScope = await getServerUrl();
  if (!isCurrent() || scope !== currentScope) return null;
  return {
    detail: cached,
    source: cached ? "cache" : null,
    error: cached ? `Hors ligne — copie locale en lecture seule. ${message}` : message,
  };
}

async function loadGoalDetail(
  goalId: string,
  isCurrent: () => boolean,
): Promise<LoadResult | null> {
  try {
    const detail = await getGoal(goalId, isCurrent);
    return isCurrent() ? { detail, source: "authoritative" } : null;
  } catch (cause) {
    if (!isCurrent()) return null;
    const message = messageFor(cause);
    try {
      return await loadCachedDetail(goalId, message, isCurrent);
    } catch {
      return isCurrent() ? { detail: null, source: null, error: message } : null;
    }
  }
}

function deriveGoalState(state: GoalDetailState) {
  const nodes = state.detail?.nodes ?? [];
  const runningAgents = [...new Set(
    nodes
      .filter((node) => ACTIVE_NODE_STATUSES.has(node.status))
      .map((node) => node.assigned_agent_id)
      .filter((agentId): agentId is string => Boolean(agentId)),
  )];
  return {
    goal: state.detail?.goal ?? null,
    nodes,
    result: state.detail?.result ?? null,
    completedCount: nodes.filter((node) => node.status === "completed").length,
    blockedCount: nodes.filter((node) => node.status === "blocked" || node.status === "failed").length,
    runningAgents,
    online: state.source === "authoritative" && !state.refreshing,
  };
}

function canMutate(state: GoalDetailState) {
  return state.source === "authoritative" && !state.busy && !state.refreshing;
}

function canStartGoal(goal: GoalDetail["goal"] | null | undefined) {
  if (goal?.current_phase === "awaiting_local_plan") return false;
  return goal?.status === "planning"
    || (goal?.autonomy_profile === "manual" && goal.status === "running")
    || awaitsEvaluatorRetry(goal);
}

function canSubmitFeedback(goalId: string | undefined, state: GoalDetailState, locked: boolean) {
  return Boolean(
    goalId
      && state.detail?.result
      && state.source === "authoritative"
      && !state.refreshing
      && !locked,
  );
}

export function useGoalDetailController(goalId: string | undefined): GoalDetailController {
  const refreshEpoch = useRef(0);
  const feedbackLockedRef = useRef(false);
  const connectionEpoch = useRef(0);
  const requiresConnectionRefresh = useRef(false);
  const [state, update] = useReducer(mergeState, INITIAL_STATE);
  const stateRef = useRef(state);
  stateRef.current = state;
  useEffect(() => subscribeConnectionChanges(() => {
    connectionEpoch.current += 1;
    refreshEpoch.current += 1;
    requiresConnectionRefresh.current = true;
    update({ source: stateRef.current.detail ? "cache" : null, refreshing: false, busy: null,
      notice: "Le jumelage a changé. Le dernier état reste en lecture seule ; actualisez pour vérifier ce projet sur la nouvelle connexion." });
  }), []);

  const refresh = useCallback(async (clearError = true) => {
    if (requiresConnectionRefresh.current && !clearError) return;
    const reconnecting = requiresConnectionRefresh.current && clearError;
    if (clearError) requiresConnectionRefresh.current = false;
    const epoch = ++refreshEpoch.current;
    const isCurrent = () => refreshEpoch.current === epoch;
    if (!goalId) {
      update({ initialLoading: false });
      return;
    }
    update(clearError ? { refreshing: true, error: null, ...(reconnecting ? { notice: null } : {}) } : { refreshing: true });
    try {
      const loaded = await loadGoalDetail(goalId, isCurrent);
      if (isCurrent() && loaded) update(loaded);
    } finally {
      if (isCurrent()) update({ refreshing: false, initialLoading: false });
    }
  }, [goalId]);

  useEffect(() => {
    void refresh(false);
    return () => {
      refreshEpoch.current += 1;
    };
  }, [refresh]);
  useLiveRefresh(() => refresh(false));

  const runAction = useCallback(async (
    action: string,
    operation: () => Promise<GoalDetail>,
  ) => {
    if (!canMutate(state) || requiresConnectionRefresh.current) return;
    const connection = connectionEpoch.current;
    const current = () => connection === connectionEpoch.current;
    update({ busy: action, error: null, notice: null });
    try {
      await operation();
    } catch (cause) {
      if (current()) update({ error: messageFor(cause) });
    } finally {
      if (current()) { await refresh(false); if (current()) update({ busy: null }); }
    }
  }, [refresh, state]);

  const start = useCallback(async () => {
    if (goalId && canStartGoal(state.detail?.goal)) {
      await runAction("start", () => startGoal(goalId));
    }
  }, [goalId, runAction, state.detail?.goal]);

  const replan = useCallback(async () => {
    if (goalId) await runAction("replan", () => replanGoal(goalId));
  }, [goalId, runAction]);

  const confirmCancel = useCallback(() => {
    if (!goalId || !canMutate(state)) return;
    Alert.alert(
      "Annuler ce but?",
      "Les preuves déjà enregistrées resteront consultables. Cette action ne sera jamais rejouée hors ligne.",
      [
        { text: "Garder le but", style: "cancel" },
        {
          text: "Annuler le but",
          style: "destructive",
          onPress: () => void runAction("cancel", () => cancelGoal(goalId)),
        },
      ],
    );
  }, [goalId, runAction, state]);

  const submitFeedback = useCallback(async (score: number) => {
    if (!canSubmitFeedback(goalId, state, feedbackLockedRef.current)) return;
    if (requiresConnectionRefresh.current) return;
    const connection = connectionEpoch.current;
    feedbackLockedRef.current = true;
    update({ feedbackLocked: true, error: null });
    try {
      await createGoalFeedback(goalId as string, { score });
      if (connection === connectionEpoch.current) update({ notice: "Feedback enregistré pour les évaluations futures." });
    } catch (cause) {
      if (connection === connectionEpoch.current) update({ error: `${messageFor(cause)} Le feedback incertain n’est pas renvoyé automatiquement.` });
    }
  }, [goalId, state]);

  return {
    ...state,
    ...deriveGoalState(state),
    refresh,
    confirmCancel,
    start,
    replan,
    submitFeedback,
  };
}

function NodeMetadata({ node }: { node: PlanNode }) {
  return (
    <>
      {node.required_skill ? (
        <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Compétence : {node.required_skill}</Text>
      ) : null}
      {node.assigned_agent_id ? (
        <Text selectable style={{ color: COLORS.subtle, fontSize: 12 }}>
          Agent : {node.assigned_agent_id}
        </Text>
      ) : null}
      {node.expected_output ? (
        <Text selectable style={{ color: COLORS.muted }}>Sortie attendue : {node.expected_output}</Text>
      ) : null}
    </>
  );
}

function NodeOutcome({ node }: { node: PlanNode }) {
  return (
    <>
      {node.result_summary ? (
        <Text selectable style={{ color: COLORS.accent }}>Résultat : {node.result_summary}</Text>
      ) : null}
      {node.error_summary ? (
        <Text selectable style={{ color: COLORS.danger }}>Erreur : {node.error_summary}</Text>
      ) : null}
    </>
  );
}

function NodeActions({ node, navigation }: { node: PlanNode; navigation: GoalDetailNavigation }) {
  const showApprovals = node.status === "waiting_permission" || node.status === "waiting_capability";
  return (
    <>
      {node.task_id ? (
        <ActionButton label="Voir la tâche" onPress={() => navigation.openTask(node.task_id as string)} />
      ) : null}
      {showApprovals ? <ActionButton label="Voir les accords" onPress={navigation.openApprovals} /> : null}
    </>
  );
}

function NodeCard({ node, navigation, readOnly }: {
  node: PlanNode;
  navigation: GoalDetailNavigation;
  readOnly: boolean;
}) {
  return (
    <Card testID={`plan-node-${node.id}`}>
      <View style={{ alignItems: "center", flexDirection: "row", justifyContent: "space-between" }}>
        <Text style={{ color: COLORS.text, flex: 1, fontSize: 16, fontWeight: "700" }}>
          {node.title}
        </Text>
        <Text
          style={{
            color: NODE_COLORS[node.status] ?? COLORS.subtle,
            fontSize: 11,
            fontWeight: "800",
            textTransform: "uppercase",
          }}
        >
          {NODE_LABELS[node.status]}
        </Text>
      </View>
      <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>{node.objective}</Text>
      <NodeMetadata node={node} />
      <NodeOutcome node={node} />
      <NodeActions node={node} navigation={navigation} />
      {node.required_skill === "writing.draft" && node.worker_job_id && node.status === "completed" ? (
        <GoalWritingDraft key={node.worker_job_id} goalId={node.goal_run_id} nodeId={node.id} workerJobId={node.worker_job_id} disabled={readOnly} />
      ) : null}
      {node.required_skill === "code.generate_python"
        && node.worker_job_id
        && (node.status === "waiting_permission" || node.status === "completed") ? (
          <GoalCodeProposalReview
            key={node.worker_job_id}
            goalId={node.goal_run_id}
            nodeId={node.id}
            disabled={readOnly}
            onOpenTask={navigation.openTask}
          />
        ) : null}
    </Card>
  );
}

function Feedback({ disabled, locked, onScore }: {
  disabled: boolean;
  locked: boolean;
  onScore: (score: number) => void;
}) {
  const unavailable = disabled || locked;
  return (
    <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
      {[1, 2, 3, 4, 5].map((score) => (
        <Pressable
          accessibilityLabel={`Noter le résultat ${score} sur 5`}
          accessibilityRole="button"
          accessibilityState={{ disabled: unavailable }}
          disabled={unavailable}
          key={score}
          onPress={() => onScore(score)}
          style={{
            alignItems: "center",
            backgroundColor: COLORS.panelRaised,
            borderColor: COLORS.border,
            borderRadius: 12,
            borderWidth: 1,
            justifyContent: "center",
            minHeight: 46,
            minWidth: 46,
            opacity: unavailable ? 0.45 : 1,
          }}
        >
          <Text style={{ color: COLORS.text, fontWeight: "700" }}>{score}</Text>
        </Pressable>
      ))}
    </View>
  );
}

function GoalActions({ controller, navigation, management = false }: { controller: GoalDetailController; navigation: GoalDetailNavigation; management?: boolean }) {
  const { busy, goal, online } = controller;
  if (!goal || !online) return null;
  const planningAction = PLANNING_PHASES[goal.current_phase]
    ? "Réessayer la planification"
    : "Démarrer le but";
  return (
    <>
      {!management && navigation.openLocalPlan && goal.status === "planning" && !goal.started_at
        && goal.step_count === 0 && goal.replan_count === 0
        && controller.nodes.length === 0 && !controller.result ? (
          <ActionButton
            disabled={Boolean(busy)}
            label={goal.current_phase === "awaiting_local_plan" ? "Reprendre le plan sur l’iPhone" : "Préparer le plan sur l’iPhone"}
            onPress={() => navigation.openLocalPlan?.(goal.id)}
          />
        ) : null}
      {!management && canStartGoal(goal) ? (
        <ActionButton
          busy={busy === "start"}
          disabled={Boolean(busy)}
          label={hasEvaluatorRetryAction(goal) ? "Réessayer l’évaluation" : goal.status === "planning" ? planningAction : "Continuer le but"}
          onPress={() => void controller.start()}
          testID="start-goal-button"
          variant="accent"
        />
      ) : null}
      {management && CANCELLABLE_GOAL_STATUSES.has(goal.status) ? (
        <ActionButton
          busy={busy === "cancel"}
          disabled={Boolean(busy)}
          label="Annuler le but"
          onPress={controller.confirmCancel}
          testID="cancel-goal-button"
          variant="danger"
        />
      ) : null}
      {management && REPLANNABLE_GOAL_STATUSES.has(goal.status) ? (
        <ActionButton
          busy={busy === "replan"}
          disabled={Boolean(busy)}
          label="Demander une replanification"
          onPress={() => void controller.replan()}
          testID="replan-goal-button"
        />
      ) : null}
    </>
  );
}

function goalPhaseNotice(goal: GoalDetail["goal"]) {
  if (awaitsEvaluatorRetry(goal)) return EVALUATOR_RETRY_PHASE;
  if (goal.status === "planning") return PLANNING_PHASES[goal.current_phase];
  if (goal.status === "running") return runningPhase(goal);
  if (goal.status !== "waiting_permission") return undefined;
  switch (goal.current_phase) {
    case "needs_user":
      return { label: "Intervention attendue", description: "Consultez le message dans la conversation du projet pour préciser la demande ou reprendre le travail." };
    case "project_ready":
      return { label: "Projet prêt à relire", description: "Examinez les fichiers et les vérifications avant de préparer une autorisation d’écriture." };
    case "code_proposal_ready":
      return CODE_PROPOSAL_PHASE;
    default:
      return undefined;
  }
}

function GoalBudgets({ goal }: { goal: GoalDetail["goal"] }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ expanded }}
        onPress={() => setExpanded(!expanded)}
        style={{ minHeight: 44, justifyContent: "center" }}
      >
        <Text style={{ color: COLORS.accent, fontWeight: "600" }}>
          {expanded ? "− Masquer les budgets" : "+ Budgets et planificateur"}
        </Text>
      </Pressable>
      {expanded ? (
        <View style={{ gap: 8 }}>
          <Text selectable style={{ color: COLORS.subtle, fontSize: 12 }}>
            Profil {goal.autonomy_profile} · planificateur {goal.planner_source}
          </Text>
          <Text style={{ color: COLORS.subtle, fontSize: 12 }}>
            Étapes {goal.step_count}/{goal.max_steps} · replans {goal.replan_count}/{goal.max_replans} · appels modèle {goal.model_call_count}/{goal.max_model_calls}
          </Text>
        </View>
      ) : null}
    </>
  );
}

function GoalProgress({ completed, total }: { completed: number; total: number }) {
  if (!total) return null;
  return (
    <View
      accessibilityRole="progressbar"
      accessibilityLabel="Étapes terminées"
      accessibilityValue={{ min: 0, max: total, now: completed }}
      style={{ height: 6, backgroundColor: COLORS.panelRaised, borderRadius: 3, overflow: "hidden" }}
    >
      <View style={{ height: 6, width: `${Math.min(100, completed / total * 100)}%`, backgroundColor: COLORS.accent }} />
    </View>
  );
}

function GoalOverview({ controller, navigation }: {
  controller: GoalDetailController;
  navigation: GoalDetailNavigation;
}) {
  const [options, setOptions] = useState(false);
  const { blockedCount, completedCount, goal, nodes, runningAgents } = controller;
  if (!goal) return null;
  const phaseNotice = goalPhaseNotice(goal);
  const phaseColor = ["project_continue", "project_building", "evaluator_retrying"].includes(goal.current_phase)
    ? COLORS.info : COLORS.warning;
  const blockedSuffix = blockedCount === 1 ? "" : "s";
  const agentsSummary = runningAgents.length
    ? `Agents en cours au dernier relevé : ${runningAgents.length}`
    : "Aucun agent en cours au dernier relevé.";
  return (
    <>
      <Card>
        <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <Text style={{ color: phaseNotice ? phaseColor : COLORS.info, fontWeight: "700", flex: 1 }}>
            {phaseNotice ? `Phase : ${phaseNotice.label}` : goalLabel(goal)}
          </Text>
          <Pressable accessibilityRole="button" accessibilityLabel="Options du projet" accessibilityState={{ expanded: options }}
            onPress={() => setOptions(!options)} style={{ minHeight: 44, justifyContent: "center" }}>
            <Text style={{ color: COLORS.accent, fontSize: 13 }}>Options {options ? "−" : "+"}</Text>
          </Pressable>
        </View>
        {phaseNotice ? (
          <Text accessibilityLiveRegion="polite" style={{ color: phaseColor, lineHeight: 20 }}>
            {phaseNotice.description}
          </Text>
        ) : null}
        <GoalProgress completed={completedCount} total={nodes.length} />
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
          <Text style={{ color: COLORS.muted, fontSize: 13 }}>{completedCount}/{nodes.length} étapes terminées{blockedCount ? ` · ${blockedCount} bloqué${blockedSuffix}` : ""}</Text>
          <Text style={{ color: COLORS.muted, fontSize: 13 }}>{agentsSummary}</Text>
        </View>
        {goal.failure_reason && (!phaseNotice || goal.current_phase === "planner_invalid_response") ? (
          <Text selectable style={{ color: COLORS.danger, lineHeight: 20 }}>Échec : {goal.failure_reason}</Text>
        ) : null}
        <GoalActions controller={controller} navigation={navigation} />
        {options ? <View style={{ gap: 10 }}>
          <GoalBudgets goal={goal} />
          <ActionButton label="Voir la tâche racine" onPress={() => navigation.openTask(goal.root_task_id)} />
          <GoalActions controller={controller} navigation={navigation} management />
        </View> : null}
      </Card>
    </>
  );
}

function GoalResultSection({ controller }: { controller: GoalDetailController }) {
  const { feedbackLocked, online, result } = controller;
  if (!result) return null;
  return (
    <>
      <SectionTitle title="Résultat final" />
      <Card testID="goal-result">
        <Text selectable style={{ color: COLORS.text, fontSize: 17, lineHeight: 24 }}>{result.answer}</Text>
        <Text style={{ color: COLORS.muted }}>
          {result.completed_nodes.length} nœuds terminés · {result.failed_nodes.length} échoués
        </Text>
        <Text style={{ color: COLORS.muted }}>
          Agents : {result.agents_used.length ? result.agents_used.join(", ") : "aucun"}
        </Text>
        {result.limitations.length ? (
          <View style={{ gap: 5 }}>
            <Text style={{ color: COLORS.warning, fontWeight: "700" }}>Limites</Text>
            {result.limitations.map((limitation) => (
              <Text key={limitation} selectable style={{ color: COLORS.muted }}>• {limitation}</Text>
            ))}
          </View>
        ) : null}
      </Card>
      <SectionTitle title="Feedback final" />
      <Feedback disabled={!online} locked={feedbackLocked} onScore={(score) => void controller.submitFeedback(score)} />
      {feedbackLocked ? (
        <Text style={{ color: COLORS.subtle }}>
          Le feedback a été tenté une fois; aucune répétition automatique n’est autorisée.
        </Text>
      ) : null}
    </>
  );
}

const PROJECT_TABS = ["Plan", "Activité", "Résultats", "Échanges"] as const;
type ProjectTab = typeof PROJECT_TABS[number];

function GoalWorkspace({ controller, navigation }: { controller: GoalDetailController; navigation: GoalDetailNavigation }) {
  const goal = controller.goal!;
  const [tab, setTab] = useState<ProjectTab>("Plan");
  const [selectedNodeId, selectNode] = useState<string | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const insets = useSafeAreaInsets();
  const graph = useProjectGraph(goal.id, controller.source === "authoritative", goal.updated_at);
  const evidence = useProjectEvidence(goal.id, controller.source === "authoritative" && controller.online, goal.updated_at);
  const nodes = graph.graph?.nodes ?? controller.nodes;
  const selected = nodes.find((node) => node.id === selectedNodeId);
  const readOnly = !controller.online || Boolean(controller.busy) || (graph.graph !== null && graph.stale);
  const panel = (name: ProjectTab) => ({ display: tab === name ? "flex" as const : "none" as const, gap: 14 });
  return <>
    <GoalOverview controller={controller} navigation={navigation} />
    <View accessibilityRole="tablist" style={{ flexDirection: "row", flexWrap: "wrap", gap: 4, borderBottomWidth: 1, borderColor: COLORS.border }}>
      {PROJECT_TABS.map((name) => <Pressable key={name} accessibilityRole="tab" accessibilityLabel={name}
        accessibilityState={{ selected: tab === name }} onPress={() => setTab(name)}
        style={{ flexGrow: 1, minHeight: 48, minWidth: 65, alignItems: "center", justifyContent: "center",
          borderBottomWidth: 2, borderBottomColor: tab === name ? COLORS.accent : "transparent" }}>
        <Text style={{ color: tab === name ? COLORS.accent : COLORS.muted, fontSize: 13, fontWeight: "600" }}>{name}</Text>
      </Pressable>)}
    </View>
    <View style={panel("Plan")} accessibilityElementsHidden={tab !== "Plan"} importantForAccessibility={tab !== "Plan" ? "no-hide-descendants" : "auto"}>
      <ProjectGraphPlan state={graph} fallbackNodes={controller.nodes} fallbackSummary={goal.evaluator_summary}
        enabled={controller.source === "authoritative"} selectedNodeId={selectedNodeId} onSelectNode={(id) => { selectNode(id); setDetailsOpen(Boolean(id)); }}
        evidence={evidence.view} evidenceStale={evidence.stale} onOpenEvidence={() => setTab("Résultats")} />
    </View>
    <View style={panel("Activité")} accessibilityElementsHidden={tab !== "Activité"} importantForAccessibility={tab !== "Activité" ? "no-hide-descendants" : "auto"}>
      {selected ? <ActionButton label="Voir les opérations de tout le projet" onPress={() => selectNode(null)} /> : null}
      <ActivityTimeline scope="goal" id={goal.id} enabled={controller.source === "authoritative"}
        refreshKey={goal.updated_at} follow nodes={nodes} selectedNodeId={selected?.id ?? null} onOpenTask={navigation.openTask} />
    </View>
    <View style={panel("Résultats")} accessibilityElementsHidden={tab !== "Résultats"} importantForAccessibility={tab !== "Résultats" ? "no-hide-descendants" : "auto"}>
      <ProjectRequirementEvidence state={evidence} disabled={!controller.online || Boolean(controller.busy)} />
      <ProjectGraphEvidence graph={graph.graph} stale={graph.stale} showCriteria={evidence.view === null} />
      {controller.nodes.some((node) => node.required_skill === "code.build_project") ? <GoalProjectReview
        key={`project:${goal.id}`} goalId={goal.id} disabled={!controller.online || Boolean(controller.busy)} onOpenTask={navigation.openTask} /> : null}
      <GoalResultSection controller={controller} />
    </View>
    <View style={panel("Échanges")} accessibilityElementsHidden={tab !== "Échanges"} importantForAccessibility={tab !== "Échanges" ? "no-hide-descendants" : "auto"}>
      <GoalConversation key={goal.id} goal={goal} disabled={!controller.online || Boolean(controller.busy)}
        onOpenGoal={navigation.openGoal} onOpenLocalPlan={navigation.openLocalPlan} onUpdated={() => controller.refresh(false)} />
    </View>
    <Modal visible={Boolean(selected) && detailsOpen} animationType="slide" presentationStyle="pageSheet" onRequestClose={() => setDetailsOpen(false)}>
      <View accessibilityViewIsModal style={{ flex: 1, backgroundColor: COLORS.background, paddingTop: Math.max(insets.top, 16), paddingBottom: insets.bottom }}>
        <View style={{ width: "100%", maxWidth: 680, alignSelf: "center", flex: 1 }}>
          <View style={{ paddingHorizontal: 20, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
            <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 18, fontWeight: "700", flex: 1 }}>Détails de l’étape</Text>
            <Pressable accessibilityRole="button" accessibilityLabel="Fermer les détails de l’étape" onPress={() => setDetailsOpen(false)} style={{ minHeight: 48, justifyContent: "center" }}>
              <Text style={{ color: COLORS.accent, fontWeight: "600" }}>Fermer</Text>
            </Pressable>
          </View>
          <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
            {selected ? <NodeCard key={selected.id} node={selected} readOnly={readOnly} navigation={{ ...navigation,
              openTask: (id) => { setDetailsOpen(false); navigation.openTask(id); },
              openApprovals: () => { setDetailsOpen(false); navigation.openApprovals(); } }} /> : null}
            {selected ? <ProjectGraphNodeEvidence graph={graph.graph} nodeId={selected.id} evidence={evidence.view}
              stale={graph.stale || evidence.stale} onOpenEvidence={() => { setDetailsOpen(false); setTab("Résultats"); }} /> : null}
            <Text style={{ color: COLORS.subtle, fontSize: 12 }}>L’objectif est celui enregistré pour cette étape. Les explications du plan restent consultables dans l’onglet Plan.</Text>
            <ActionButton label="Voir les opérations de cette étape" onPress={() => { setDetailsOpen(false); setTab("Activité"); }} />
          </ScrollView>
        </View>
      </View>
    </Modal>
  </>;
}

function GoalBody({ controller, navigation }: { controller: GoalDetailController; navigation: GoalDetailNavigation }) {
  if (controller.goal) return <GoalWorkspace key={controller.goal.id} controller={controller} navigation={navigation} />;
  if (!controller.initialLoading && !controller.refreshing && !controller.error) {
    return <EmptyState title="But introuvable" subtitle="Aucune preuve autoritaire ou locale n’est disponible." />;
  }
  return null;
}

export function GoalDetailContent({ controller, navigation }: {
  controller: GoalDetailController;
  navigation: GoalDetailNavigation;
}) {
  const subtitle = controller.goal ? undefined : "Preuves du control plane";
  return (
    <ScreenShell
      title={controller.goal?.objective ?? "But"}
      subtitle={subtitle}
      onRefresh={() => void controller.refresh()}
      refreshing={controller.refreshing}
      testID="goal-detail-screen"
    >
      <ErrorBanner message={controller.error} />
      {controller.source === "cache" ? (
        <Text accessibilityLiveRegion="polite" style={{ color: COLORS.warning, lineHeight: 20 }}>
          Copie locale possiblement périmée : démarrage, annulation, replanification, feedback et permissions sont verrouillés.
        </Text>
      ) : null}
      {controller.notice ? (
        <Text accessibilityLiveRegion="polite" style={{ color: COLORS.accent }}>{controller.notice}</Text>
      ) : null}
      <Pressable accessibilityRole="button" accessibilityLabel="Actualiser les preuves" accessibilityState={{ disabled: Boolean(controller.busy) || controller.refreshing, busy: controller.refreshing }}
        disabled={Boolean(controller.busy) || controller.refreshing} onPress={() => void controller.refresh()} testID="refresh-goal-button"
        style={{ minHeight: 44, alignSelf: "flex-end", justifyContent: "center" }}>
        <Text style={{ color: COLORS.accent, fontSize: 13 }}>{controller.refreshing ? "Actualisation…" : "Actualiser"}</Text>
      </Pressable>
      <GoalBody controller={controller} navigation={navigation} />
    </ScreenShell>
  );
}
