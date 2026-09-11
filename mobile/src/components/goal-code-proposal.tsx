import { useEffect, useRef, useState } from "react";
import { ScrollView, Text, View } from "react-native";

import { ActionButton, COLORS, ErrorBanner } from "@/components/swarm-ui";
import { reviewGoalCodeProposal, type GoalCodeProposalReview as ProposalReviewSession } from "@/lib/api/client";

type Props = {
  goalId: string;
  nodeId: string;
  disabled: boolean;
  onOpenTask: (taskId: string) => void;
};

function messageFor(cause: unknown) {
  return cause instanceof Error ? cause.message : String(cause);
}

export function GoalCodeProposalReview({ goalId, nodeId, disabled, onOpenTask }: Props) {
  const [review, setReview] = useState<ProposalReviewSession | null>(null);
  const [busy, setBusy] = useState<"load" | "apply" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applyLocked, setApplyLocked] = useState(false);
  const busyRef = useRef(false);
  const epoch = useRef(0);

  useEffect(() => () => { epoch.current += 1; }, []);

  const load = async () => {
    if (disabled || busyRef.current) return;
    const current = ++epoch.current;
    busyRef.current = true;
    setBusy("load");
    setError(null);
    setReview(null);
    try {
      const next = await reviewGoalCodeProposal(goalId, nodeId);
      if (current !== epoch.current) return;
      setReview(next);
      setApplyLocked(false);
    } catch (cause) {
      if (current === epoch.current) setError(messageFor(cause));
    } finally {
      if (current === epoch.current) {
        busyRef.current = false;
        setBusy(null);
      }
    }
  };

  const prepare = async () => {
    if (disabled || busyRef.current || applyLocked || !review || review.proposal.status !== "proposal") return;
    const current = epoch.current;
    busyRef.current = true;
    setBusy("apply");
    setApplyLocked(true);
    setError(null);
    try {
      const result = await review.prepareApproval();
      if (current === epoch.current) onOpenTask(result.task_id);
    } catch (cause) {
      if (current === epoch.current) {
        setError(`${messageFor(cause)} La demande peut avoir été créée. Actualisez la proposition avant toute nouvelle tentative.`);
      }
    } finally {
      if (current === epoch.current) {
        busyRef.current = false;
        setBusy(null);
      }
    }
  };

  const proposal = review?.proposal;
  const unavailable = disabled || Boolean(busy);
  const loadLabel = review || applyLocked
    ? "Actualiser la proposition"
    : error ? "Réessayer le chargement" : "Examiner le code proposé";
  return (
    <View style={{ gap: 10 }}>
      <ActionButton
        label={loadLabel}
        busy={busy === "load"}
        disabled={unavailable}
        onPress={() => void load()}
      />
      <ErrorBanner message={error} />
      {proposal ? (
        <>
          <Text accessibilityRole="header" style={{ color: COLORS.warning, fontWeight: "800" }}>
            Code généré — non vérifié
          </Text>
          <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
            Cette revue n’exécute pas le code. L’écriture du fichier exige ensuite votre autorisation unique dans la tâche.
          </Text>
          <Text selectable style={{ color: COLORS.text }}>Fichier proposé : {proposal.path}</Text>
          <Text selectable style={{ color: COLORS.subtle, fontSize: 12 }}>
            SHA-256 du contenu proposé : {proposal.sha256}
          </Text>
          <Text selectable style={{ color: COLORS.muted }}>Résumé généré : {proposal.summary}</Text>
          <ScrollView
            nestedScrollEnabled
            style={{ maxHeight: 360, backgroundColor: COLORS.background, borderRadius: 10 }}
            contentContainerStyle={{ padding: 10 }}
          >
            <Text selectable style={{ color: COLORS.text, fontFamily: "Courier", fontSize: 12, lineHeight: 18 }}>
              {proposal.content}
            </Text>
          </ScrollView>
          {proposal.status === "proposal" && proposal.task_id === null ? (
            <ActionButton
              label="Préparer l’autorisation d’écriture"
              busy={busy === "apply"}
              disabled={unavailable || applyLocked}
              onPress={() => void prepare()}
              variant="accent"
            />
          ) : null}
          {proposal.task_id ? (
            <ActionButton
              label={proposal.status === "waiting_permission" ? "Voir la demande d’autorisation" : "Voir la tâche et ses preuves"}
              disabled={unavailable}
              onPress={() => onOpenTask(proposal.task_id as string)}
            />
          ) : null}
          {proposal.status === "applied" ? (
            <Text style={{ color: COLORS.accent }}>Le serveur indique que le fichier a été écrit. L’exécution du programme reste à vérifier.</Text>
          ) : null}
          {proposal.status === "failed" ? (
            <Text style={{ color: COLORS.warning }}>L’application du fichier a échoué. Consultez les preuves de la tâche.</Text>
          ) : null}
        </>
      ) : null}
    </View>
  );
}
