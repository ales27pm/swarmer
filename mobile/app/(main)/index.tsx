import { type Dispatch, useCallback, useEffect, useReducer, useRef } from "react";
import { AppState, Pressable, Text, TextInput, View } from "react-native";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";

import { ScreenShell } from "@/components/screen-shell";
import {
  ActionButton,
  COLORS,
  EmptyState,
  ErrorBanner,
  SectionTitle,
  timeAgo,
  useAccessibilityAnnouncement,
} from "@/components/swarm-ui";
import {
  bootstrapSync,
  listMessages,
  planTask,
  sendChat,
  type Bootstrap,
  type Message,
  type Task,
  type ToolCall,
} from "@/lib/api/client";
import { useLiveRefresh, useLiveSync } from "@/lib/sync/live-sync-context";
import type { LiveSyncState } from "@/lib/sync/live-sync";

const SUGGESTIONS = [
  "Liste les fichiers à la racine du projet.",
  "Vérifie les tests actuels sans modifier le code.",
];

type PlanningResult = ToolCall | { task_id: string; proposal: unknown; task: Task | null };

type ChatState = {
  input: string;
  interactionMode: "chat" | "task";
  conversationId: string | undefined;
  messages: Message[];
  lastTask: Task | null;
  bootstrap: Bootstrap | null;
  notice: string;
  error: string | null;
  busy: boolean;
  refreshing: boolean;
};

type ChatStatePatch = Partial<ChatState>;
type ChatDispatch = Dispatch<ChatStatePatch>;

const INITIAL_CHAT_STATE: ChatState = {
  input: "",
  interactionMode: "chat",
  conversationId: undefined,
  messages: [],
  lastTask: null,
  bootstrap: null,
  notice: "Prêt à discuter. Passe en mode Tâche lorsque tu veux agir.",
  error: null,
  busy: false,
  refreshing: false,
};

function mergeChatState(state: ChatState, patch: ChatStatePatch): ChatState {
  return { ...state, ...patch };
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

function planningStatus(result: PlanningResult): string {
  if ("tool_name" in result) {
    if (result.status === "waiting_permission") {
      return "Une autorisation unique est requise avant l’exécution.";
    }
    if (result.status === "completed") {
      const publicResult: unknown = result.result;
      if (
        publicResult === null ||
        typeof publicResult !== "object" ||
        Array.isArray(publicResult)
      ) {
        return "L’appel signale une fin sans résultat d’exécution vérifié; aucune réussite n’est confirmée.";
      }
      return "L’exécuteur local a terminé et enregistré un résultat vérifié.";
    }
    if (result.status === "failed") return "L’exécution a échoué. Consulte la tâche.";
    return `Appel ${result.tool_name}: ${result.status}.`;
  }
  return "Le modèle a produit une proposition, sans prétendre l’avoir exécutée.";
}

function taskAfterPlanning(result: PlanningResult, fallback: Task): Task {
  if ("task" in result && result.task) return result.task;
  return fallback;
}

function failedAttemptNotice(createdTask: Task | null): string {
  return createdTask
    ? "La tâche a été créée, mais aucune planification ou exécution réussie n’a été confirmée."
    : "Aucune création de tâche n’a été confirmée pour cette tentative.";
}

async function refreshBootstrap(
  dispatch: ChatDispatch,
  updateError = true,
  isCurrent: () => boolean = () => true,
) {
  if (!isCurrent()) return;
  dispatch({ refreshing: true });
  try {
    const bootstrap = await bootstrapSync(isCurrent);
    if (!isCurrent()) return;
    dispatch(updateError ? { bootstrap, error: null } : { bootstrap });
  } catch (cause) {
    if (!isCurrent()) return;
    dispatch(
      updateError
        ? { bootstrap: null, error: errorMessage(cause) }
        : { bootstrap: null },
    );
  } finally {
    if (isCurrent()) dispatch({ refreshing: false });
  }
}

async function refreshConversation(conversationId: string | undefined, dispatch: ChatDispatch) {
  if (!conversationId) return;
  try {
    dispatch({ messages: await listMessages(conversationId) });
  } catch {
    // Keep the rendered conversation while preserving the primary error.
  }
}

async function submitChatIntent(
  state: ChatState,
  dispatch: ChatDispatch,
  refreshStatus: (updateError?: boolean) => Promise<void>,
) {
  const content = state.input.trim();
  if (!content || state.busy) return;

  dispatch({
    busy: true,
    error: null,
    notice: state.interactionMode === "task"
      ? "Création de la tâche authentifiée…"
      : "Le modèle local prépare une réponse…",
  });
  let activeConversation = state.conversationId;
  let createdTask: Task | null = null;
  try {
    const chat = await sendChat(
      content,
      state.conversationId,
      "normal",
      state.interactionMode === "task",
    );
    createdTask = chat.task;
    activeConversation = chat.conversation_id;
    dispatch({
      conversationId: chat.conversation_id,
      lastTask: chat.task ?? state.lastTask,
      input: "",
    });
    dispatch({ messages: await listMessages(chat.conversation_id) });
    if (chat.task) {
      dispatch({ notice: "Le modèle du control plane prépare un plan…" });
      const result = await planTask(chat.task.id);
      dispatch({ notice: planningStatus(result) });
      dispatch({ lastTask: taskAfterPlanning(result, chat.task) });
    } else {
      dispatch({ notice: "Réponse conversationnelle reçue. Aucune tâche n’a été créée." });
    }
  } catch (cause) {
    dispatch({ error: errorMessage(cause), notice: failedAttemptNotice(createdTask) });
  } finally {
    await refreshConversation(activeConversation, dispatch);
    await refreshStatus(false);
    dispatch({ busy: false });
  }
}

function useChatController() {
  const [state, dispatch] = useReducer(mergeChatState, INITIAL_CHAT_STATE);
  const refreshEpoch = useRef(0);
  useAccessibilityAnnouncement(state.notice);
  const setInput = useCallback((input: string) => dispatch({ input }), []);

  const refreshStatus = useCallback(
    (updateError = true) => {
      const requestEpoch = ++refreshEpoch.current;
      return refreshBootstrap(
        dispatch,
        updateError,
        () => requestEpoch === refreshEpoch.current,
      );
    },
    [dispatch],
  );

  useFocusEffect(
    useCallback(() => {
      void refreshStatus(false);
      const subscription = AppState.addEventListener("change", (nextState) => {
        if (nextState === "active") void refreshStatus(false);
      });

      return () => {
        refreshEpoch.current += 1;
        subscription.remove();
      };
    }, [refreshStatus]),
  );
  useLiveRefresh(() => refreshStatus(false));

  return {
    ...state,
    refreshStatus,
    setInteractionMode: (interactionMode: "chat" | "task") => dispatch({ interactionMode }),
    setInput,
    submit: () => submitChatIntent(state, dispatch, refreshStatus),
  };
}

function pendingAgreementLabel(pending: number): string {
  return `${pending} accord${pending > 1 ? "s" : ""} en attente`;
}

type ConnectionPanelProps = {
  bootstrap: Bootstrap | null;
  liveState: LiveSyncState;
  pending: number;
  onOpenSettings: () => void;
  onOpenApprovals: () => void;
};

function ConnectionPanel({
  bootstrap,
  liveState,
  pending,
  onOpenSettings,
  onOpenApprovals,
}: ConnectionPanelProps) {
  return (
    <>
      <View style={{ alignItems: "center", flexDirection: "row", gap: 8 }}>
        <View
          style={{
            backgroundColor: bootstrap ? COLORS.accent : COLORS.danger,
            borderRadius: 999,
            height: 8,
            width: 8,
          }}
        />
        <Text style={{ color: COLORS.muted, flex: 1, fontSize: 12 }}>
          {bootstrap ? "Control plane authentifié" : "Control plane injoignable ou non jumelé"}
          {pending ? ` · ${pendingAgreementLabel(pending)}` : ""}
        </Text>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Ouvrir les réglages"
          hitSlop={12}
          onPress={onOpenSettings}
          testID="open-settings-button"
        >
          <Text style={{ color: COLORS.accent, fontSize: 13, fontWeight: "700" }}>Réglages</Text>
        </Pressable>
      </View>
      <Text style={{ color: liveState === "connected" ? COLORS.accent : COLORS.subtle, fontSize: 11 }}>
        {liveState === "connected"
          ? "Temps réel connecté"
          : liveState === "connecting"
            ? "Connexion temps réel en cours…"
            : liveState === "paused"
              ? "Temps réel en pause"
              : bootstrap
                ? "Temps réel déconnecté — actualisation REST disponible"
                : "Temps réel non établi"}
      </Text>

      {pending ? (
        <ActionButton
          label={`Voir ${pendingAgreementLabel(pending)}`}
          onPress={onOpenApprovals}
          testID="open-pending-approvals"
        />
      ) : null}
    </>
  );
}

function ChatMessage({ message }: { message: Message }) {
  const isUser = message.role === "user";
  const isProposalOnly = message.metadata?.verified_status === "proposal_only";
  return (
    <View style={{ alignItems: isUser ? "flex-end" : "flex-start", gap: 4 }}>
      <View
        style={{
          backgroundColor: isUser ? COLORS.accent : COLORS.panel,
          borderColor: isUser ? COLORS.accent : COLORS.border,
          borderRadius: 16,
          borderWidth: 1,
          maxWidth: "88%",
          paddingHorizontal: 14,
          paddingVertical: 11,
        }}
      >
        {isProposalOnly ? (
          <Text
            style={{
              color: COLORS.warning,
              fontSize: 11,
              fontWeight: "800",
              marginBottom: 6,
              textTransform: "uppercase",
            }}
          >
            Proposition du modèle — non vérifiée
          </Text>
        ) : null}
        <Text
          selectable
          style={{ color: isUser ? COLORS.accentText : COLORS.text, lineHeight: 20 }}
        >
          {message.content}
        </Text>
      </View>
      <Text style={{ color: COLORS.subtle, fontSize: 10 }}>
        {message.agent_id ?? message.role} · {timeAgo(message.created_at)}
      </Text>
    </View>
  );
}

function MessageList({ messages }: { messages: Message[] }) {
  return (
    <View style={{ gap: 10 }} testID="chat-messages">
      {messages.map((message) => (
        <ChatMessage key={message.id} message={message} />
      ))}
    </View>
  );
}

function Suggestions({ onSelect }: { onSelect: (suggestion: string) => void }) {
  return (
    <View style={{ gap: 8 }}>
      {SUGGESTIONS.map((suggestion) => (
        <Pressable
          accessibilityRole="button"
          key={suggestion}
          onPress={() => onSelect(suggestion)}
          style={({ pressed }) => ({
            backgroundColor: COLORS.panel,
            borderColor: COLORS.border,
            borderRadius: 12,
            borderWidth: 1,
            minHeight: 46,
            opacity: pressed ? 0.75 : 1,
            padding: 13,
          })}
        >
          <Text style={{ color: COLORS.muted, lineHeight: 19 }}>{suggestion}</Text>
        </Pressable>
      ))}
    </View>
  );
}

function Conversation({
  messages,
  onSelectSuggestion,
}: {
  messages: Message[];
  onSelectSuggestion: (suggestion: string) => void;
}) {
  if (messages.length) return <MessageList messages={messages} />;
  return (
    <>
      <EmptyState
        title="Console du swarm"
        subtitle="Décris une intention. Le modèle du control plane propose; l’exécuteur prouve; les actions sensibles attendent ton accord."
      />
      <Suggestions onSelect={onSelectSuggestion} />
    </>
  );
}

type IntentComposerProps = {
  input: string;
  interactionMode: "chat" | "task";
  busy: boolean;
  authenticated: boolean;
  notice: string;
  onChangeInput: (input: string) => void;
  onChangeMode: (mode: "chat" | "task") => void;
  onSubmit: () => void;
};

function IntentComposer({
  input,
  interactionMode,
  busy,
  authenticated,
  notice,
  onChangeInput,
  onChangeMode,
  onSubmit,
}: IntentComposerProps) {
  return (
    <>
      <SectionTitle title={interactionMode === "chat" ? "Discussion" : "Nouvelle tâche"} />
      <View style={{ flexDirection: "row", gap: 8 }}>
        {(["chat", "task"] as const).map((mode) => (
          <Pressable
            accessibilityRole="button"
            key={mode}
            onPress={() => onChangeMode(mode)}
            style={{
              backgroundColor: interactionMode === mode ? COLORS.accent : COLORS.panel,
              borderColor: interactionMode === mode ? COLORS.accent : COLORS.border,
              borderRadius: 12,
              borderWidth: 1,
              flex: 1,
              padding: 12,
            }}
            testID={`interaction-mode-${mode}`}
          >
            <Text style={{
              color: interactionMode === mode ? COLORS.accentText : COLORS.text,
              fontWeight: "700",
              textAlign: "center",
            }}>
              {mode === "chat" ? "Discuter" : "Tâche"}
            </Text>
          </Pressable>
        ))}
      </View>
      <Text style={{ color: COLORS.muted, fontSize: 12, fontWeight: "700" }}>
        {interactionMode === "chat" ? "Message" : "Intention pour le swarm"}
      </Text>
      <TextInput
        accessibilityLabel="Intention pour le swarm"
        editable={!busy}
        multiline
        onChangeText={onChangeInput}
        placeholder="Qu’est-ce qu’on fait?"
        placeholderTextColor={COLORS.subtle}
        style={{
          backgroundColor: COLORS.panel,
          borderColor: COLORS.border,
          borderRadius: 16,
          borderWidth: 1,
          color: COLORS.text,
          minHeight: 104,
          padding: 14,
          textAlignVertical: "top",
        }}
        testID="chat-input"
        value={input}
      />
      <ActionButton
        busy={busy}
        disabled={!input.trim() || !authenticated}
        label="Envoyer"
        onPress={onSubmit}
        testID="send-button"
        variant="accent"
      />
      <Text
        accessibilityLiveRegion="polite"
        selectable
        style={{ color: COLORS.muted, lineHeight: 19 }}
      >
        {notice}
      </Text>
    </>
  );
}

function LastTaskLink({ lastTask, onOpen }: { lastTask: Task | null; onOpen: (id: string) => void }) {
  if (!lastTask) return null;
  return <ActionButton label="Voir la tâche et ses preuves" onPress={() => onOpen(lastTask.id)} />;
}

export default function ChatScreen() {
  const router = useRouter();
  const launchParameters = useLocalSearchParams<{ draft?: string; intentMode?: string }>();
  const chat = useChatController();
  const setChatInput = chat.setInput;
  const setInteractionMode = chat.setInteractionMode;
  const live = useLiveSync();
  const pending = chat.bootstrap?.counts.approvals_pending ?? 0;
  const handledDraft = useRef<string | null>(null);

  useEffect(() => {
    const draft = Array.isArray(launchParameters.draft)
      ? launchParameters.draft[0]
      : launchParameters.draft;
    if (!draft || handledDraft.current === draft) return;
    handledDraft.current = draft;
    setChatInput(draft.slice(0, 8_000));
    if (launchParameters.intentMode === "task") setInteractionMode("task");
  }, [launchParameters.draft, launchParameters.intentMode, setChatInput, setInteractionMode]);

  return (
    <ScreenShell
      title="monGARS"
      subtitle="Console locale du swarm"
      testID="chat-screen"
      onRefresh={() => void chat.refreshStatus()}
      refreshing={chat.refreshing}
    >
      <ConnectionPanel
        bootstrap={chat.bootstrap}
        liveState={live.state}
        pending={pending}
        onOpenSettings={() => router.push("/settings")}
        onOpenApprovals={() => router.push("/approvals")}
      />
      <ErrorBanner message={chat.error} />
      <Conversation messages={chat.messages} onSelectSuggestion={chat.setInput} />
      <IntentComposer
        authenticated={Boolean(chat.bootstrap)}
        busy={chat.busy}
        input={chat.input}
        interactionMode={chat.interactionMode}
        notice={chat.notice}
        onChangeInput={chat.setInput}
        onChangeMode={chat.setInteractionMode}
        onSubmit={() => void chat.submit()}
      />
      <LastTaskLink
        lastTask={chat.lastTask}
        onOpen={(id) => router.push({ pathname: "/task/[id]", params: { id } })}
      />
    </ScreenShell>
  );
}
