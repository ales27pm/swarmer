import { useCallback, useEffect, useState } from "react";
import { Alert, Text, View } from "react-native";
import { useLocalSearchParams } from "expo-router";

import { ScreenShell } from "@/components/screen-shell";
import {
  ActionButton,
  ApprovalDecisionCard,
  Card,
  COLORS,
  EmptyState,
  ErrorBanner,
  SectionTitle,
  StatusBadge,
  timeAgo,
  useAccessibilityAnnouncement,
  useApprovalDecisionLocks,
} from "@/components/swarm-ui";
import {
  ApiError,
  cancelTask,
  createFeedback,
  getServerUrl,
  getTask,
  planTask,
  type Approval,
  type TaskDetail,
} from "@/lib/api/client";
import {
  approvalDecisionError,
  submitApprovalDecision,
} from "@/lib/approval-decision";
import { localApprovals, localTask } from "@/lib/state/replica";
import { useLiveRefresh } from "@/lib/sync/live-sync-context";

const CANCELLABLE = new Set(["created", "planned", "waiting_permission", "queued", "blocked"]);

function JsonEvidence({ label, value }: { label: string; value: unknown }) {
  if (value === null || value === undefined) return null;
  return (
    <View style={{ gap: 5 }}>
      <Text style={{ color: COLORS.subtle, fontSize: 11, fontWeight: "800", textTransform: "uppercase" }}>
        {label}
      </Text>
      <Text
        selectable
        style={{
          backgroundColor: COLORS.background,
          borderRadius: 10,
          color: COLORS.muted,
          fontFamily: "Courier",
          fontSize: 11,
          lineHeight: 17,
          padding: 10,
        }}
      >
        {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
      </Text>
    </View>
  );
}

function TaskSummary({
  task,
  busy,
  readOnly,
  onPlan,
  onCancel,
}: {
  task: TaskDetail["task"];
  busy: string | null;
  readOnly: boolean;
  onPlan: () => void;
  onCancel: () => void;
}) {
  return (
    <Card>
      <View style={{ alignItems: "center", flexDirection: "row", justifyContent: "space-between" }}>
        <StatusBadge status={task.status} />
        <Text style={{ color: COLORS.subtle, fontSize: 11 }}>{timeAgo(task.updated_at)}</Text>
      </View>
      <Text selectable style={{ color: COLORS.text, fontSize: 17, fontWeight: "700", lineHeight: 23 }}>
        {task.input}
      </Text>
      {task.error_json?.message ? (
        <Text selectable style={{ color: COLORS.danger, lineHeight: 20 }}>
          {task.error_json.message}
        </Text>
      ) : null}
      {task.status === "created" && !readOnly ? (
        <ActionButton
          busy={busy === "plan"}
          disabled={Boolean(busy)}
          label="Demander un plan au modèle"
          onPress={onPlan}
          variant="accent"
        />
      ) : null}
      {CANCELLABLE.has(task.status) && !readOnly ? (
        <ActionButton
          busy={busy === "cancel"}
          disabled={Boolean(busy)}
          label="Annuler la tâche"
          onPress={onCancel}
          testID="cancel-task-button"
          variant="danger"
        />
      ) : null}
      {task.status === "running" ? (
        <Text style={{ color: COLORS.warning, lineHeight: 19 }}>
          L’exécution est déjà lancée et ne peut pas être annulée sans risquer un état trompeur.
        </Text>
      ) : null}
    </Card>
  );
}

function ApprovalRequests({
  approvals,
  busy,
  lockedApprovalIds,
  onDecision,
}: {
  approvals: Approval[];
  busy: string | null;
  lockedApprovalIds: ReadonlySet<string>;
  onDecision: (approval: Approval, decision: "approve" | "deny") => void;
}) {
  const pending = approvals.filter((approval) => approval.status === "pending");
  if (!pending.length) return null;
  return (
    <>
      <SectionTitle title="Permission demandée" />
      {pending.map((approval) => (
        <ApprovalDecisionCard
          key={approval.id}
          allowTestID="detail-allow-button"
          approval={approval}
          busy={busy}
          decisionLocked={lockedApprovalIds.has(approval.id)}
          denyTestID="detail-deny-button"
          onDecision={(decision) => onDecision(approval, decision)}
        />
      ))}
    </>
  );
}

function ToolEvidence({ tools }: { tools: TaskDetail["tool_calls"] }) {
  return (
    <>
      <SectionTitle title="Appels d’outils" />
      {tools.length ? tools.map((tool) => (
        <Card key={tool.id}>
          <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
            <Text selectable style={{ color: COLORS.text, fontWeight: "700" }}>{tool.tool_name}</Text>
            <Text style={{ color: tool.status === "completed" ? COLORS.accent : tool.status === "failed" ? COLORS.danger : COLORS.warning, fontSize: 11, fontWeight: "800", textTransform: "uppercase" }}>
              {tool.status}
            </Text>
          </View>
          <Text style={{ color: COLORS.warning, fontSize: 11, fontWeight: "800", textTransform: "uppercase" }}>
            Libellé public généré par le serveur
          </Text>
          <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>{tool.summary}</Text>
          <JsonEvidence label="Arguments publics expurgés" value={tool.arguments} />
          <JsonEvidence label="Résultat public vérifié" value={tool.result} />
          <JsonEvidence label="Erreur d’exécution" value={tool.error} />
        </Card>
      )) : (
        <Text style={{ color: COLORS.subtle }}>Aucun outil proposé ou exécuté.</Text>
      )}
    </>
  );
}

function Timeline({ messages }: { messages: TaskDetail["messages"] }) {
  return (
    <>
      <SectionTitle title="Timeline" />
      {messages.length ? messages.map((message) => {
        const isProposalOnly = message.metadata?.verified_status === "proposal_only";
        return (
          <Card key={message.id}>
            <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
              <Text style={{ color: COLORS.accent, fontSize: 11, fontWeight: "800", textTransform: "uppercase" }}>
                {message.agent_id ?? message.role}
              </Text>
              <Text style={{ color: COLORS.subtle, fontSize: 10 }}>{timeAgo(message.created_at)}</Text>
            </View>
            {isProposalOnly ? (
              <Text style={{ color: COLORS.warning, fontSize: 11, fontWeight: "800", textTransform: "uppercase" }}>
                Proposition du modèle — non vérifiée
              </Text>
            ) : null}
            <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>{message.content}</Text>
          </Card>
        );
      }) : (
        <Text style={{ color: COLORS.subtle }}>Aucun message lié à cette tâche.</Text>
      )}
    </>
  );
}

function FeedbackControls({
  busy,
  feedback,
  onRate,
}: {
  busy: string | null;
  feedback: string | null;
  onRate: (score: number, label: string) => void;
}) {
  return (
    <>
      <SectionTitle title="Feedback" />
      <View style={{ flexDirection: "row", gap: 8 }}>
        <ActionButton
          busy={busy === "feedback:good"}
          disabled={Boolean(busy) || Boolean(feedback)}
          label="Utile"
          onPress={() => onRate(5, "good")}
          style={{ flex: 1 }}
          testID="feedback-up-button"
        />
        <ActionButton
          busy={busy === "feedback:bad"}
          disabled={Boolean(busy) || Boolean(feedback)}
          label="À revoir"
          onPress={() => onRate(1, "bad")}
          style={{ flex: 1 }}
          testID="feedback-down-button"
          variant="danger"
        />
      </View>
      {feedback ? <Text style={{ color: COLORS.accent }}>{feedback}</Text> : null}
    </>
  );
}

type RefreshTask = (clearError?: boolean) => Promise<string | null>;
type SetNullableText = (value: string | null) => void;

type TaskActionContext = {
  busy: string | null;
  detail: TaskDetail | null;
  lockApproval: (approvalId: string) => void;
  refresh: RefreshTask;
  setBusy: SetNullableText;
  setError: SetNullableText;
  setFeedback: SetNullableText;
  setNotice: SetNullableText;
};

function errorMessage(cause: unknown) {
  return cause instanceof Error ? cause.message : String(cause);
}

function actionErrorMessage(cause: unknown) {
  if (cause instanceof ApiError && cause.status === 409) {
    return "L’état a changé avant cette action. La tâche a été actualisée.";
  }
  return errorMessage(cause);
}

function useTaskDetailLoader(
  taskId: string | undefined,
  reconcileApprovals: (approvals: Approval[]) => void,
) {
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);

  const refresh = useCallback(async (clearError = true) => {
    if (!taskId) {
      setInitialLoading(false);
      return null;
    }
    setRefreshing(true);
    if (clearError) setError(null);
    try {
      const nextDetail = await getTask(taskId);
      setDetail(nextDetail);
      setOffline(false);
      reconcileApprovals(nextDetail.approvals);
      return null;
    } catch (cause) {
      const message = errorMessage(cause);
      setError(message);
      setOffline(true);
      const scope = await getServerUrl().catch(() => null);
      const cachedTask = scope ? await localTask(scope, taskId).catch(() => null) : null;
      if (cachedTask && scope) {
        const approvals = await localApprovals(scope, "all").catch(() => []);
        const cachedApprovals = approvals.filter((approval) => approval.task_id === taskId);
        setDetail({ task: cachedTask, approvals: cachedApprovals, messages: [], tool_calls: [] });
        reconcileApprovals(cachedApprovals);
      }
      return message;
    } finally {
      setRefreshing(false);
      setInitialLoading(false);
    }
  }, [reconcileApprovals, taskId]);

  useEffect(() => {
    void (async () => {
      await refresh(false);
    })();
  }, [refresh]);
  useLiveRefresh(() => refresh(false));

  return { detail, error, initialLoading, offline, refresh, refreshing, setError };
}

async function runTaskAction(
  context: TaskActionContext,
  action: string,
  operation: () => Promise<unknown>,
) {
  if (context.busy) return;
  context.setBusy(action);
  context.setError(null);
  context.setNotice(null);
  try {
    await operation();
  } catch (cause) {
    context.setError(actionErrorMessage(cause));
  } finally {
    await context.refresh(false);
    context.setBusy(null);
  }
}

async function decideTaskApproval(
  context: TaskActionContext,
  approval: Approval,
  decision: "approve" | "deny",
) {
  if (context.busy) return;
  context.setBusy(`${approval.id}:${decision}`);
  context.setError(null);
  context.setNotice(null);
  const outcome = await submitApprovalDecision(
    approval.id,
    decision,
    context.lockApproval,
    "L’état a changé avant cette action.",
  );
  context.setNotice(outcome.notice);
  const refreshError = await context.refresh(false);
  context.setError(
    approvalDecisionError(outcome, refreshError, {
      refreshFailure: "Impossible d’actualiser les preuves authentifiées",
      refreshed: "La tâche a été actualisée.",
    }),
  );
  context.setBusy(null);
}

async function rateTask(context: TaskActionContext, score: number, label: string) {
  if (!context.detail || context.busy) return;
  context.setBusy(`feedback:${label}`);
  context.setError(null);
  try {
    await createFeedback({ task_id: context.detail.task.id, score, label });
    context.setFeedback("Feedback enregistré pour les évaluations futures.");
  } catch (cause) {
    context.setError(errorMessage(cause));
  } finally {
    context.setBusy(null);
  }
}

function confirmTaskCancellation(context: TaskActionContext, task: TaskDetail["task"]) {
  if (context.busy) return;
  Alert.alert(
    "Annuler cette tâche?",
    "L’exécution sera bloquée. Les messages et preuves déjà enregistrés resteront consultables.",
    [
      { text: "Garder la tâche", style: "cancel" },
      {
        text: "Annuler la tâche",
        style: "destructive",
        onPress: () => void runTaskAction(context, "cancel", () => cancelTask(task.id)),
      },
    ],
  );
}

function createTaskActions(context: TaskActionContext) {
  return {
    cancel: (task: TaskDetail["task"]) => confirmTaskCancellation(context, task),
    decide: (approval: Approval, decision: "approve" | "deny") => {
      void decideTaskApproval(context, approval, decision);
    },
    plan: (taskId: string) => {
      void runTaskAction(context, "plan", () => planTask(taskId));
    },
    rate: (score: number, label: string) => {
      void rateTask(context, score, label);
    },
  };
}

function taskSubtitle(task: TaskDetail["task"] | undefined, initialLoading: boolean) {
  if (task) return `${task.id} · mode ${task.mode}`;
  if (initialLoading) return "Chargement des preuves authentifiées…";
  return "Aucune preuve authentifiée chargée.";
}

function TaskEvidence({
  actions,
  busy,
  detail,
  feedback,
  lockedApprovalIds,
  readOnly,
}: {
  actions: ReturnType<typeof createTaskActions>;
  busy: string | null;
  detail: TaskDetail;
  feedback: string | null;
  lockedApprovalIds: ReadonlySet<string>;
  readOnly: boolean;
}) {
  const { task } = detail;
  return (
    <>
      <TaskSummary
        task={task}
        busy={busy}
        readOnly={readOnly}
        onPlan={() => actions.plan(task.id)}
        onCancel={() => actions.cancel(task)}
      />
      <ApprovalRequests
        approvals={detail.approvals}
        busy={busy}
        lockedApprovalIds={readOnly ? new Set(detail.approvals.map((approval) => approval.id)) : lockedApprovalIds}
        onDecision={actions.decide}
      />
      <ToolEvidence tools={detail.tool_calls} />
      <Timeline messages={detail.messages} />
      {task.status === "completed" && !readOnly ? (
        <FeedbackControls busy={busy} feedback={feedback} onRate={actions.rate} />
      ) : null}
    </>
  );
}

function TaskDetailContent({
  actions,
  busy,
  detail,
  error,
  feedback,
  initialLoading,
  lockedApprovalIds,
  offline,
  refreshing,
}: {
  actions: ReturnType<typeof createTaskActions>;
  busy: string | null;
  detail: TaskDetail | null;
  error: string | null;
  feedback: string | null;
  initialLoading: boolean;
  lockedApprovalIds: ReadonlySet<string>;
  offline: boolean;
  refreshing: boolean;
}) {
  if (detail) {
    return (
      <TaskEvidence
        actions={actions}
        busy={busy}
        detail={detail}
        feedback={feedback}
        lockedApprovalIds={lockedApprovalIds}
        readOnly={offline}
      />
    );
  }
  if (!initialLoading && !refreshing && !error) {
    return <EmptyState title="Tâche introuvable" subtitle="Aucune donnée authentifiée n’est disponible." />;
  }
  return null;
}

function useTaskDetailState(taskId: string | undefined) {
  const approvalLocks = useApprovalDecisionLocks();
  const loader = useTaskDetailLoader(taskId, approvalLocks.reconcileApprovals);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  useAccessibilityAnnouncement(notice);

  return {
    ...loader,
    ...approvalLocks,
    busy,
    feedback,
    notice,
    setBusy,
    setFeedback,
    setNotice,
  };
}

export default function TaskDetailScreen() {
  const { id } = useLocalSearchParams<{ id?: string | string[] }>();
  const taskId = Array.isArray(id) ? id[0] : id;
  const state = useTaskDetailState(taskId);
  const actions = createTaskActions({
    busy: state.busy,
    detail: state.detail,
    lockApproval: state.lockApproval,
    refresh: state.refresh,
    setBusy: state.setBusy,
    setError: state.setError,
    setFeedback: state.setFeedback,
    setNotice: state.setNotice,
  });
  const task = state.detail?.task;

  return (
    <ScreenShell
      title={task?.title || "Tâche"}
      subtitle={taskSubtitle(task, state.initialLoading)}
      onRefresh={() => void state.refresh()}
      refreshing={state.refreshing}
      testID="task-detail-screen"
    >
      <ErrorBanner message={state.error} />
      {state.offline ? (
        <Text accessibilityLiveRegion="polite" style={{ color: COLORS.warning, lineHeight: 19 }}>
          Copie locale hors ligne : les messages et appels d’outils peuvent être incomplets. Toutes les actions sont verrouillées jusqu’au retour des preuves authentifiées.
        </Text>
      ) : null}
      {state.notice ? (
        <Text accessibilityLiveRegion="polite" selectable style={{ color: COLORS.accent }}>
          {state.notice}
        </Text>
      ) : null}
      <ActionButton
        busy={state.refreshing}
        disabled={Boolean(state.busy)}
        label="Actualiser les preuves"
        onPress={() => void state.refresh()}
        testID="refresh-task-button"
      />
      <TaskDetailContent
        actions={actions}
        busy={state.busy}
        detail={state.detail}
        error={state.error}
        feedback={state.feedback}
        initialLoading={state.initialLoading}
        lockedApprovalIds={state.lockedApprovalIds}
        offline={state.offline}
        refreshing={state.refreshing}
      />
    </ScreenShell>
  );
}
