import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Text, View } from "react-native";
import { useRouter } from "expo-router";

import { ScreenShell } from "@/components/screen-shell";
import {
  ActionButton,
  ApprovalDecisionCard,
  Card,
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
import { iphoneCapabilityTransport } from "@/lib/iphone-capabilities/runtime";
import { assertCapabilityRequestFresh } from "@/lib/iphone-capabilities/grant";
import type {
  CapabilityArgumentsByName,
  CapabilityAuthorizationDecision,
  CapabilityAuthorizationResponse,
  CapabilityRequestDetail,
  CapabilityRequestPreview,
  CapabilityResult,
  IPhoneCapabilityName,
} from "@/lib/iphone-capabilities/types";

const CAPABILITY_LABELS: Record<IPhoneCapabilityName, string> = {
  "iphone.location.current": "Partager la position actuelle",
  "iphone.contacts.lookup": "Rechercher un contact",
  "iphone.calendar.events": "Consulter une période du calendrier",
  "iphone.photos.pick": "Choisir une photo",
  "iphone.mail.compose": "Composer un courriel",
  "iphone.sms.compose": "Composer un SMS",
};

function textLength(value: string | undefined): number {
  return value ? Array.from(value).length : 0;
}

function capabilityArguments<Name extends IPhoneCapabilityName>(
  request: CapabilityRequestDetail,
  capability: Name,
): CapabilityArgumentsByName[Name] {
  if (request.capability !== capability) {
    throw new Error("Capability summary/request mismatch.");
  }
  return request.arguments as CapabilityArgumentsByName[Name];
}

type CapabilitySummaryFormatter = (request: CapabilityRequestDetail) => string;

const CAPABILITY_SAFE_SUMMARIES: Record<IPhoneCapabilityName, CapabilitySummaryFormatter> = {
  "iphone.location.current": () =>
    "Aucun argument. iOS demandera l’accès à la position avant de la transmettre.",
  "iphone.contacts.lookup": (request) => {
    const arguments_ = capabilityArguments(request, "iphone.contacts.lookup");
    return `Recherche exacte : ${arguments_.query}`;
  },
  "iphone.calendar.events": (request) => {
    const arguments_ = capabilityArguments(request, "iphone.calendar.events");
    return `Période exacte : ${arguments_.start} → ${arguments_.end}. Aucun événement n’est préaffiché.`;
  },
  "iphone.photos.pick": () =>
    "Aucun identifiant de photo. Le sélecteur iOS exigera un choix manuel.",
  "iphone.mail.compose": (request) => {
    const arguments_ = capabilityArguments(request, "iphone.mail.compose");
    return [
      `Destinataires masqués : ${arguments_.recipients?.length ?? 0}/20`,
      `objet masqué : ${textLength(arguments_.subject)}/500 caractères`,
      `corps masqué : ${textLength(arguments_.body)}/10000 caractères`,
    ].join(" · ");
  },
  "iphone.sms.compose": (request) => {
    const arguments_ = capabilityArguments(request, "iphone.sms.compose");
    return [
      `Destinataires masqués : ${arguments_.recipients?.length ?? 0}/20`,
      `message masqué : ${textLength(arguments_.message)}/2000 caractères`,
    ].join(" · ");
  },
};

function capabilitySafeSummary(request: CapabilityRequestDetail): string {
  return CAPABILITY_SAFE_SUMMARIES[request.capability](request);
}

function capabilityResultNotice(result: CapabilityResult): string {
  switch (result.status) {
    case "completed":
      return "Action iPhone exécutée et résultat transmis.";
    case "cancelled":
      return "Action annulée dans iOS; résultat transmis.";
    case "denied":
      return "Permission iOS refusée; résultat transmis.";
    case "failed":
      return "Échec de l’action iPhone; résultat transmis.";
  }
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

function isPendingCapabilityPreview(preview: CapabilityRequestPreview): boolean {
  return preview.status === "waiting_approval" || preview.status === "approved";
}

function pendingCapabilityRequestIds(previews: CapabilityRequestPreview[]): string[] {
  return [
    ...new Set(
      previews
        .filter(isPendingCapabilityPreview)
        .map((preview) => preview.request_id),
    ),
  ];
}

function isActionableCapabilityDetail(detail: CapabilityRequestDetail): boolean {
  if (detail.status === "waiting_approval") return true;
  return detail.status === "approved" && detail.grant === null;
}

function uniqueActionableCapabilities(
  details: CapabilityRequestDetail[],
): CapabilityRequestDetail[] {
  return [
    ...new Map(
      details
        .filter(isActionableCapabilityDetail)
        .map((detail) => [detail.request_id, detail] as const),
    ).values(),
  ];
}

const INVALID_CAPABILITY_REQUEST =
  "Cette demande iPhone n’est plus valide. Actualisez la liste.";

function requireActionableCapability(
  items: CapabilityRequestDetail[],
  requestId: string,
  decision: CapabilityAuthorizationDecision,
): CapabilityRequestDetail {
  const request = items.find((item) => item.request_id === requestId);
  if (!request) throw new Error(INVALID_CAPABILITY_REQUEST);
  if (request.status === "waiting_approval") return request;
  const recoveringGrant = request.status === "approved" && request.grant === null;
  if (!recoveringGrant || decision !== "approve") {
    throw new Error(INVALID_CAPABILITY_REQUEST);
  }
  return request;
}

function deniedCapabilityNotice(
  authorization: CapabilityAuthorizationResponse,
): string {
  if (authorization.status !== "denied" || authorization.grant !== null) {
    throw new Error("Le serveur n’a pas confirmé le refus.");
  }
  return "Demande iPhone refusée.";
}

async function approvedCapabilityNotice(
  requestId: string,
  authorization: CapabilityAuthorizationResponse,
): Promise<string> {
  if (authorization.status !== "approved" || authorization.grant === null) {
    throw new Error("Le serveur n’a pas fourni d’autorisation utilisable.");
  }
  const result = await iphoneCapabilityTransport.execute(requestId);
  return capabilityResultNotice(result);
}

async function submitCapabilityDecision(
  requestId: string,
  decision: CapabilityAuthorizationDecision,
): Promise<string> {
  const authorization = await iphoneCapabilityTransport.authorize(requestId, decision);
  if (decision === "deny") return deniedCapabilityNotice(authorization);
  return approvedCapabilityNotice(requestId, authorization);
}

function useCapabilityExpiry(expiresAt: string): boolean {
  const expiresAtMs = Date.parse(expiresAt);
  const [observedAt, setObservedAt] = useState(() => Date.now());
  const invalid = !Number.isFinite(expiresAtMs);
  const expired = invalid || observedAt >= expiresAtMs;

  useEffect(() => {
    if (invalid || expired) return;
    const observeNow = () => setObservedAt(Date.now());
    const remaining = Math.max(0, expiresAtMs - Date.now());
    const timer = setTimeout(observeNow, Math.min(remaining + 1, 2_147_483_647));
    const subscription = AppState.addEventListener("change", (nextState) => {
      if (nextState === "active") observeNow();
    });
    return () => {
      clearTimeout(timer);
      subscription.remove();
    };
  }, [expired, expiresAtMs, invalid]);

  return expired;
}

function IPhoneCapabilityCard({
  busy,
  locked,
  onDecision,
  request,
}: {
  busy: string | null;
  locked: boolean;
  onDecision: (decision: CapabilityAuthorizationDecision) => void;
  request: CapabilityRequestDetail;
}) {
  const expired = useCapabilityExpiry(request.expires_at);
  const recoveringGrant = request.status === "approved";
  const disabled = locked || expired;
  return (
    <Card testID={`iphone-capability-card-${request.request_id}`}>
      <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 17, fontWeight: "800" }}>
        {CAPABILITY_LABELS[request.capability]}
      </Text>
      <Text selectable style={{ color: COLORS.muted }}>
        Demande : {request.request_id}
      </Text>
      <Text selectable style={{ color: COLORS.muted }}>
        Agent : {request.agent_id} · appareil : {request.target_device_id}
      </Text>
      <Text selectable style={{ color: expired ? COLORS.danger : COLORS.muted }}>
        Expiration : {request.expires_at}
      </Text>
      <Text selectable style={{ color: COLORS.muted }}>
        Empreinte exacte : {request.action_digest}
      </Text>
      <Text style={{ color: COLORS.text }}>{capabilitySafeSummary(request)}</Text>
      {recoveringGrant ? (
        <Text accessibilityRole="alert" style={{ color: COLORS.warning }}>
          L’accord est déjà enregistré, mais son secret à usage unique n’est plus disponible sur cet iPhone. Une nouvelle autorisation explicite remplacera l’ancien secret non consommé.
        </Text>
      ) : null}
      {expired ? (
        <Text accessibilityRole="alert" style={{ color: COLORS.danger }}>
          Demande expirée — actualisez la liste.
        </Text>
      ) : null}
      <View style={{ flexDirection: "row", gap: 10 }}>
        <ActionButton
          accessibilityHint={recoveringGrant
            ? "Remplace le secret non consommé, puis consomme la nouvelle autorisation avant d’ouvrir l’interface iOS."
            : "Autorise cette demande exacte, la consomme sur le serveur, puis ouvre l’interface iOS nécessaire."}
          accessibilityLabel={recoveringGrant
            ? "Récupérer l’autorisation iPhone une fois"
            : "Autoriser l’action iPhone une fois"}
          busy={busy === `${request.request_id}:approve`}
          disabled={disabled}
          label={recoveringGrant ? "Récupérer une fois" : "Autoriser une fois"}
          onPress={() => onDecision("approve")}
          style={{ flex: 1 }}
          testID={`allow-iphone-capability-${request.request_id}`}
          variant="accent"
        />
        {recoveringGrant ? null : (
          <ActionButton
            accessibilityHint="Refuse cette demande sans ouvrir d’interface iOS."
            accessibilityLabel="Refuser l’action iPhone"
            busy={busy === `${request.request_id}:deny`}
            disabled={disabled}
            label="Refuser"
            onPress={() => onDecision("deny")}
            style={{ flex: 1 }}
            testID={`deny-iphone-capability-${request.request_id}`}
            variant="danger"
          />
        )}
      </View>
    </Card>
  );
}

type ApprovalDecision = "approve" | "deny";

function useApprovalQueue() {
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
      const message = errorMessage(cause);
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

  async function decide(id: string, decision: ApprovalDecision) {
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

  return {
    decidedTaskId,
    deciding,
    error,
    items,
    lockedApprovalIds,
    notice,
    offline,
    refresh,
    refreshing,
    decide,
  };
}

function useCapabilityQueue() {
  const [items, setItems] = useState<CapabilityRequestDetail[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refreshEpoch = useRef(0);
  const decisionInFlight = useRef(false);
  useAccessibilityAnnouncement(notice);

  const refreshCapabilities = useCallback(async (clearError = true) => {
    if (decisionInFlight.current) return null;
    const epoch = ++refreshEpoch.current;
    setRefreshing(true);
    if (clearError) setError(null);
    try {
      const previews = await iphoneCapabilityTransport.refresh();
      const details = await Promise.all(
        pendingCapabilityRequestIds(previews).map((requestId) =>
          iphoneCapabilityTransport.load(requestId),
        ),
      );
      if (epoch !== refreshEpoch.current) return null;
      setItems(uniqueActionableCapabilities(details));
      setError(null);
      return null;
    } catch (cause) {
      const message = errorMessage(cause);
      if (epoch === refreshEpoch.current) {
        setItems([]);
        setError(`Demandes iPhone indisponibles : ${message}`);
      }
      return message;
    } finally {
      if (epoch === refreshEpoch.current) setRefreshing(false);
    }
  }, []);

  async function decideCapability(
    requestId: string,
    decision: CapabilityAuthorizationDecision,
  ) {
    if (decisionInFlight.current || refreshing) return;
    try {
      const request = requireActionableCapability(items, requestId, decision);
      assertCapabilityRequestFresh(request);
    } catch {
      setError(INVALID_CAPABILITY_REQUEST);
      return;
    }
    decisionInFlight.current = true;
    refreshEpoch.current += 1;
    setRefreshing(false);
    setBusy(`${requestId}:${decision}`);
    setError(null);
    setNotice(null);
    try {
      setNotice(await submitCapabilityDecision(requestId, decision));
      setItems((current) => current.filter((item) => item.request_id !== requestId));
    } catch (cause) {
      setError(`Action iPhone bloquée : ${errorMessage(cause)}`);
    } finally {
      decisionInFlight.current = false;
      setBusy(null);
    }
  }

  return {
    busy,
    decide: decideCapability,
    error,
    items,
    notice,
    refresh: refreshCapabilities,
    refreshing,
  };
}

function useApprovalQueues() {
  const approvalQueue = useApprovalQueue();
  const capabilityQueue = useCapabilityQueue();
  const refreshApprovals = approvalQueue.refresh;
  const refreshCapabilities = capabilityQueue.refresh;
  const refreshAll = useCallback(async (clearError = true) => {
    await Promise.all([
      refreshApprovals(clearError),
      refreshCapabilities(clearError),
    ]);
  }, [refreshApprovals, refreshCapabilities]);

  useEffect(() => {
    void refreshAll(false);
  }, [refreshAll]);
  useLiveRefresh(() => refreshAll(false));

  return { approvalQueue, capabilityQueue, refreshAll };
}

function DecisionFeedback({
  capabilityNotice,
  decidedTaskId,
  notice,
  onOpenTask,
}: {
  capabilityNotice: string | null;
  decidedTaskId: string | null;
  notice: string | null;
  onOpenTask: (taskId: string) => void;
}) {
  return (
    <>
      {notice ? (
        <Text accessibilityLiveRegion="polite" selectable style={{ color: COLORS.accent }}>
          {notice}
        </Text>
      ) : null}
      {decidedTaskId ? (
        <ActionButton
          label="Voir le résultat et les preuves"
          onPress={() => onOpenTask(decidedTaskId)}
          testID="decided-task-button"
        />
      ) : null}
      {capabilityNotice ? (
        <Text accessibilityLiveRegion="polite" selectable style={{ color: COLORS.accent }}>
          {capabilityNotice}
        </Text>
      ) : null}
    </>
  );
}

function EmptyApprovalQueue({
  capabilityError,
  capabilityItems,
  capabilityRefreshing,
  error,
  items,
  refreshing,
}: {
  capabilityError: string | null;
  capabilityItems: CapabilityRequestDetail[];
  capabilityRefreshing: boolean;
  error: string | null;
  items: Approval[];
  refreshing: boolean;
}) {
  const unavailable = error !== null || capabilityError !== null;
  const loading = refreshing || capabilityRefreshing;
  if (items.length || capabilityItems.length || loading || unavailable) return null;
  return (
    <EmptyState
      title="Aucun accord en attente"
      subtitle="Les actions sensibles et les demandes iPhone apparaîtront ici avant toute exécution."
    />
  );
}

function PendingDecisionLists({
  approvalItems,
  capabilityBusy,
  capabilityItems,
  capabilityRefreshing,
  deciding,
  lockedApprovalIds,
  offline,
  onApprovalDecision,
  onCapabilityDecision,
  onOpenTask,
}: {
  approvalItems: Approval[];
  capabilityBusy: string | null;
  capabilityItems: CapabilityRequestDetail[];
  capabilityRefreshing: boolean;
  deciding: string | null;
  lockedApprovalIds: ReadonlySet<string>;
  offline: boolean;
  onApprovalDecision: (id: string, decision: ApprovalDecision) => void;
  onCapabilityDecision: (
    requestId: string,
    decision: CapabilityAuthorizationDecision,
  ) => void;
  onOpenTask: (taskId: string) => void;
}) {
  return (
    <>
      <View style={{ gap: 12 }} testID="iphone-capabilities-list">
        {capabilityItems.map((item) => (
          <IPhoneCapabilityCard
            key={item.request_id}
            busy={capabilityBusy}
            locked={capabilityRefreshing || capabilityBusy !== null}
            onDecision={(decision) => onCapabilityDecision(item.request_id, decision)}
            request={item}
          />
        ))}
      </View>
      <View style={{ gap: 12 }} testID="approvals-list">
        {approvalItems.map((item) => (
          <ApprovalDecisionCard
            key={item.id}
            allowTestID={`allow-button-${item.id}`}
            approval={item}
            busy={deciding}
            cardTestID={`approval-card-${item.id}`}
            decisionLocked={offline || lockedApprovalIds.has(item.id)}
            denyTestID={`deny-button-${item.id}`}
            onDecision={(decision) => {
              if (!offline) onApprovalDecision(item.id, decision);
            }}
            onOpenTask={() => onOpenTask(item.task_id)}
          />
        ))}
      </View>
    </>
  );
}

export default function ApprovalsScreen() {
  const router = useRouter();
  const { approvalQueue, capabilityQueue, refreshAll } = useApprovalQueues();
  const refreshing = approvalQueue.refreshing || capabilityQueue.refreshing;

  return (
    <ScreenShell
      title="Accords"
      subtitle="Chaque secret d’autorisation est à usage unique. Seule la récupération explicite d’un secret approuvé mais perdu peut le remplacer."
      onRefresh={() => void refreshAll()}
      refreshing={refreshing}
      testID="approvals-screen"
    >
      <ErrorBanner message={approvalQueue.error} />
      <ErrorBanner message={capabilityQueue.error} />
      <ActionButton
        busy={refreshing}
        disabled={capabilityQueue.busy !== null || approvalQueue.deciding !== null}
        label="Actualiser les accords"
        onPress={() => void refreshAll()}
        testID="refresh-approvals-button"
      />
      <DecisionFeedback
        capabilityNotice={capabilityQueue.notice}
        decidedTaskId={approvalQueue.decidedTaskId}
        notice={approvalQueue.notice}
        onOpenTask={(taskId) =>
          router.push({ pathname: "/task/[id]", params: { id: taskId } })
        }
      />
      <EmptyApprovalQueue
        capabilityError={capabilityQueue.error}
        capabilityItems={capabilityQueue.items}
        capabilityRefreshing={capabilityQueue.refreshing}
        error={approvalQueue.error}
        items={approvalQueue.items}
        refreshing={approvalQueue.refreshing}
      />
      <PendingDecisionLists
        approvalItems={approvalQueue.items}
        capabilityBusy={capabilityQueue.busy}
        capabilityItems={capabilityQueue.items}
        capabilityRefreshing={capabilityQueue.refreshing}
        deciding={approvalQueue.deciding}
        lockedApprovalIds={approvalQueue.lockedApprovalIds}
        offline={approvalQueue.offline}
        onApprovalDecision={(id, decision) => void approvalQueue.decide(id, decision)}
        onCapabilityDecision={(requestId, decision) =>
          void capabilityQueue.decide(requestId, decision)
        }
        onOpenTask={(taskId) =>
          router.push({ pathname: "/task/[id]", params: { id: taskId } })
        }
      />
    </ScreenShell>
  );
}
