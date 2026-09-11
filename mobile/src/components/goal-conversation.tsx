import { useCallback, useEffect, useRef, useState } from "react";
import { Text, TextInput, View } from "react-native";

import { ActionButton, Card, COLORS, ErrorBanner, SectionTitle } from "@/components/swarm-ui";
import { ApiError, getGoalConversation, type GoalConversationSession, type GoalDetail, type GoalReplyAttempt } from "@/lib/api/client";
import { subscribeConnectionChanges } from "@/lib/connection-events";

type Props = {
  goal: GoalDetail["goal"];
  disabled: boolean;
  onOpenGoal: (goalId: string) => void;
  onUpdated: () => Promise<void>;
};

export function GoalConversation({ goal, disabled, onOpenGoal, onUpdated }: Props) {
  const [session, setSession] = useState<GoalConversationSession | null>(null);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [pending, setPending] = useState<GoalReplyAttempt | null>(null);
  const busy = useRef(false);
  const epoch = useRef(0);
  const mounted = useRef(true);
  const authorityEpoch = useRef(0);
  useEffect(() => {
    mounted.current = true;
    const unsubscribe = subscribeConnectionChanges(() => {
      authorityEpoch.current += 1;
      epoch.current += 1;
      setSession(null); setInput(""); setPending(null); setLoading(false);
      setNotice("Le jumelage a changé. Actualisez la conversation de cette connexion.");
    });
    return () => { mounted.current = false; unsubscribe(); };
  }, []);

  const reload = useCallback(async () => {
    const current = ++epoch.current;
    setLoading(true);
    try {
      const next = await getGoalConversation(goal.id);
      if (current === epoch.current) {
        setSession(next);
        setError(null);
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
    if (!disabled && !busy.current) {
      void reload();
    }
    return () => { epoch.current += 1; };
  }, [disabled, goal.updated_at, reload]);

  const send = async () => {
    if (disabled || loading || busy.current || !session || (!pending && !input.trim())) return;
    busy.current = true;
    setSending(true);
    setError(null);
    let attempt = pending;
    const authority = authorityEpoch.current;
    try {
      attempt = attempt ?? session.prepareReply(input);
      setPending(attempt);
      const result = await attempt.send();
      if (!mounted.current || authority !== authorityEpoch.current) return;
      setInput("");
      setPending(null);
      setNotice(result.goal.id === goal.id ? "Message enregistré dans le projet." : "Un nouveau travail lié au même projet a été créé.");
      if (result.goal.id !== goal.id) onOpenGoal(result.goal.id);
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
        {question ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.warning }}>Une précision est demandée. Votre réponse sera liée à cette question ; elle n’autorise aucune écriture.</Text> : null}
        {notice ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.accent }}>{notice}</Text> : null}
        <TextInput
          accessibilityLabel={question ? "Réponse à la question du projet" : "Message pour le projet"}
          placeholder={question ? "Votre réponse…" : "Précisez le besoin, demandez un changement ou poursuivez le projet…"}
          placeholderTextColor={COLORS.subtle}
          value={input}
          onChangeText={setInput}
          multiline
          editable={!disabled && !sending && !pending}
          style={{ minHeight: 104, padding: 12, color: COLORS.text, backgroundColor: COLORS.background, borderRadius: 10, textAlignVertical: "top" }}
        />
        <Text style={{ color: tooLong ? COLORS.danger : COLORS.subtle }}>{Array.from(input.trim()).length}/4000 caractères</Text>
        <ActionButton label={pending ? "Réessayer le même envoi" : question ? "Répondre à la question" : "Envoyer au projet"} disabled={unavailable || (!pending && (!input.trim() || tooLong))} busy={sending} onPress={() => void send()} variant="accent" />
        {disabled && !session ? <Text style={{ color: COLORS.subtle }}>Connectez-vous pour lire la conversation et envoyer un message.</Text> : null}
      </Card>
    </>
  );
}
