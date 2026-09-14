import { useCallback, useEffect, useRef, useState } from "react";
import { Text, View } from "react-native";

import { KeyboardInputGroup, KeyboardTextInput } from "@/components/screen-shell";
import { ActionButton, Card, COLORS, ErrorBanner, SectionTitle } from "@/components/swarm-ui";
import { ApiError, getGoalConversation, type GoalConversationSession, type GoalDetail, type GoalReplyAttempt } from "@/lib/api/client";
import { subscribeConnectionChanges } from "@/lib/connection-events";

type Props = {
  goal: GoalDetail["goal"];
  disabled: boolean;
  onOpenGoal: (goalId: string) => void;
  onOpenLocalPlan?: (goalId: string) => void;
  onUpdated: () => Promise<void>;
};

export function GoalConversation({ goal, disabled, onOpenGoal, onOpenLocalPlan, onUpdated }: Props) {
  const [session, setSession] = useState<GoalConversationSession | null>(null);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [pending, setPending] = useState<{ attempt: GoalReplyAttempt; mode: "automatic" | "iphone_local" } | null>(null);
  const busy = useRef(false);
  const deferredRefresh = useRef(false);
  const epoch = useRef(0);
  const mounted = useRef(true);
  const authorityEpoch = useRef(0);
  useEffect(() => {
    mounted.current = true;
    const unsubscribe = subscribeConnectionChanges(() => {
      authorityEpoch.current += 1;
      epoch.current += 1;
      deferredRefresh.current = false;
      setSession(null); setInput(""); setPending(null); setLoading(false);
      setNotice("Le jumelage a changé. Actualisez la conversation de cette connexion.");
    });
    return () => { mounted.current = false; unsubscribe(); };
  }, []);

  const reload = useCallback(async (clearError = true) => {
    deferredRefresh.current = false;
    const current = ++epoch.current;
    setLoading(true);
    try {
      const next = await getGoalConversation(goal.id);
      if (current === epoch.current) {
        setSession(next);
        if (clearError) setError(null);
      }
    } catch (cause) {
      if (current === epoch.current) {
        setSession(null);
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    } finally {
      if (current === epoch.current) setLoading(false);
    }
  }, [goal.id]);

  useEffect(() => {
    if (!disabled) {
      if (busy.current) deferredRefresh.current = true;
      else void reload();
    }
    return () => { epoch.current += 1; };
  }, [disabled, goal.updated_at, reload]);

  useEffect(() => {
    // Parent refreshes can invalidate a reload while the reply is still sending.
    if (!disabled && !sending && deferredRefresh.current) void reload(false);
  }, [disabled, reload, sending]);

  const canPlanLocal = Boolean(onOpenLocalPlan) && Boolean(session?.conversation.project_id)
    && ["completed", "failed", "cancelled", "budget_exhausted"].includes(goal.status)
    && session?.conversation.active_goal_id === goal.id;
  const send = async (requestedMode: "automatic" | "iphone_local" = "automatic") => {
    if (disabled || loading || busy.current || !session || (!pending && !input.trim())) return;
    if (!pending && requestedMode === "iphone_local" && !canPlanLocal) return;
    busy.current = true;
    setSending(true);
    setError(null);
    let attempt = pending?.attempt;
    const mode = pending?.mode ?? requestedMode;
    const authority = authorityEpoch.current;
    try {
      attempt = attempt ?? (mode === "iphone_local" ? session.prepareReply(input, { planningMode: "iphone_local" }) : session.prepareReply(input));
      setPending({ attempt, mode });
      const result = await attempt.send();
      if (!mounted.current || authority !== authorityEpoch.current) return;
      setInput("");
      setPending(null);
      setNotice(mode === "iphone_local" ? "La suite du projet attend le plan initial sur l’iPhone." : result.goal.id === goal.id ? "Message enregistré dans le projet." : "Un nouveau travail lié au même projet a été créé.");
      if (mode === "iphone_local") onOpenLocalPlan?.(result.goal.id);
      else if (result.goal.id !== goal.id) onOpenGoal(result.goal.id);
      else {
        await reload();
        await onUpdated();
      }
    } catch (cause) {
      if (!mounted.current || authority !== authorityEpoch.current) return;
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
        setPending(null);
        await reload();
        setError("L’état de la conversation a changé. Relisez la question actuelle avant d’envoyer votre réponse.");
      } else {
        setError(`${cause instanceof Error ? cause.message : String(cause)}${attempt ? " Réessayez le même envoi : son identifiant reste inchangé pour éviter un doublon." : ""}`);
      }
    } finally {
      busy.current = false;
      if (mounted.current) setSending(false);
    }
  };

  const conversation = session?.conversation;
  const question = conversation?.messages.find((message) => message.id === conversation.pending_question_id);
  const unavailable = disabled || loading || sending || !session;
  const tooLong = Array.from(input.trim()).length > 4_000;
  return (
    <>
      <SectionTitle title="Conversation du projet" />
      <Card>
        <Text style={{ color: COLORS.muted }}>Les échanges restent liés au projet, y compris lorsqu’un nouveau travail prend la suite.</Text>
        <ActionButton label="Actualiser la conversation" disabled={disabled || sending || loading} busy={loading} onPress={() => void reload()} />
        <ErrorBanner message={error} />
        {conversation?.active_goal_id && conversation.active_goal_id !== goal.id ? (
          <ActionButton label="Ouvrir le travail le plus récent" onPress={() => onOpenGoal(conversation.active_goal_id)} disabled={disabled || sending} />
        ) : null}
        {conversation?.messages.map((message) => (
          <View key={message.id} style={{ gap: 4, borderLeftWidth: 2, borderLeftColor: message.role === "user" ? COLORS.accent : COLORS.border, paddingLeft: 10 }}>
            <Text style={{ color: COLORS.subtle, fontSize: 12 }}>{message.role === "user" ? "Vous" : "Assistant"}</Text>
            <Text selectable style={{ color: COLORS.text, lineHeight: 21 }}>{message.content}</Text>
          </View>
        ))}
        {question ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.warning }}>Une précision est demandée. Votre réponse sera liée à cette question et permettra de poursuivre le travail sur le projet. L’application des fichiers dans votre espace de travail nécessitera une approbation distincte.</Text> : null}
        {notice ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.accent }}>{notice}</Text> : null}
        <KeyboardInputGroup testID="goal-conversation-composer">
          <KeyboardTextInput
            accessibilityLabel={question ? "Réponse à la question du projet" : "Message pour le projet"}
            placeholder={question ? "Votre réponse…" : "Précisez le besoin, demandez un changement ou poursuivez le projet…"}
            placeholderTextColor={COLORS.subtle}
            value={input}
            onChangeText={setInput}
            multiline
            editable={!disabled && !sending && !pending}
            style={{ minHeight: 104, maxHeight: 200, padding: 12, color: COLORS.text, backgroundColor: COLORS.background, borderRadius: 10, textAlignVertical: "top" }}
          />
          <Text style={{ color: tooLong ? COLORS.danger : COLORS.subtle }}>{Array.from(input.trim()).length}/4000 caractères</Text>
          <ActionButton label={pending ? "Réessayer le même envoi" : question ? "Répondre à la question" : "Envoyer au projet"} disabled={unavailable || (!pending && (!input.trim() || tooLong))} busy={sending} onPress={() => void send()} variant="accent" />
          {canPlanLocal && !pending ? <ActionButton label="Planifier la suite sur l’iPhone" disabled={unavailable || !input.trim() || tooLong} onPress={() => void send("iphone_local")} /> : null}
          {canPlanLocal ? <Text style={{ color: COLORS.subtle }}>La suite locale conserve le projet et attendra ton plan iPhone avant de lancer les agents.</Text> : null}
        </KeyboardInputGroup>
        {disabled && !session ? <Text style={{ color: COLORS.subtle }}>Connectez-vous pour lire la conversation et envoyer un message.</Text> : null}
      </Card>
    </>
  );
}
