import {
  useCallback,
  useEffect,
  useState,
  type PropsWithChildren,
  type ReactNode,
} from "react";
import type { StyleProp, ViewStyle } from "react-native";
import {
  AccessibilityInfo,
  ActivityIndicator,
  AppState,
  Pressable,
  Text,
  View,
} from "react-native";

import type { Approval, TaskStatus } from "@/lib/api/client";

export const COLORS = {
  background: "#09090b",
  panel: "#18181b",
  panelRaised: "#27272a",
  border: "#71717a",
  text: "#fafafa",
  muted: "#a1a1aa",
  // Meets WCAG AA for small text on every surface token, including panelRaised.
  subtle: "#8f8f99",
  accent: "#34d399",
  accentText: "#052e20",
  danger: "#f87171",
  warning: "#fbbf24",
  info: "#38bdf8",
} as const;

export function useAccessibilityAnnouncement(message: string | null) {
  useEffect(() => {
    if (message) AccessibilityInfo.announceForAccessibility(message);
  }, [message]);
}

const STATUS: Record<TaskStatus, { label: string; color: string }> = {
  created: { label: "Créée", color: COLORS.muted },
  planned: { label: "Planifiée", color: COLORS.info },
  waiting_permission: { label: "Permission", color: COLORS.warning },
  queued: { label: "En file", color: COLORS.muted },
  running: { label: "En cours", color: COLORS.info },
  blocked: { label: "Bloquée", color: COLORS.warning },
  completed: { label: "Terminée", color: COLORS.accent },
  failed: { label: "Échouée", color: COLORS.danger },
  cancelled: { label: "Annulée", color: COLORS.subtle },
};

export function Card({
  children,
  style,
  testID,
}: PropsWithChildren<{ style?: StyleProp<ViewStyle>; testID?: string }>) {
  return (
    <View
      testID={testID}
      style={[
        {
          backgroundColor: COLORS.panel,
          borderColor: COLORS.border,
          borderRadius: 16,
          borderWidth: 1,
          gap: 10,
          padding: 16,
        },
        style,
      ]}
    >
      {children}
    </View>
  );
}

export function StatusBadge({ status }: { status: TaskStatus }) {
  const value = STATUS[status];
  return (
    <View
      accessibilityLabel={`Statut: ${value.label}`}
      style={{
        alignSelf: "flex-start",
        backgroundColor: `${value.color}1f`,
        borderColor: `${value.color}66`,
        borderRadius: 999,
        borderWidth: 1,
        paddingHorizontal: 10,
        paddingVertical: 5,
      }}
    >
      <Text style={{ color: value.color, fontSize: 12, fontWeight: "700" }}>
        {value.label}
      </Text>
    </View>
  );
}

type ButtonVariant = "accent" | "neutral" | "danger";

export function ActionButton({
  label,
  accessibilityLabel,
  accessibilityHint,
  onPress,
  disabled = false,
  busy = false,
  variant = "neutral",
  style,
  testID,
}: {
  label: string;
  accessibilityLabel?: string;
  accessibilityHint?: string;
  onPress: () => void;
  disabled?: boolean;
  busy?: boolean;
  variant?: ButtonVariant;
  style?: StyleProp<ViewStyle>;
  testID?: string;
}) {
  const backgroundColor = variant === "accent" ? COLORS.accent : COLORS.panelRaised;
  const color =
    variant === "accent"
      ? COLORS.accentText
      : variant === "danger"
        ? COLORS.danger
        : COLORS.text;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityHint={accessibilityHint}
      accessibilityLabel={accessibilityLabel ?? label}
      accessibilityState={{ busy, disabled: disabled || busy }}
      disabled={disabled || busy}
      onPress={onPress}
      testID={testID}
      style={({ pressed }) => [
        {
          alignItems: "center",
          backgroundColor,
          borderColor: variant === "accent" ? COLORS.accent : COLORS.border,
          borderRadius: 12,
          borderWidth: 1,
          flexDirection: "row",
          justifyContent: "center",
          minHeight: 46,
          opacity: disabled || busy ? 0.45 : pressed ? 0.75 : 1,
          paddingHorizontal: 16,
          paddingVertical: 11,
        },
        style,
      ]}
    >
      {busy ? <ActivityIndicator color={color} size="small" /> : null}
      <Text style={{ color, fontSize: 14, fontWeight: "700" }}>
        {busy ? `${label}…` : label}
      </Text>
    </Pressable>
  );
}

const APPROVAL_RISK_LABEL = { low: "faible", medium: "moyen", high: "élevé" } as const;

function useApprovalExpiry(expiresAt: string, status: Approval["status"]): boolean {
  const [observedAt, setObservedAt] = useState(() => Date.now());
  const expiresAtMs = Date.parse(expiresAt);
  const pending = status === "pending";
  const invalidDeadline = !Number.isFinite(expiresAtMs);
  const expired =
    status === "expired" ||
    (pending && (invalidDeadline || observedAt >= expiresAtMs));

  useEffect(() => {
    if (!pending || invalidDeadline || Date.now() >= expiresAtMs) return;

    const refreshClock = () => setObservedAt(Date.now());
    const remaining = Math.max(0, expiresAtMs - Date.now());
    const timer = setTimeout(refreshClock, Math.min(remaining + 1, 2_147_483_647));
    const subscription = AppState.addEventListener("change", (nextState) => {
      if (nextState === "active") refreshClock();
    });
    return () => {
      clearTimeout(timer);
      subscription.remove();
    };
  }, [expiresAtMs, invalidDeadline, observedAt, pending]);

  return expired;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isPositiveSafeInteger(value: unknown): boolean {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNonEmptyStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.length > 0 && value.every(isNonEmptyString);
}

const APPROVAL_SUMMARIES: Record<string, string> = {
  "workspace.list_dir": "List a workspace directory",
  "workspace.read_text": "Read a workspace file",
  "workspace.write_text": "Write text to a workspace file",
  "process.run": "Run a sandboxed process",
};

const APPROVAL_RISKS = new Set<unknown>(["low", "medium", "high"]);
const WORKSPACE_ACTIONS = new Set([
  "workspace.list_dir",
  "workspace.read_text",
  "workspace.write_text",
]);

const ACTION_PREVIEW_KEYS = new Set([
  "operation",
  "target",
  "working_directory",
  "command",
  "details",
  "arguments_redacted",
]);

function hasCanonicalActionDigest(value: unknown): boolean {
  return typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value);
}

function hasApprovalActionIdentity(
  record: Record<string, unknown>,
  action: string,
): boolean {
  return [
    isNonEmptyString(record.id),
    isNonEmptyString(record.task_id),
    isNonEmptyString(record.tool_call_id),
    hasCanonicalActionDigest(record.action_digest),
    APPROVAL_SUMMARIES[action] === record.summary,
    APPROVAL_RISKS.has(record.risk),
  ].every(Boolean);
}

function hasBaseActionPreview(preview: Record<string, unknown>): boolean {
  return [
    Object.keys(preview).every((key) => ACTION_PREVIEW_KEYS.has(key)),
    isNonEmptyString(preview.operation),
    isNonEmptyStringArray(preview.details),
    typeof preview.arguments_redacted === "boolean",
  ].every(Boolean);
}

function hasProcessActionPreview(preview: Record<string, unknown>): boolean {
  return [
    isNonEmptyString(preview.target),
    isNonEmptyString(preview.working_directory),
    isNonEmptyStringArray(preview.command),
  ].every(Boolean);
}

function hasWorkspaceActionPreview(
  action: string,
  preview: Record<string, unknown>,
): boolean {
  const workspaceShape = [
    WORKSPACE_ACTIONS.has(action),
    isNonEmptyString(preview.target),
    preview.working_directory === undefined,
    preview.command === undefined,
  ].every(Boolean);
  if (!workspaceShape) return false;
  return action !== "workspace.write_text" || preview.arguments_redacted === true;
}

function hasExactApprovalAction(approval: Approval): boolean {
  const record = approval as unknown as Record<string, unknown>;
  const action = record.action;
  const preview = record.action_preview;
  if (!isNonEmptyString(action) || !isRecord(preview)) return false;
  const commonShape = [
    hasApprovalActionIdentity(record, action),
    hasBaseActionPreview(preview),
  ].every(Boolean);
  if (!commonShape) return false;
  return action === "process.run"
    ? hasProcessActionPreview(preview)
    : hasWorkspaceActionPreview(action, preview);
}

function hasTrustedConsentContext(approval: Approval): boolean {
  const { requester, policy } = approval;
  return [
    approval.consent_context_valid === true,
    requester?.type === "device",
    isNonEmptyString(requester?.id),
    isNonEmptyString(requester?.name),
    policy?.decision === "ask",
    isNonEmptyString(policy?.rule_id),
    isNonEmptyString(policy?.reason),
    isNonEmptyString(approval.affected_data_summary),
    isPositiveSafeInteger(approval.audit_id),
  ].every(Boolean);
}

type ApprovalDecisionCardProps = {
  approval: Approval;
  busy: string | null;
  decisionLocked?: boolean;
  onDecision: (decision: "approve" | "deny") => void;
  onOpenTask?: () => void;
  allowTestID?: string;
  denyTestID?: string;
  cardTestID?: string;
};

export function useApprovalDecisionLocks() {
  const [lockedApprovalIds, setLockedApprovalIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );

  const lockApproval = useCallback((approvalId: string) => {
    setLockedApprovalIds((current) => {
      if (current.has(approvalId)) return current;
      const next = new Set(current);
      next.add(approvalId);
      return next;
    });
  }, []);

  const reconcileApprovals = useCallback((approvals: Approval[]) => {
    const stillPending = new Set(
      approvals.filter((approval) => approval.status === "pending").map((approval) => approval.id),
    );
    setLockedApprovalIds((current) => {
      const next = new Set([...current].filter((approvalId) => stillPending.has(approvalId)));
      if (next.size === current.size) return current;
      return next;
    });
  }, []);

  return { lockedApprovalIds, lockApproval, reconcileApprovals };
}

type ApprovalDecision = "approve" | "deny";

function ApprovalRiskHeader({ approval, expired }: { approval: Approval; expired: boolean }) {
  return (
    <View style={{ alignItems: "center", flexDirection: "row", justifyContent: "space-between" }}>
      <Text
        style={{
          color: approval.risk === "high" ? COLORS.danger : COLORS.warning,
          fontSize: 12,
          fontWeight: "800",
          textTransform: "uppercase",
        }}
      >
        Risque {APPROVAL_RISK_LABEL[approval.risk]}
      </Text>
      <Text
        testID={`approval-status-${approval.id}`}
        style={{ color: expired ? COLORS.danger : COLORS.subtle, fontSize: 11, fontWeight: expired ? "800" : "400" }}
      >
        {expired ? "Expiré" : timeAgo(approval.created_at)}
      </Text>
    </View>
  );
}

function ApprovalExactAction({
  approval,
  actionValid,
}: {
  approval: Approval;
  actionValid: boolean;
}) {
  if (!actionValid) {
    return (
      <>
        <Text
          accessibilityRole="header"
          style={{ color: COLORS.danger, fontSize: 12, fontWeight: "800", marginTop: 5 }}
        >
          Action exacte indisponible
        </Text>
      </>
    );
  }

  return (
    <>
      <Text
        accessibilityRole="header"
        style={{ color: COLORS.info, fontSize: 12, fontWeight: "800", marginTop: 5 }}
      >
        Action exacte liée
      </Text>
      <Text selectable style={{ color: COLORS.text, fontSize: 16, fontWeight: "700" }}>
        {approval.action_preview.operation}
      </Text>
      {approval.action_preview.target ? (
        <Text selectable style={{ color: COLORS.text, lineHeight: 20 }}>
          Cible exacte : {approval.action_preview.target}
        </Text>
      ) : null}
      {approval.action_preview.working_directory ? (
        <Text selectable style={{ color: COLORS.text, lineHeight: 20 }}>
          Dossier de travail : {approval.action_preview.working_directory}
        </Text>
      ) : null}
      {approval.action_preview.command?.length ? (
        <Text selectable style={{ color: COLORS.text, lineHeight: 20 }}>
          Commande :{" "}
          {approval.action_preview.command
            .map((value) => JSON.stringify(value))
            .join(" ")}
        </Text>
      ) : null}
      {approval.action_preview.details.map((detail, index) => (
        <Text
          key={`${index}:${detail}`}
          selectable
          style={{ color: COLORS.muted, lineHeight: 20 }}
        >
          {detail}
        </Text>
      ))}
    </>
  );
}

function ApprovalValidityAlerts({
  actionUnavailable,
  bindingValid,
  consentUnavailable,
  expired,
}: {
  actionUnavailable: boolean;
  bindingValid: boolean;
  consentUnavailable: boolean;
  expired: boolean;
}) {
  return (
    <>
      {!bindingValid ? (
        <Text accessibilityRole="alert" selectable style={{ color: COLORS.danger, lineHeight: 20 }}>
          Liaison invalide : cet accord ne peut pas être autorisé.
        </Text>
      ) : null}
      {consentUnavailable ? (
        <Text accessibilityRole="alert" selectable style={{ color: COLORS.danger, lineHeight: 20 }}>
          Contexte de consentement invalide : cet accord ne peut pas être autorisé.
        </Text>
      ) : null}
      {actionUnavailable ? (
        <Text accessibilityRole="alert" selectable style={{ color: COLORS.danger, lineHeight: 20 }}>
          Les détails liés à cette action sont incomplets ou invalides. Aucune décision n’est permise.
        </Text>
      ) : null}
      {expired ? (
        <Text accessibilityRole="alert" selectable style={{ color: COLORS.danger, lineHeight: 20 }}>
          Expiré : cette demande n’est plus actionnable.
        </Text>
      ) : null}
    </>
  );
}

function requesterLabel(approval: Approval): string {
  const { requester } = approval;
  return requester
    ? `${requester.name} (${requester.type} ${requester.id})`
    : "indisponible";
}

function ApprovalTrustedHeading({ valid }: { valid: boolean }) {
  return (
    <Text
      accessibilityRole="header"
      style={{
        color: valid ? COLORS.info : COLORS.danger,
        fontSize: 12,
        fontWeight: "800",
        textTransform: "uppercase",
      }}
    >
      {valid ? "Contexte vérifié par le serveur" : "Contexte serveur non vérifiable"}
    </Text>
  );
}

function ApprovalProvenance({ approval }: { approval: Approval }) {
  return (
    <>
      <Text selectable style={{ color: COLORS.text, lineHeight: 20 }}>
        Demandeur authentifié : {requesterLabel(approval)}
      </Text>
      <Text selectable style={{ color: COLORS.text, lineHeight: 20 }}>
        Motif de la politique : {approval.policy?.reason ?? "indisponible"}
      </Text>
      <Text selectable style={{ color: COLORS.muted, fontSize: 12, lineHeight: 18 }}>
        Règle : {approval.policy?.rule_id ?? "indisponible"}
      </Text>
      <Text selectable style={{ color: COLORS.text, lineHeight: 20 }}>
        Données touchées : {approval.affected_data_summary ?? "indisponibles"}
      </Text>
      <Text selectable style={{ color: COLORS.muted, fontSize: 12 }}>
        Audit : {approval.audit_id ?? "indisponible"} · Accord : {approval.id}
      </Text>
    </>
  );
}

function ApprovalBindingMetadata({ approval }: { approval: Approval }) {
  const digest =
    typeof approval.action_digest === "string" &&
    /^sha256:[0-9a-f]{64}$/.test(approval.action_digest)
      ? approval.action_digest
      : "indisponible ou invalide";
  return (
    <>
      <Text selectable style={{ color: COLORS.subtle, fontSize: 11 }}>
        Tâche {approval.task_id} · appel {approval.tool_call_id} · expiration{" "}
        {Number.isFinite(Date.parse(approval.expires_at))
          ? new Date(approval.expires_at).toLocaleTimeString()
          : "invalide"}
      </Text>
      <Text style={{ color: COLORS.subtle, fontSize: 10, lineHeight: 15 }}>
        L’empreinte lie cet accord à l’appel, l’outil et tous ses arguments exacts.
      </Text>
      <Text selectable style={{ color: COLORS.subtle, fontSize: 10 }}>
        Empreinte {digest}
      </Text>
    </>
  );
}

function ApprovalTrustedContext({
  actionValid,
  approval,
  consentUnavailable,
  expired,
  trustedContextValid,
}: {
  actionValid: boolean;
  approval: Approval;
  consentUnavailable: boolean;
  expired: boolean;
  trustedContextValid: boolean;
}) {
  return (
    <View
      testID={`approval-trusted-context-${approval.id}`}
      style={{
        backgroundColor: COLORS.panelRaised,
        borderColor: trustedContextValid ? `${COLORS.info}66` : `${COLORS.danger}66`,
        borderRadius: 12,
        borderWidth: 1,
        gap: 7,
        padding: 12,
      }}
    >
      <ApprovalTrustedHeading valid={trustedContextValid} />
      <ApprovalProvenance approval={approval} />
      <ApprovalExactAction actionValid={actionValid} approval={approval} />
      <ApprovalValidityAlerts
        actionUnavailable={!actionValid}
        bindingValid={approval.binding_valid === true}
        consentUnavailable={consentUnavailable}
        expired={expired}
      />
      <ApprovalBindingMetadata approval={approval} />
    </View>
  );
}

function ApprovalModelContext({ approval }: { approval: Approval }) {
  const summary = isNonEmptyString(approval.summary)
    ? approval.summary
    : "indisponible";
  return (
    <View
      testID={`approval-model-context-${approval.id}`}
      style={{
        backgroundColor: `${COLORS.warning}0d`,
        borderColor: `${COLORS.warning}55`,
        borderRadius: 12,
        borderWidth: 1,
        gap: 6,
        padding: 12,
      }}
    >
      <Text
        accessibilityRole="header"
        style={{
          color: COLORS.warning,
          fontSize: 12,
          fontWeight: "800",
          textTransform: "uppercase",
        }}
      >
        Détails du modèle masqués
      </Text>
      <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>
        Libellé public : {summary}
      </Text>
      <Text style={{ color: COLORS.subtle, fontSize: 11, lineHeight: 16 }}>
        Le texte libre de la proposition n’est pas exposé pour un appel d’outil.
      </Text>
      <Text style={{ color: COLORS.subtle, fontSize: 11, lineHeight: 16 }}>
        Ce texte n’autorise pas l’action.
      </Text>
    </View>
  );
}

type ApprovalControlState = {
  decisionLocked: boolean;
  decisionUnavailable: boolean;
  expired: boolean;
};

const APPROVAL_DECISION_HINTS = {
  approve: {
    available: "Autorise uniquement l’appel exact affiché sur cette carte.",
    expired: "Cette demande a expiré et ne peut plus être autorisée.",
  },
  deny: {
    available: "Refuse l’appel exact affiché sur cette carte.",
    expired: "Cette demande a expiré et n’est plus actionnable.",
  },
} as const;

function approvalDecisionHint(
  decision: ApprovalDecision,
  state: ApprovalControlState,
): string {
  if (state.decisionLocked) {
    return "Une décision a déjà été transmise. Actualise les preuves avant toute autre action.";
  }
  if (state.expired) return APPROVAL_DECISION_HINTS[decision].expired;
  if (state.decisionUnavailable) {
    return "Les preuves requises sont incomplètes; cette action est désactivée.";
  }
  return APPROVAL_DECISION_HINTS[decision].available;
}

function approvalOperation(approval: Approval): string {
  const preview = (approval as unknown as { action_preview?: unknown }).action_preview;
  if (!isRecord(preview) || !isNonEmptyString(preview.operation)) {
    return "action liée indisponible";
  }
  return preview.operation;
}

function ApprovalDecisionControls({
  approval,
  allowTestID,
  busy,
  decisionLocked,
  decisionUnavailable,
  denyTestID,
  expired,
  onDecision,
}: {
  approval: Approval;
  allowTestID?: string;
  busy: string | null;
  decisionLocked: boolean;
  decisionUnavailable: boolean;
  denyTestID?: string;
  expired: boolean;
  onDecision: (decision: ApprovalDecision) => void;
}) {
  const operation = approvalOperation(approval);
  const controlState = { decisionLocked, decisionUnavailable, expired };
  return (
    <View style={{ flexDirection: "row", gap: 8 }}>
      <ActionButton
        accessibilityHint={approvalDecisionHint("approve", controlState)}
        accessibilityLabel={`Autoriser une fois : ${operation}`}
        busy={busy === `${approval.id}:approve`}
        disabled={Boolean(busy) || decisionUnavailable}
        label="Autoriser une fois"
        onPress={() => onDecision("approve")}
        style={{ flex: 1 }}
        testID={allowTestID}
        variant="accent"
      />
      <ActionButton
        accessibilityHint={approvalDecisionHint("deny", controlState)}
        accessibilityLabel={`Refuser : ${operation}`}
        busy={busy === `${approval.id}:deny`}
        disabled={Boolean(busy) || decisionUnavailable}
        label="Refuser"
        onPress={() => onDecision("deny")}
        style={{ flex: 1 }}
        testID={denyTestID}
        variant="danger"
      />
    </View>
  );
}

type ApprovalValidationState = {
  actionValid: boolean;
  consentUnavailable: boolean;
  decisionUnavailable: boolean;
  trustedContextValid: boolean;
};

function approvalValidationState(
  approval: Approval,
  expired: boolean,
  decisionLocked: boolean,
): ApprovalValidationState {
  const consentContextValid = hasTrustedConsentContext(approval);
  const actionValid = hasExactApprovalAction(approval);
  const bindingValid = approval.binding_valid === true;
  const actionable = [
    approval.status === "pending",
    !expired,
    bindingValid,
    consentContextValid,
    actionValid,
    !decisionLocked,
  ].every(Boolean);
  return {
    actionValid,
    consentUnavailable: !consentContextValid,
    decisionUnavailable: !actionable,
    trustedContextValid: [consentContextValid, actionValid, bindingValid].every(Boolean),
  };
}

function isApprovalExpiredAtPress(approval: Approval): boolean {
  if (approval.status === "expired") return true;
  if (approval.status !== "pending") return false;
  const expiresAtMs = Date.parse(approval.expires_at);
  return !Number.isFinite(expiresAtMs) || Date.now() >= expiresAtMs;
}

function canSubmitApprovalDecision(
  approval: Approval,
  busy: string | null,
  decisionLocked: boolean,
): boolean {
  return [
    approval.status === "pending",
    !isApprovalExpiredAtPress(approval),
    approval.binding_valid === true,
    hasTrustedConsentContext(approval),
    hasExactApprovalAction(approval),
    !decisionLocked,
    !busy,
  ].every(Boolean);
}

function ApprovalDecisionLockNotice({ locked }: { locked: boolean }) {
  if (!locked) return null;
  return (
    <Text accessibilityRole="alert" selectable style={{ color: COLORS.warning, lineHeight: 20 }}>
      Décision déjà transmise : cette carte reste verrouillée jusqu’à une actualisation authentifiée.
    </Text>
  );
}

function ApprovalTaskLink({ onOpenTask }: { onOpenTask?: () => void }) {
  if (!onOpenTask) return null;
  return <ActionButton label="Voir la tâche et ses preuves" onPress={onOpenTask} />;
}

export function ApprovalDecisionCard(props: ApprovalDecisionCardProps) {
  const { approval } = props;
  return (
    <ApprovalDecisionCardContent
      key={`${approval.id}:${approval.status}:${approval.expires_at}`}
      {...props}
    />
  );
}

function ApprovalDecisionCardContent({
  approval,
  busy,
  decisionLocked = false,
  onDecision,
  onOpenTask,
  allowTestID,
  denyTestID,
  cardTestID,
}: ApprovalDecisionCardProps) {
  const expired = useApprovalExpiry(approval.expires_at, approval.status);
  const validation = approvalValidationState(approval, expired, decisionLocked);

  const decide = (decision: ApprovalDecision) => {
    if (!canSubmitApprovalDecision(approval, busy, decisionLocked)) return;
    onDecision(decision);
  };

  return (
    <Card testID={cardTestID}>
      <ApprovalRiskHeader approval={approval} expired={expired} />
      <ApprovalTrustedContext
        actionValid={validation.actionValid}
        approval={approval}
        consentUnavailable={validation.consentUnavailable}
        expired={expired}
        trustedContextValid={validation.trustedContextValid}
      />
      <ApprovalModelContext approval={approval} />
      <ApprovalDecisionLockNotice locked={decisionLocked} />
      <ApprovalTaskLink onOpenTask={onOpenTask} />
      <ApprovalDecisionControls
        approval={approval}
        allowTestID={allowTestID}
        busy={busy}
        decisionLocked={decisionLocked}
        decisionUnavailable={validation.decisionUnavailable}
        denyTestID={denyTestID}
        expired={expired}
        onDecision={decide}
      />
    </Card>
  );
}

export function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <View
      accessible
      accessibilityLiveRegion="polite"
      accessibilityRole="alert"
      style={{
        backgroundColor: `${COLORS.danger}14`,
        borderColor: `${COLORS.danger}66`,
        borderRadius: 12,
        borderWidth: 1,
        padding: 12,
      }}
    >
      <Text selectable style={{ color: COLORS.danger, lineHeight: 20 }}>
        {message}
      </Text>
    </View>
  );
}

export function EmptyState({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <View style={{ alignItems: "center", gap: 8, paddingHorizontal: 24, paddingVertical: 48 }}>
      <Text style={{ color: COLORS.text, fontSize: 17, fontWeight: "700" }}>{title}</Text>
      <Text style={{ color: COLORS.muted, lineHeight: 20, textAlign: "center" }}>
        {subtitle}
      </Text>
    </View>
  );
}

export function SectionTitle({ title, right }: { title: string; right?: ReactNode }) {
  return (
    <View style={{ alignItems: "center", flexDirection: "row", justifyContent: "space-between" }}>
      <Text
        accessibilityRole="header"
        style={{
          color: COLORS.muted,
          fontSize: 12,
          fontWeight: "800",
          letterSpacing: 1.1,
          textTransform: "uppercase",
        }}
      >
        {title}
      </Text>
      {right}
    </View>
  );
}

export function timeAgo(iso: string | null): string {
  if (!iso) return "jamais";
  const timestamp = new Date(iso).getTime();
  if (!Number.isFinite(timestamp)) return "date inconnue";
  const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60_000));
  if (minutes < 1) return "à l’instant";
  if (minutes < 60) return `il y a ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `il y a ${hours} h`;
  return `il y a ${Math.floor(hours / 24)} j`;
}
