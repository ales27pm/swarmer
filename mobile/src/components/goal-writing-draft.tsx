import { useEffect, useRef, useState } from "react";
import { ScrollView, Text, View } from "react-native";

import { ActionButton, COLORS, ErrorBanner } from "@/components/swarm-ui";
import { getGoalWritingDraft, type GoalWritingDraft as WritingDraft } from "@/lib/application-api/server";
import { subscribeConnectionChanges } from "@/lib/connection-events";

type Props = { goalId: string; nodeId: string; workerJobId: string; disabled: boolean };

export function GoalWritingDraft({ goalId, nodeId, workerJobId, disabled }: Props) {
  const [draft, setDraft] = useState<WritingDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const epoch = useRef(0);
  const busyRef = useRef(false);
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;

  useEffect(() => {
    epoch.current += 1;
    busyRef.current = false;
    setBusy(false); setDraft(null); setError(null);
    const unsubscribe = subscribeConnectionChanges(() => {
      epoch.current += 1;
      busyRef.current = false;
      setBusy(false); setDraft(null);
      setError("Le jumelage a changé. Rechargez le document depuis cette connexion.");
    });
    return () => { epoch.current += 1; unsubscribe(); };
  }, [goalId, nodeId, workerJobId]);

  const load = async () => {
    if (disabledRef.current || busyRef.current) return;
    const current = ++epoch.current;
    const shouldAccept = () => current === epoch.current && !disabledRef.current;
    busyRef.current = true;
    setBusy(true); setError(null); setDraft(null);
    try {
      const result = await getGoalWritingDraft(goalId, nodeId, workerJobId, shouldAccept);
      if (shouldAccept()) setDraft(result);
    } catch {
      if (shouldAccept()) setError("Le document n’a pas pu être chargé. Actualisez les preuves, puis réessayez.");
    } finally {
      if (current === epoch.current) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };

  return (
    <View style={{ gap: 10 }}>
      <ActionButton
        label={draft ? "Actualiser le document" : error ? "Réessayer le document" : "Lire le document complet"}
        busy={busy}
        disabled={disabled || busy}
        onPress={() => void load()}
      />
      <ErrorBanner message={error} />
      {draft ? (
        <>
          <Text accessibilityRole="header" style={{ color: COLORS.text, fontWeight: "800" }}>Document rédigé</Text>
          <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
            Ce document est une proposition à relire. Les actions décrites ne sont pas exécutées par cette lecture.
          </Text>
          <Text selectable style={{ color: COLORS.muted }}>{draft.summary}</Text>
          <ScrollView nestedScrollEnabled style={{ maxHeight: 420, backgroundColor: COLORS.background, borderRadius: 10 }} contentContainerStyle={{ padding: 12 }}>
            <Text selectable style={{ color: COLORS.text, lineHeight: 23 }}>{draft.text}</Text>
          </ScrollView>
        </>
      ) : null}
    </View>
  );
}
