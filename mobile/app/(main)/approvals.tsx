import { useCallback, useEffect, useState } from "react";
import { Text, View } from "react-native";
import { useRouter } from "expo-router";

import { ScreenShell } from "@/components/screen-shell";
import {
  ActionButton,
  ApprovalDecisionCard,
  COLORS,
  EmptyState,
  ErrorBanner,
  useAccessibilityAnnouncement,
  useApprovalDecisionLocks,
} from "@/components/swarm-ui";
import { getServerUrl, listApprovals, type Approval } from "@/lib/api/client";
import { localApprovals } from "@/lib/state/replica";
import { useLiveRefresh } from "@/lib/sync/live-sync-context";
import {
  approvalDecisionError,
  submitApprovalDecision,
} from "@/lib/approval-decision";

export default function ApprovalsScreen() {
  const router = useRouter();
  const [items, setItems] = useState<Approval[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [decidedTaskId, setDecidedTaskId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);
  const { lockedApprovalIds, lockApproval, reconcileApprovals } =
    useApprovalDecisionLocks();
  useAccessibilityAnnouncement(notice);

  const refresh = useCallback(async (clearError = true) => {
    setRefreshing(true);
    if (clearError) setError(null);
    try {
      const approvals = await listApprovals();
      setItems(approvals);
      setOffline(false);
      reconcileApprovals(approvals);
      return null;
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      try {
        const cached = await localApprovals(await getServerUrl());
        setItems(cached);
        setOffline(true);
        setError(
          cached.length
            ? `Hors ligne — accords en cache, décisions désactivées. ${message}`
            : message,
        );
      } catch {
        setError(message);
      }
      return message;
    } finally {
      setRefreshing(false);
    }
  }, [reconcileApprovals]);

  useEffect(() => {
    void (async () => {
      await refresh(false);
    })();
  }, [refresh]);
  useLiveRefresh(() => refresh(false));

  async function decide(id: string, decision: "approve" | "deny") {
    if (deciding) return;
    setDeciding(`${id}:${decision}`);
    setError(null);
    setNotice(null);
    setDecidedTaskId(null);
    const outcome = await submitApprovalDecision(id, decision, lockApproval);
    setNotice(outcome.notice);
    setDecidedTaskId(outcome.taskId);
    const refreshError = await refresh(false);
    setError(
      approvalDecisionError(outcome, refreshError, {
        refreshFailure: "Impossible d’actualiser l’état authentifié",
        refreshed: "La liste a été actualisée.",
      }),
    );
    setDeciding(null);
  }

  return (
    <ScreenShell
      title="Accords"
      subtitle="Chaque accord est à usage unique. Une seconde décision est refusée par le serveur."
      onRefresh={() => void refresh()}
      refreshing={refreshing}
      testID="approvals-screen"
    >
      <ErrorBanner message={error} />
      <ActionButton
        busy={refreshing}
        label="Actualiser les accords"
        onPress={() => void refresh()}
        testID="refresh-approvals-button"
      />
      {notice ? (
        <Text accessibilityLiveRegion="polite" selectable style={{ color: COLORS.accent }}>
          {notice}
        </Text>
      ) : null}
      {decidedTaskId ? (
        <ActionButton
          label="Voir le résultat et les preuves"
          onPress={() =>
            router.push({ pathname: "/task/[id]", params: { id: decidedTaskId } })
          }
          testID="decided-task-button"
        />
      ) : null}
      {!items.length && !refreshing && !error ? (
        <EmptyState
          title="Aucun accord en attente"
          subtitle="Les écritures et processus sensibles apparaîtront ici avec l’action et son risque exacts."
        />
      ) : null}
      <View style={{ gap: 12 }} testID="approvals-list">
        {items.map((item) => (
          <ApprovalDecisionCard
            key={item.id}
            allowTestID={`allow-button-${item.id}`}
            approval={item}
            busy={deciding}
            cardTestID={`approval-card-${item.id}`}
            decisionLocked={offline || lockedApprovalIds.has(item.id)}
            denyTestID={`deny-button-${item.id}`}
            onDecision={(decision) => {
              if (!offline) void decide(item.id, decision);
            }}
            onOpenTask={() =>
              router.push({ pathname: "/task/[id]", params: { id: item.task_id } })
            }
          />
        ))}
      </View>
    </ScreenShell>
  );
}
