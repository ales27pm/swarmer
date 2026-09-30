import { type Dispatch, useCallback, useEffect, useReducer, useRef, useState } from "react";
import { AppState, Pressable, Text, View, useWindowDimensions } from "react-native";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";

import { AssistantSuggestions, AssistantWelcome, IntentComposer } from "@/components/assistant-start";
import { ConversationHistory, HistoryDrawer } from "@/components/conversation-history";
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
  createGoal,
  listMessages,
  sendChat,
  type Bootstrap,
  type Message,
  type Task,
} from "@/lib/application-api/server";
import { newGoalMessageId } from "@/lib/api/project";
import { useLiveRefresh, useLiveSync } from "@/lib/sync/live-sync-context";
import type { LiveSyncState } from "@/lib/sync/live-sync";
import { subscribeConnectionChanges } from "@/lib/connection-events";

type ChatState = {
  input: string;
  interactionMode: "chat" | "task";
  conversationId: string | undefined;
  messages: Message[];
  lastTask: Task | null;
  lastGoalId: string | null;
  bootstrap: Bootstrap | null;
  notice: string;
  error: string | null;
  busy: boolean;
  refreshing: boolean;
};

type ChatStatePatch = Partial<ChatState>;
type ChatDispatch = Dispatch<ChatStatePatch>;
type MessageReader = (id: string, isCurrent: () => boolean) => Promise<Message[] | undefined>;

const INITIAL_CHAT_STATE: ChatState = {
  input: "",
  interactionMode: "chat",
  conversationId: undefined,
  messages: [],
  lastTask: null,
  lastGoalId: null,
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
  readMessages: MessageReader,
  isCurrent: () => boolean = () => true,
) {
  if (!conversationId || !isCurrent()) return;
  try {
    const messages = await readMessages(conversationId, isCurrent);
    if (messages && isCurrent()) dispatch({ messages });
  } catch {
    // Keep the rendered conversation while preserving the primary error.
  }
}

async function submitChatIntent(
  state: ChatState,
  dispatch: ChatDispatch,
  refreshStatus: (updateError?: boolean) => Promise<void>,
  readMessages: MessageReader,
  accepted: () => void,
  isCurrent: () => boolean,
) {
  const content = state.input.trim();
  if (!content || state.busy || !state.bootstrap || !isCurrent()) return;

  dispatch({
    busy: true,
    error: null,
    notice: "L’assistant prépare une réponse…",
  });
  let activeConversation = state.conversationId;
  try {
    const chat = await sendChat(
      content,
      state.conversationId,
      "normal",
      false,
      isCurrent,
    );
    if (!isCurrent()) return;
    accepted();
    activeConversation = chat.conversation_id;
    dispatch({
      conversationId: chat.conversation_id,
      lastTask: chat.task ?? state.lastTask,
      input: "",
    });
    const messages = await readMessages(chat.conversation_id, isCurrent);
    if (!isCurrent()) return;
    if (messages) dispatch({ messages });
    if (chat.task) {
      dispatch({ notice: "Le serveur a lié une tâche à cette réponse. Aucun démarrage n’a été demandé depuis Discuter." });
    } else {
      dispatch({ notice: "Réponse conversationnelle reçue. Aucune tâche n’a été créée." });
    }
  } catch (cause) {
    if (isCurrent()) dispatch({ error: errorMessage(cause), notice: "La réponse n’a pas pu être confirmée. Aucun démarrage de tâche n’a été demandé." });
  } finally {
    if (isCurrent()) await refreshConversation(activeConversation, dispatch, readMessages, isCurrent);
    if (isCurrent()) await refreshStatus(false);
    if (isCurrent()) dispatch({ busy: false });
  }
}

function useChatController(onOpenGoal: (id: string) => void) {
  const [state, dispatch] = useReducer(mergeChatState, INITIAL_CHAT_STATE);
  const refreshEpoch = useRef(0);
  const connectionEpoch = useRef(0);
  const conversationEpoch = useRef(0);
  const messageReadEpoch = useRef(0);
  const drafts = useRef(new Map<string, string>());
  const goalAttempt = useRef<{ key: string; id: string } | null>(null);
  const [openingConversation, setOpeningConversation] = useState(false);
  const activeSubmission = useRef<{ input: string } | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  useAccessibilityAnnouncement(state.notice);
  const setInput = useCallback((input: string) => {
    if (input !== stateRef.current.input) goalAttempt.current = null;
    dispatch({ input });
  }, []);
  const readMessages = useCallback<MessageReader>(async (id, isCurrent) => {
    const read = ++messageReadEpoch.current;
    const applies = () => read === messageReadEpoch.current && isCurrent();
    const messages = await listMessages(id, applies);
    return applies() ? messages : undefined;
  }, []);

  useEffect(() => {
    const unsubscribe = subscribeConnectionChanges(() => {
      refreshEpoch.current += 1;
      connectionEpoch.current += 1;
      conversationEpoch.current += 1;
      messageReadEpoch.current += 1;
      drafts.current.clear();
      goalAttempt.current = null;
      setOpeningConversation(false);
      const pending = activeSubmission.current;
      activeSubmission.current = null;
      dispatch({
        bootstrap: null, conversationId: undefined, messages: [], lastTask: null, lastGoalId: null,
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
    if (openingConversation || activeSubmission.current || state.busy || !state.bootstrap || !state.input.trim()) return;
    const submission = { input: state.input };
    const connection = connectionEpoch.current;
    activeSubmission.current = submission;
    ++refreshEpoch.current;
    ++messageReadEpoch.current;
    dispatch({ refreshing: false });
    const isCurrent = () => connectionEpoch.current === connection && activeSubmission.current === submission;
    try {
      if (state.interactionMode === "task") {
        const objective = state.input.trim();
        if ([...objective].length > 4000) {
          dispatch({ error: "La demande de projet est limitée à 4 000 caractères. Raccourcis-la avant de l’envoyer ; ton brouillon est conservé." });
          return;
        }
        const key = JSON.stringify([state.conversationId, objective]);
        if (goalAttempt.current?.key !== key) goalAttempt.current = { key, id: newGoalMessageId() };
        dispatch({ busy: true, error: null, notice: "Création du projet authentifié…" });
        try {
          const detail = await createGoal({ objective, autonomy_profile: "autonomous",
            conversation_id: state.conversationId, client_request_id: goalAttempt.current.id }, isCurrent);
          if (!isCurrent()) return;
          goalAttempt.current = null;
          drafts.current.delete(state.conversationId ?? "new");
          dispatch({ input: "", lastGoalId: detail.goal.id, notice: "Projet créé. Vérifie la demande, puis démarre le projet depuis sa fiche." });
          onOpenGoal(detail.goal.id);
        } catch (cause) {
          if (isCurrent()) dispatch({ error: errorMessage(cause), notice: "La création n’a pas été confirmée. Ton brouillon est conservé ; une nouvelle tentative identique retrouvera le même projet si le serveur l’a reçu." });
        } finally {
          if (isCurrent()) await refreshStatus(false);
          if (isCurrent()) dispatch({ busy: false });
        }
        return;
      }
      await submitChatIntent(state, dispatch, refreshStatus, readMessages, () => drafts.current.delete(state.conversationId ?? "new"), isCurrent);
    } finally {
      if (activeSubmission.current === submission) activeSubmission.current = null;
    }
  };

  const selectConversation = async (conversationId?: string) => {
    if (activeSubmission.current || stateRef.current.busy) return;
    const previous = stateRef.current;
    if (conversationId === previous.conversationId && !previous.error) return;
    if (conversationId && !previous.bootstrap?.conversations.some((item) => item.id === conversationId)) return;
    drafts.current.set(previous.conversationId ?? "new", previous.input);
    goalAttempt.current = null;
    const version = ++conversationEpoch.current;
    const connection = connectionEpoch.current;
    ++refreshEpoch.current;
    ++messageReadEpoch.current;
    const isCurrent = () => version === conversationEpoch.current && connection === connectionEpoch.current;
    setOpeningConversation(Boolean(conversationId));
    dispatch({ conversationId, messages: [], lastTask: null, lastGoalId: null, error: null, notice: "", refreshing: false,
      interactionMode: "chat", input: drafts.current.get(conversationId ?? "new") ?? "" });
    if (!conversationId) return;
    try {
      const messages = await readMessages(conversationId, isCurrent);
      if (!isCurrent() || !messages) return;
      dispatch({ messages, lastTask: previous.bootstrap?.tasks.find((item) => item.conversation_id === conversationId) ?? null });
    } catch (cause) {
      if (isCurrent()) dispatch({ error: errorMessage(cause), notice: "La discussion n’a pas pu être chargée. Réessaie depuis l’historique." });
    } finally { if (isCurrent()) setOpeningConversation(false); }
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
    const conversation = conversationEpoch.current;
    const id = stateRef.current.conversationId;
    const isCurrent = () => requestEpoch === refreshEpoch.current && conversation === conversationEpoch.current;
    await refreshBootstrap(dispatch, false, isCurrent);
    if (!activeSubmission.current) await refreshConversation(id, dispatch, readMessages, isCurrent);
  });

  return {
    ...state,
    openingConversation,
    selectConversation,
    refreshStatus,
    setInteractionMode: (interactionMode: "chat" | "task") => {
      if (interactionMode !== stateRef.current.interactionMode) goalAttempt.current = null;
      dispatch({ interactionMode });
    },
    setInput,
    submit,
  };
}

function pendingAgreementLabel(pending: number): string {
  return `${pending} autorisation${pending > 1 ? "s" : ""} en attente`;
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
  const { width, fontScale } = useWindowDimensions();
  const wide = width >= 900 && fontScale < 1.6;
  const [historyOpen, setHistoryOpen] = useState(false);
  const launchParameters = useLocalSearchParams<{ draft?: string; intentMode?: string }>();
  const chat = useChatController((id) => router.push({ pathname: "/goal/[id]", params: { id } }));
  const setChatInput = chat.setInput;
  const setInteractionMode = chat.setInteractionMode;
  const live = useLiveSync();
  const pending = chat.bootstrap?.counts.approvals_pending ?? 0;
  const handledDraft = useRef<string | null>(null);
  const history = {
    conversations: chat.bootstrap?.conversations ?? [], selectedId: chat.conversationId,
    disabled: chat.busy, connected: Boolean(chat.bootstrap),
    onSelect: (id: string) => { setHistoryOpen(false); void chat.selectConversation(id); },
    onNew: () => { setHistoryOpen(false); void chat.selectConversation(); },
  };

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
      <View style={{ flexDirection: wide ? "row" : "column", gap: 24 }}>
      {wide ? <View style={{ width: 260, borderRightWidth: 1, borderColor: COLORS.border, paddingRight: 20 }}><ConversationHistory {...history} /></View> : null}
      <View style={{ flex: 1, minWidth: 0, gap: 16 }}>
      {!wide ? <View style={{ flexDirection: "row", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        {!wide ? <Pressable accessibilityRole="button" onPress={() => setHistoryOpen(true)} testID="open-chat-history"
          style={{ minHeight: 44, justifyContent: "center" }}>
          <Text style={{ color: COLORS.accent, fontSize: 14, fontWeight: "600" }}>Historique</Text>
        </Pressable> : null}
        <Pressable accessibilityRole="button" onPress={history.onNew} disabled={chat.busy || chat.openingConversation}
          accessibilityState={{ disabled: chat.busy || chat.openingConversation }} testID="new-chat-button"
          style={{ minHeight: 44, justifyContent: "center", opacity: chat.busy || chat.openingConversation ? 0.5 : 1 }}>
          <Text style={{ color: COLORS.accent, fontSize: 14, fontWeight: "600" }}>Nouvelle discussion</Text>
        </Pressable>
      </View> : null}
      <ConnectionPanel
        bootstrap={chat.bootstrap}
        liveState={live.state}
        pending={pending}
        onOpenSettings={() => router.push("/settings")}
        onOpenApprovals={() => router.push("/approvals")}
      />
      <ErrorBanner message={chat.error} />
      {chat.openingConversation ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.muted }}>Chargement de la discussion…</Text> : null}
      {chat.messages.length ? <MessageList messages={chat.messages} /> : <AssistantWelcome />}
      <IntentComposer
        authenticated={Boolean(chat.bootstrap)}
        busy={chat.busy || chat.openingConversation}
        input={chat.input}
        interactionMode={chat.interactionMode}
        notice={chat.notice}
        onChangeInput={chat.setInput}
        onChangeMode={chat.setInteractionMode}
        onOpenSettings={() => router.push("/settings")}
        onSubmit={() => void chat.submit()}
      />
      <Pressable accessibilityRole="button" onPress={() => router.push({ pathname: "/swarm", params: { create: "1" } })}
        style={{ minHeight: 48, justifyContent: "center", borderBottomColor: COLORS.border, borderBottomWidth: 0.5 }} testID="chat-new-project">
        <Text style={{ color: COLORS.accent, fontSize: 15, fontWeight: "600" }}>Nouveau projet</Text>
      </Pressable>
      {!chat.messages.length ? <AssistantSuggestions onSelect={chat.setInput} disabled={chat.busy} /> : null}
      <LastTaskLink
        lastTask={chat.lastTask}
        onOpen={(id) => router.push({ pathname: "/task/[id]", params: { id } })}
      />
      {chat.lastGoalId ? <ActionButton label="Voir le projet et ses résultats" onPress={() => router.push({ pathname: "/goal/[id]", params: { id: chat.lastGoalId! } })} /> : null}
      </View>
      </View>
      {!wide ? <HistoryDrawer {...history} visible={historyOpen} onClose={() => setHistoryOpen(false)} /> : null}
    </ScreenShell>
  );
}
