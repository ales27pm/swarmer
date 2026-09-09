import { useCallback, useEffect, useReducer, useRef } from "react";
import { Alert, Pressable, Text, View } from "react-native";

import { ScreenShell } from "@/components/screen-shell";
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
} from "@/lib/api/client";
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

const ACTIVE_NODE_STATUSES = new Set<GoalNodeStatus>([
  "dispatched",
  "running",
  "waiting_permission",
  "waiting_capability",
]);
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
  const [state, update] = useReducer(mergeState, INITIAL_STATE);

  const refresh = useCallback(async (clearError = true) => {
    const epoch = ++refreshEpoch.current;
    const isCurrent = () => refreshEpoch.current === epoch;
    if (!goalId) {
      update({ initialLoading: false });
      return;
    }
    update(clearError ? { refreshing: true, error: null } : { refreshing: true });
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
    if (!canMutate(state)) return;
    update({ busy: action, error: null, notice: null });
    try {
      await operation();
    } catch (cause) {
      update({ error: messageFor(cause) });
    } finally {
      await refresh(false);
      update({ busy: null });
    }
  }, [refresh, state]);

  const start = useCallback(async () => {
    if (goalId) await runAction("start", () => startGoal(goalId));
  }, [goalId, runAction]);

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
    feedbackLockedRef.current = true;
    update({ feedbackLocked: true, error: null });
    try {
      await createGoalFeedback(goalId as string, { score });
      update({ notice: "Feedback enregistré pour les évaluations futures." });
    } catch (cause) {
      update({ error: `${messageFor(cause)} Le feedback incertain n’est pas renvoyé automatiquement.` });
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

function NodeCard({ node, navigation }: { node: PlanNode; navigation: GoalDetailNavigation }) {
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

function GoalActions({ controller }: { controller: GoalDetailController }) {
  const { busy, goal, online } = controller;
  if (!goal || !online) return null;
  return (
    <>
      {goal.status === "planning" ? (
        <ActionButton
          busy={busy === "start"}
          disabled={Boolean(busy)}
          label="Démarrer le but"
          onPress={() => void controller.start()}
          testID="start-goal-button"
          variant="accent"
        />
      ) : null}
      {CANCELLABLE_GOAL_STATUSES.has(goal.status) ? (
        <ActionButton
          busy={busy === "cancel"}
          disabled={Boolean(busy)}
          label="Annuler le but"
          onPress={controller.confirmCancel}
          testID="cancel-goal-button"
          variant="danger"
        />
      ) : null}
      {REPLANNABLE_GOAL_STATUSES.has(goal.status) ? (
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

function GoalOverview({ controller, navigation }: {
  controller: GoalDetailController;
  navigation: GoalDetailNavigation;
}) {
  const { blockedCount, completedCount, goal, nodes, runningAgents } = controller;
  if (!goal) return null;
  const blockedSuffix = blockedCount === 1 ? "" : "s";
  const agentsSummary = runningAgents.length
    ? `Agents en cours : ${runningAgents.join(", ")}`
    : "Aucun agent en cours.";
  return (
    <>
      <SectionTitle title="État autoritaire" />
      <Card>
        <Text style={{ color: COLORS.info, fontWeight: "800" }}>{GOAL_LABELS[goal.status]}</Text>
        <Text style={{ color: COLORS.text, fontSize: 17, fontWeight: "700" }}>Phase : {goal.current_phase}</Text>
        <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
          {completedCount}/{nodes.length} nœuds terminés · {blockedCount} bloqué{blockedSuffix}
        </Text>
        <Text style={{ color: COLORS.muted, lineHeight: 20 }}>{agentsSummary}</Text>
        <Text style={{ color: COLORS.subtle, fontSize: 12 }}>
          Profil {goal.autonomy_profile} · planificateur {goal.planner_source}
        </Text>
        <Text style={{ color: COLORS.subtle, fontSize: 12 }}>
          Étapes {goal.step_count}/{goal.max_steps} · replans {goal.replan_count}/{goal.max_replans} · appels modèle {goal.model_call_count}/{goal.max_model_calls}
        </Text>
        {goal.evaluator_summary ? (
          <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>
            Évaluation : {goal.evaluator_summary}
          </Text>
        ) : null}
        {goal.failure_reason ? (
          <Text selectable style={{ color: COLORS.danger, lineHeight: 20 }}>Échec : {goal.failure_reason}</Text>
        ) : null}
        <ActionButton label="Voir la tâche racine" onPress={() => navigation.openTask(goal.root_task_id)} />
        <GoalActions controller={controller} />
      </Card>
    </>
  );
}

function GoalPlan({ nodes, navigation }: { nodes: PlanNode[]; navigation: GoalDetailNavigation }) {
  return (
    <>
      <SectionTitle title="Plan" />
      {nodes.length ? nodes.map((node) => (
        <NodeCard key={node.id} node={node} navigation={navigation} />
      )) : (
        <Text style={{ color: COLORS.subtle }}>Aucun nœud autoritaire disponible.</Text>
      )}
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

function GoalBody({ controller, navigation }: {
  controller: GoalDetailController;
  navigation: GoalDetailNavigation;
}) {
  if (controller.goal) {
    return (
      <>
        <GoalOverview controller={controller} navigation={navigation} />
        <GoalPlan nodes={controller.nodes} navigation={navigation} />
        <GoalResultSection controller={controller} />
      </>
    );
  }
  if (!controller.initialLoading && !controller.refreshing && !controller.error) {
    return <EmptyState title="But introuvable" subtitle="Aucune preuve autoritaire ou locale n’est disponible." />;
  }
  return null;
}

export function GoalDetailContent({ controller, navigation }: {
  controller: GoalDetailController;
  navigation: GoalDetailNavigation;
}) {
  const subtitle = controller.goal
    ? `${controller.goal.id} · ${GOAL_LABELS[controller.goal.status]}`
    : "Preuves du control plane";
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
      <ActionButton
        busy={controller.refreshing}
        disabled={Boolean(controller.busy)}
        label="Actualiser les preuves"
        onPress={() => void controller.refresh()}
        testID="refresh-goal-button"
      />
      <GoalBody controller={controller} navigation={navigation} />
    </ScreenShell>
  );
}
