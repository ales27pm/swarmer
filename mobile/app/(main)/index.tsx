import { type Dispatch, useCallback, useEffect, useReducer, useRef } from "react";
import { AppState, Pressable, Text, View } from "react-native";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";

import { AssistantSuggestions, AssistantWelcome, IntentComposer } from "@/components/assistant-start";
import { ScreenShell } from "@/components/screen-shell";
import {
  ActionButton,
  COLORS,
  ErrorBanner,
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
} from "@/lib/application-api/server";
import { useLiveRefresh, useLiveSync } from "@/lib/sync/live-sync-context";
import type { LiveSyncState } from "@/lib/sync/live-sync";
import { subscribeConnectionChanges } from "@/lib/connection-events";

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
  notice: "",
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

async function refreshConversation(
  conversationId: string | undefined,
  dispatch: ChatDispatch,
  isCurrent: () => boolean = () => true,
) {
  if (!conversationId || !isCurrent()) return;
  try {
    const messages = await listMessages(conversationId, isCurrent);
    if (isCurrent()) dispatch({ messages });
  } catch {
    // Keep the rendered conversation while preserving the primary error.
  }
}

async function submitChatIntent(
  state: ChatState,
  dispatch: ChatDispatch,
  refreshStatus: (updateError?: boolean) => Promise<void>,
  isCurrent: () => boolean,
) {
  const content = state.input.trim();
  if (!content || state.busy || !state.bootstrap || !isCurrent()) return;

  dispatch({
    busy: true,
    error: null,
    notice: state.interactionMode === "task"
      ? "Création de la tâche authentifiée…"
      : "L’assistant prépare une réponse…",
  });
  let activeConversation = state.conversationId;
  let createdTask: Task | null = null;
  try {
    const chat = await sendChat(
      content,
      state.conversationId,
      "normal",
      state.interactionMode === "task",
      isCurrent,
    );
    if (!isCurrent()) return;
    createdTask = chat.task;
    activeConversation = chat.conversation_id;
    dispatch({
      conversationId: chat.conversation_id,
      lastTask: chat.task ?? state.lastTask,
      input: "",
    });
    const messages = await listMessages(chat.conversation_id, isCurrent);
    if (!isCurrent()) return;
    dispatch({ messages });
    if (chat.task) {
      dispatch({ notice: "L’équipe prépare un plan…" });
      const result = await planTask(chat.task.id, isCurrent);
      if (!isCurrent()) return;
      dispatch({ notice: planningStatus(result) });
      dispatch({ lastTask: taskAfterPlanning(result, chat.task) });
    } else {
      dispatch({ notice: "Réponse conversationnelle reçue. Aucune tâche n’a été créée." });
    }
  } catch (cause) {
    if (isCurrent()) dispatch({ error: errorMessage(cause), notice: failedAttemptNotice(createdTask) });
  } finally {
    if (isCurrent()) await refreshConversation(activeConversation, dispatch, isCurrent);
    if (isCurrent()) await refreshStatus(false);
    if (isCurrent()) dispatch({ busy: false });
  }
}

function useChatController() {
  const [state, dispatch] = useReducer(mergeChatState, INITIAL_CHAT_STATE);
  const refreshEpoch = useRef(0);
  const connectionEpoch = useRef(0);
  const activeSubmission = useRef<{ input: string } | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  useAccessibilityAnnouncement(state.notice);
  const setInput = useCallback((input: string) => dispatch({ input }), []);

  useEffect(() => {
    const unsubscribe = subscribeConnectionChanges(() => {
      refreshEpoch.current += 1;
      connectionEpoch.current += 1;
      const pending = activeSubmission.current;
      activeSubmission.current = null;
      dispatch({
        bootstrap: null, conversationId: undefined, messages: [], lastTask: null,
        input: stateRef.current.input || pending?.input || "",
        busy: false, refreshing: false, error: null,
        notice: pending
          ? "Le jumelage a changé. Une opération déjà envoyée peut se poursuivre sur l’ancien serveur ; elle n’est pas renvoyée automatiquement."
          : "Le jumelage a changé. Vérifie la connexion avant d’envoyer ton prochain message.",
      });
    });
    return () => {
      unsubscribe();
      refreshEpoch.current += 1;
      connectionEpoch.current += 1;
      activeSubmission.current = null;
    };
  }, []);

  const submit = async () => {
    if (activeSubmission.current || state.busy || !state.bootstrap || !state.input.trim()) return;
    const submission = { input: state.input };
    const connection = connectionEpoch.current;
    activeSubmission.current = submission;
    const isCurrent = () => connectionEpoch.current === connection && activeSubmission.current === submission;
    try {
      await submitChatIntent(state, dispatch, refreshStatus, isCurrent);
    } finally {
      if (activeSubmission.current === submission) activeSubmission.current = null;
    }
  };

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
  useLiveRefresh(async () => {
    const requestEpoch = ++refreshEpoch.current;
    const isCurrent = () => requestEpoch === refreshEpoch.current;
    await refreshBootstrap(dispatch, false, isCurrent);
    await refreshConversation(state.conversationId, dispatch, isCurrent);
  });

  return {
    ...state,
    refreshStatus,
    setInteractionMode: (interactionMode: "chat" | "task") => dispatch({ interactionMode }),
    setInput,
    submit,
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
    <View style={{ gap: 4 }}>
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
          {bootstrap ? "Serveur connecté" : "Connexion au serveur à vérifier"}
          {pending ? ` · ${pendingAgreementLabel(pending)}` : ""}
        </Text>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Ouvrir les réglages"
          style={{ minHeight: 44, justifyContent: "center", paddingLeft: 8 }}
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
                ? "Suivi interrompu · tire pour actualiser"
                : "Temps réel non établi"}
      </Text>

      {pending ? (
        <ActionButton
          label={`Voir ${pendingAgreementLabel(pending)}`}
          onPress={onOpenApprovals}
          testID="open-pending-approvals"
        />
      ) : null}
    </View>
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
      showTitle={false}
      title="monGARS"
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
      {chat.messages.length ? <MessageList messages={chat.messages} /> : <AssistantWelcome />}
      <IntentComposer
        authenticated={Boolean(chat.bootstrap)}
        busy={chat.busy}
        input={chat.input}
        interactionMode={chat.interactionMode}
        notice={chat.notice}
        onChangeInput={chat.setInput}
        onChangeMode={chat.setInteractionMode}
        onOpenSettings={() => router.push("/settings")}
        onSubmit={() => void chat.submit()}
      />
      {!chat.messages.length ? <AssistantSuggestions onSelect={chat.setInput} disabled={chat.busy} /> : null}
      <LastTaskLink
        lastTask={chat.lastTask}
        onOpen={(id) => router.push({ pathname: "/task/[id]", params: { id } })}
      />
    </ScreenShell>
  );
}
