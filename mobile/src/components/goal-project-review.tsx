import { useEffect, useRef, useState } from "react";
import { ScrollView, Text, View } from "react-native";

import { ActionButton, Card, COLORS, ErrorBanner, SectionTitle } from "@/components/swarm-ui";
import { ApiError, reviewGoalProject, type ProjectReview } from "@/lib/api/client";
import type { ProjectCheck, ProjectPreview } from "@/lib/api/project";
import { subscribeConnectionChanges } from "@/lib/connection-events";

const STATES: Record<ProjectPreview["state"], string> = {
  building: "Construction en cours", needs_user: "Votre réponse est attendue", ready: "Révision prête à relire",
  waiting_permission: "Autorisation d’écriture en attente", applied: "Révision écrite", failed: "Construction échouée",
};

function CheckReceipt({ check }: { check: ProjectCheck }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <View style={{ gap: 5 }}>
      <Text selectable style={{ color: check.status === "passed" ? COLORS.accent : COLORS.warning }}>
        {check.status === "passed" ? "Réussi" : check.status === "failed" ? "Échoué" : "Non exécuté"} · {check.command.join(" ")}
      </Text>
      <Text style={{ color: COLORS.subtle }}>Code de sortie : {check.exit_code ?? "indisponible"} · {check.duration_ms} ms</Text>
      {check.output ? <ActionButton label={expanded ? "Masquer le journal" : "Voir le journal de vérification"} onPress={() => setExpanded(!expanded)} /> : null}
      {expanded ? <Text selectable style={{ color: COLORS.muted, fontFamily: "Courier", fontSize: 12 }}>{check.output}</Text> : null}
    </View>
  );
}

export function GoalProjectReview({ goalId, disabled, onOpenTask }: { goalId: string; disabled: boolean; onOpenTask: (taskId: string) => void }) {
  const [review, setReview] = useState<ProjectReview | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [locked, setLocked] = useState(false);
  const [busy, setBusy] = useState<"load" | "apply" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);
  const epoch = useRef(0);
  useEffect(() => {
    const unsubscribe = subscribeConnectionChanges(() => {
      epoch.current += 1;
      busyRef.current = false;
      setBusy(null); setReview(null); setSelected(null); setReviewed(false); setLocked(false);
      setError("Le jumelage a changé. Rechargez la révision depuis cette connexion.");
    });
    return () => { epoch.current += 1; unsubscribe(); };
  }, []);

  const load = async () => {
    if (disabled || busyRef.current) return;
    const current = ++epoch.current;
    busyRef.current = true;
    setBusy("load"); setError(null); setReview(null); setReviewed(false); setSelected(null);
    try {
      const value = await reviewGoalProject(goalId);
      if (current === epoch.current) { setReview(value); setLocked(false); }
    } catch (cause) {
      if (current === epoch.current) setError(cause instanceof ApiError && cause.status === 404 ? "Aucune révision n’est encore disponible. Actualisez après la prochaine étape de construction." : cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (current === epoch.current) { busyRef.current = false; setBusy(null); }
    }
  };
  const apply = async () => {
    if (disabled || busyRef.current || locked || !reviewed || !review) return;
    const current = epoch.current;
    busyRef.current = true; setBusy("apply"); setLocked(true); setError(null);
    try {
      const result = await review.prepareApproval();
      if (current === epoch.current) onOpenTask(result.task_id);
    } catch (cause) {
      if (current === epoch.current) setError(`${cause instanceof Error ? cause.message : String(cause)} La demande peut avoir été créée. Actualisez la révision avant toute nouvelle tentative.`);
    } finally {
      if (current === epoch.current) { busyRef.current = false; setBusy(null); }
    }
  };
  const project = review?.project;
  const file = project?.files.find((file) => file.path === selected);
  const failed = project?.checks.some((check) => check.status === "failed");
  const canApply = project?.state === "ready" && !project.task_id && project.files.length > 0 && !failed && project.checks.some((check) => check.status === "passed");
  const unavailable = disabled || Boolean(busy);
  return (
    <>
      <SectionTitle title="Fichiers et vérifications du projet" />
      <Card>
        <ActionButton label={review || locked || error ? "Actualiser la révision du projet" : "Examiner le projet"} disabled={unavailable} busy={busy === "load"} onPress={() => void load()} />
        <ErrorBanner message={error} />
        {project ? (
          <>
            <Text style={{ color: COLORS.accent, fontWeight: "800" }}>{STATES[project.state]} · révision {project.revision}</Text>
            <Text selectable style={{ color: COLORS.subtle }}>Révision {project.revision_id} · SHA-256 {project.sha256}</Text>
            <Text selectable style={{ color: COLORS.text }}>{project.message}</Text>
            <Text style={{ color: COLORS.muted }}>Plan du projet</Text>
            {project.plan.map((step, index) => <Text key={index} selectable style={{ color: COLORS.text }}>{index + 1}. {step}</Text>)}
            <Text style={{ color: COLORS.muted }}>{project.files.length} fichiers · {project.runtime}</Text>
            {project.files.map((file) => <ActionButton key={file.path} label={`Lire ${file.path}`} disabled={unavailable} onPress={() => setSelected(file.path)} />)}
            {file ? (
              <View style={{ gap: 5 }}>
                <Text selectable style={{ color: COLORS.accent }}>{file.path}</Text>
                <ScrollView nestedScrollEnabled style={{ maxHeight: 360, backgroundColor: COLORS.background }} contentContainerStyle={{ padding: 10 }}>
                  <Text selectable style={{ color: COLORS.text, fontFamily: "Courier", fontSize: 12, lineHeight: 18 }}>{file.content || "(fichier vide)"}</Text>
                </ScrollView>
              </View>
            ) : null}
            <Text style={{ color: COLORS.muted }}>Résultats des vérifications isolées</Text>
            {project.checks.map((check, index) => <CheckReceipt key={`${project.revision_id}:${index}`} check={check} />)}
            {!project.checks.length ? <Text style={{ color: COLORS.warning }}>Aucune vérification exécutée n’est disponible.</Text> : null}
            {failed ? <Text style={{ color: COLORS.warning }}>Des vérifications ont échoué. Cette révision ne peut pas être présentée comme terminée.</Text> : null}
            <Text style={{ color: COLORS.muted }}>Instructions de lancement</Text>
            <Text selectable style={{ color: COLORS.text }}>{project.run_instructions || "Pas encore disponibles."}</Text>
            <Text style={{ color: COLORS.subtle }}>Les commandes et instructions ci-dessus ne sont pas lancées par cette interface. Une écriture approuvée conserve cette révision ; elle ne déploie pas l’application.</Text>
            {canApply ? (
              <>
                <ActionButton label={reviewed ? "Révision marquée comme relue" : "J’ai relu les fichiers et les vérifications"} disabled={unavailable || locked || !selected} onPress={() => setReviewed(!reviewed)} />
                <ActionButton label="Préparer l’autorisation du projet" variant="accent" disabled={unavailable || locked || !reviewed} busy={busy === "apply"} onPress={() => void apply()} />
              </>
            ) : null}
            {project.task_id ? <ActionButton label="Voir l’autorisation et les preuves du projet" disabled={unavailable} onPress={() => onOpenTask(project.task_id as string)} /> : null}
          </>
        ) : null}
      </Card>
    </>
  );
}
