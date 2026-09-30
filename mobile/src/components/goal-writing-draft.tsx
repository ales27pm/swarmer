import { useCallback, useEffect, useRef, useState } from "react";
import { ScrollView, Share, Text, View } from "react-native";

import { ActionButton, COLORS, ErrorBanner } from "@/components/swarm-ui";
import { getGoalWritingDraft, type GoalWritingDraft as WritingDraft } from "@/lib/application-api/server";
import { subscribeConnectionChanges } from "@/lib/connection-events";

type Props = { goalId: string; nodeId: string; workerJobId: string; disabled: boolean; autoLoad?: boolean };

export function GoalWritingDraft({ goalId, nodeId, workerJobId, disabled, autoLoad = false }: Props) {
  const [draft, setDraft] = useState<WritingDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shareError, setShareError] = useState<string | null>(null);
  const epoch = useRef(0);
  const busyRef = useRef(false);
  const attemptedAutomaticLoad = useRef(false);
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;

  useEffect(() => {
    epoch.current += 1;
    attemptedAutomaticLoad.current = false;
    busyRef.current = false;
    setBusy(false); setDraft(null); setError(null); setShareError(null);
    const unsubscribe = subscribeConnectionChanges(() => {
      epoch.current += 1;
      // A new pairing must never silently fetch the old project's document.
      attemptedAutomaticLoad.current = true;
      busyRef.current = false;
      setBusy(false); setDraft(null);
      setShareError(null);
      setError("Le jumelage a changé. Rechargez le document depuis cette connexion.");
    });
    return () => { epoch.current += 1; unsubscribe(); };
  }, [goalId, nodeId, workerJobId]);

  const load = useCallback(async () => {
    if (disabledRef.current || busyRef.current) return;
    const current = ++epoch.current;
    const shouldAccept = () => current === epoch.current && !disabledRef.current;
    busyRef.current = true;
    setBusy(true); setError(null);
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
  }, [goalId, nodeId, workerJobId]);

  useEffect(() => {
    if (!autoLoad || disabled || attemptedAutomaticLoad.current) return;
    attemptedAutomaticLoad.current = true;
    void load();
  }, [autoLoad, disabled, load]);

  const share = async () => {
    if (!draft) return;
    const current = epoch.current;
    setShareError(null);
    try { await Share.share({ message: draft.text }); }
    catch { if (current === epoch.current) setShareError("Le partage n’a pas pu être ouvert. Le texte reste sélectionnable pour le copier."); }
  };

  return (
    <View style={{ gap: 10 }}>
      <ActionButton
        label={error ? "Réessayer le document" : draft ? "Actualiser le document" : "Lire le document complet"}
        busy={busy}
        disabled={disabled || busy}
        onPress={() => void load()}
      />
      <ErrorBanner message={error} />
      <ErrorBanner message={shareError} />
      {draft && (disabled || error) ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.warning }}>
        Dernier document chargé, non actualisé. Son contenu peut être périmé.
      </Text> : null}
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
          <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Maintiens le texte pour le sélectionner et le copier, y compris ses liens sources.</Text>
          <ActionButton label="Partager le document" onPress={() => void share()} />
        </>
      ) : null}
    </View>
  );
}
