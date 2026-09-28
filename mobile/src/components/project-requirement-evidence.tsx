import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Pressable, Text, TextInput, View } from "react-native";

import { getProjectEvidence, putProjectEvidence } from "@/lib/application-api/server";
import type { EvidenceCriterion, EvidenceMapping, EvidenceStaleReason, ProjectEvidenceView, ProjectEvidenceWrite } from "@/lib/api/project-evidence";
import { subscribeConnectionChanges } from "@/lib/connection-events";
import { useLiveRefresh, useLiveSync } from "@/lib/sync/live-sync-context";
import { ActionButton, Card, COLORS, ErrorBanner } from "./swarm-ui";

const STALE_REASONS: Record<EvidenceStaleReason, string> = {
  goal_changed: "Contexte modifié.", revision_changed: "Une autre révision est maintenant courante.",
  producer_changed: "L’étape ou l’agent producteur a changé.", evidence_changed: "Les fichiers ou les résultats de contrôle ont changé.",
};
let requestSequence = 0;
const requestId = () => `evidence:${Date.now()}:${++requestSequence}:${Math.random().toString(36).slice(2)}`;

export function useProjectEvidence(goalId: string, enabled: boolean, refreshKey: string) {
  const [view, setView] = useState<ProjectEvidenceView | null>(null);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(true);
  const [connectionGeneration, setConnectionGeneration] = useState(0);
  const epoch = useRef(0);
  const active = useRef(AppState.currentState === "active");
  const blockedConnection = useRef(false);
  const mutation = useRef(false);
  const pending = useRef<{ fingerprint: string; requestId: string } | null>(null);
  const { state: liveState } = useLiveSync();
  const allowed = useRef(false);
  allowed.current = enabled && liveState === "connected";
  const latest = useRef({ view, stale });
  latest.current = { view, stale };

  const refresh = useCallback(async (explicit = false) => {
    if (!allowed.current || !active.current || mutation.current || blockedConnection.current && !explicit) return;
    if (explicit) blockedConnection.current = false;
    const current = ++epoch.current;
    const accepts = () => epoch.current === current && allowed.current && active.current;
    setBusy(true); setStale(true); setError(null);
    try {
      const value = await getProjectEvidence(goalId, accepts);
      if (accepts()) { setView(value); setStale(false); }
    } catch {
      if (accepts()) setError("Les liens de preuve n’ont pas pu être actualisés. Le relevé conservé reste non confirmé.");
    } finally { if (current === epoch.current) setBusy(false); }
  }, [goalId]);

  const save = useCallback(async (criterionIndex: number, body: Omit<ProjectEvidenceWrite, "request_id">) => {
    if (!allowed.current || !active.current || blockedConnection.current || mutation.current || latest.current.stale) return null;
    const currentView = latest.current.view;
    const revision = currentView?.current_revision;
    const criterion = currentView?.criteria.find((item) => item.index === criterionIndex);
    if (!currentView || !revision || !criterion || revision.goal_run_id !== goalId
      || body.context_sha256 !== currentView.context_sha256 || body.conversation_revision !== currentView.conversation_revision
      || body.criterion_sha256 !== criterion.sha256 || body.expected_version !== (criterion.mapping?.version ?? 0)
      || body.project_id !== revision.project_id || body.node_id !== revision.node_id
      || body.revision_id !== revision.id || body.revision_sha256 !== revision.sha256) return null;
    const fingerprint = JSON.stringify([goalId, criterionIndex, body]);
    if (pending.current?.fingerprint !== fingerprint) pending.current = { fingerprint, requestId: requestId() };
    mutation.current = true;
    const current = ++epoch.current;
    const accepts = () => epoch.current === current && allowed.current && active.current && !blockedConnection.current;
    setSaving(true); setBusy(false); setStale(true); setError(null);
    try {
      const value = await putProjectEvidence(goalId, criterionIndex, { ...body, request_id: pending.current.requestId }, accepts);
      if (accepts()) { pending.current = null; setView(value); setStale(false); return value; }
    } catch {
      if (accepts()) setError("Enregistrement non confirmé. Actualisez les preuves avant une nouvelle action; votre saisie est conservée.");
    } finally { mutation.current = false; setSaving(false); }
    return null;
  }, [goalId]);

  useLiveRefresh(() => refresh());
  useEffect(() => {
    if (enabled && liveState === "connected") void refresh();
    else { epoch.current += 1; setStale(true); setBusy(false); }
    return () => { epoch.current += 1; };
  }, [enabled, liveState, refreshKey, refresh]);
  useEffect(() => subscribeConnectionChanges(() => {
    epoch.current += 1; blockedConnection.current = true;
    setConnectionGeneration((value) => value + 1); setStale(true); setBusy(false);
    setError("Le jumelage a changé. Actualisez les preuves; votre saisie reste conservée sans être envoyée.");
  }), []);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      active.current = state === "active";
      if (active.current) void refresh();
      else { epoch.current += 1; setStale(true); setBusy(false); }
    });
    return () => subscription.remove();
  }, [refresh]);
  return { view, busy, saving, error, stale: stale || !enabled || liveState !== "connected", connectionGeneration, refresh, save };
}
export type ProjectEvidenceState = ReturnType<typeof useProjectEvidence>;

function Checkbox({ label, checked, disabled, onPress }: { label: string; checked: boolean; disabled: boolean; onPress: () => void }) {
  return <Pressable accessibilityRole="checkbox" accessibilityLabel={label} accessibilityState={{ checked, disabled }}
    disabled={disabled} onPress={onPress} style={{ minHeight: 44, flexDirection: "row", alignItems: "center", gap: 10, paddingVertical: 8, opacity: disabled ? 0.5 : 1 }}>
    <Text accessible={false} style={{ color: checked ? COLORS.accent : COLORS.muted, fontSize: 21 }}>{checked ? "☑" : "☐"}</Text>
    <Text style={{ flex: 1, color: COLORS.text, lineHeight: 20 }}>{label}</Text>
  </Pressable>;
}

function SavedMapping({ mapping }: { mapping: EvidenceMapping }) {
  return <View style={{ gap: 5 }}>
    <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Lien enregistré · version {mapping.version} · {new Date(mapping.recorded_at).toLocaleString("fr-CA")}</Text>
    <Text selectable style={{ color: COLORS.muted, fontSize: 13 }}>Exigence liée : {mapping.criterion_text}</Text>
    <Text selectable style={{ color: COLORS.muted, fontSize: 12 }}>Révision liée : {mapping.revision_id}</Text>
    <Text selectable style={{ color: COLORS.muted, fontSize: 12 }}>Étape liée : {mapping.node_id}</Text>
    {mapping.files.map((file) => <Text key={file.id} selectable style={{ color: COLORS.muted }}>Fichier : {file.path}</Text>)}
    {mapping.checks.map((check) => <Text key={check.id} selectable style={{ color: COLORS.muted }}>Contrôle {check.index + 1} : {check.command?.join(" ") ?? "commande non enregistrée"} · {check.status === "passed" ? "réussi" : check.status === "failed" ? "échoué" : "ignoré"}</Text>)}
    {mapping.public_explanation ? <Text selectable style={{ color: COLORS.text, lineHeight: 20 }}>{mapping.public_explanation}</Text> : null}
    {mapping.stale_reasons.map((reason) => <Text key={reason} style={{ color: COLORS.warning }}>{STALE_REASONS[reason]}</Text>)}
  </View>;
}

function snapshotKey(view: ProjectEvidenceView, criterion: EvidenceCriterion, generation: number): string {
  return JSON.stringify([generation, view.goal_run_id, view.project_id, view.context_sha256, view.conversation_revision, criterion.sha256,
    criterion.mapping?.version ?? 0, view.current_revision]);
}
function draftFor(view: ProjectEvidenceView, criterion: EvidenceCriterion, generation: number) {
  const mapping = criterion.status !== "stale" ? criterion.mapping : null;
  return { snapshot: snapshotKey(view, criterion, generation), fileIds: mapping?.file_ids ?? [], checkIds: mapping?.check_ids ?? [],
    reviewed: false, explanation: criterion.mapping?.public_explanation ?? "" };
}

function CriterionEvidence({ criterion, view, state, disabled }: {
  criterion: EvidenceCriterion; view: ProjectEvidenceView; state: ProjectEvidenceState; disabled: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [draft, setDraft] = useState(() => draftFor(view, criterion, state.connectionGeneration));
  const revision = view.current_revision;
  const currentDraft = draft.snapshot === snapshotKey(view, criterion, state.connectionGeneration);
  const canMap = revision?.goal_run_id === view.goal_run_id && revision.project_id === view.project_id;
  const readOnly = disabled || state.stale || state.busy || state.saving || !canMap;
  const selectedChecks = revision?.checks.filter((check) => draft.checkIds.includes(check.id)) ?? [];
  const canReviewSelection = selectedChecks.length > 0 && selectedChecks.length === draft.checkIds.length
    && selectedChecks.every((check) => check.status === "passed" && check.exit_code === 0);
  const status = state.stale ? "État non confirmé" : criterion.status === "stale" ? "Preuves à revoir"
    : criterion.status === "reviewed" ? "Revu explicitement" : criterion.status === "linked" ? "Preuves liées" : "Aucune preuve liée";
  const color = state.stale || criterion.status === "stale" ? COLORS.warning : criterion.status === "reviewed" ? COLORS.accent : COLORS.muted;
  const toggle = (field: "fileIds" | "checkIds", id: string) => setDraft((value) => ({ ...value, reviewed: false,
    [field]: value[field].includes(id) ? value[field].filter((item) => item !== id) : [...value[field], id] }));
  const save = async () => {
    if (!revision || readOnly || !currentDraft || !draft.fileIds.length && !draft.checkIds.length || draft.reviewed && !canReviewSelection) return;
    const updated = await state.save(criterion.index, { expected_version: criterion.mapping?.version ?? 0,
      context_sha256: view.context_sha256, conversation_revision: view.conversation_revision, criterion_sha256: criterion.sha256,
      project_id: revision.project_id, node_id: revision.node_id, revision_id: revision.id, revision_sha256: revision.sha256,
      file_ids: draft.fileIds, check_ids: draft.checkIds, review_status: draft.reviewed ? "reviewed" : "linked", public_explanation: draft.explanation });
    const updatedCriterion = updated?.criteria.find((item) => item.id === criterion.id);
    if (updated && updatedCriterion) { setDraft(draftFor(updated, updatedCriterion, state.connectionGeneration)); setExpanded(false); }
  };
  return <View style={{ borderTopWidth: 0.5, borderColor: COLORS.border, gap: 8, paddingVertical: 6 }}>
    <Pressable accessibilityRole="button" accessibilityLabel={`Exigence ${criterion.index + 1} : ${criterion.text}`}
      accessibilityState={{ expanded }} onPress={() => setExpanded(!expanded)} style={{ minHeight: 48, gap: 5, paddingVertical: 6 }}>
      <Text style={{ color: COLORS.text, fontWeight: "600", lineHeight: 21 }}>{criterion.index + 1}. {criterion.text}</Text>
      <Text style={{ color, fontSize: 13 }}>{status} · {expanded ? "Masquer −" : "Détails +"}</Text>
    </Pressable>
    {expanded ? <View style={{ gap: 10, paddingBottom: 10 }}>
      {criterion.mapping ? <SavedMapping mapping={criterion.mapping} /> : null}
      {!canMap ? <Text style={{ color: COLORS.warning, lineHeight: 20 }}>{revision
        ? "Cette révision provient d’un autre but. Une révision produite par ce but est nécessaire pour relier ses preuves."
        : "Aucune révision de fichiers disponible pour relier des preuves."}</Text> : null}
      {!currentDraft ? <>
        <Text style={{ color: COLORS.warning, lineHeight: 20 }}>Le contexte a changé. Votre note est conservée; sélectionnez à nouveau les preuves de la révision actuelle.</Text>
        <ActionButton label="Utiliser la révision actuelle" disabled={readOnly} onPress={() => setDraft({ ...draftFor(view, criterion, state.connectionGeneration), fileIds: [], checkIds: [], explanation: draft.explanation })} />
      </> : null}
      {revision && canMap ? <>
        <Text style={{ color: COLORS.text, fontWeight: "600" }}>Choisir les preuves · révision {revision.revision}</Text>
        <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Étape productrice : {revision.node_id}. Seuls les fichiers et contrôles de cette révision peuvent être liés.</Text>
        {revision.files.map((file) => <Checkbox key={file.id} label={`Fichier : ${file.path}`} checked={draft.fileIds.includes(file.id)} disabled={readOnly || !currentDraft} onPress={() => toggle("fileIds", file.id)} />)}
        {revision.checks.map((check) => <Checkbox key={check.id} label={`Contrôle ${check.index + 1} : ${check.command?.join(" ") ?? "commande non enregistrée"} — ${check.status === "passed" && check.exit_code === 0 ? "réussi" : check.status === "skipped" ? "ignoré" : "non réussi"}`}
          checked={draft.checkIds.includes(check.id)} disabled={readOnly || !currentDraft} onPress={() => toggle("checkIds", check.id)} />)}
        <Checkbox label="J’ai revu ces preuves pour cette exigence" checked={draft.reviewed} disabled={readOnly || !currentDraft || !canReviewSelection}
          onPress={() => setDraft((value) => ({ ...value, reviewed: !value.reviewed }))} />
        <Text style={{ color: COLORS.subtle, fontSize: 12, lineHeight: 18 }}>Sélectionnez au moins un contrôle pour une revue explicite. Tous les contrôles sélectionnés doivent être réussis avec une sortie 0. Cette revue ne certifie pas à elle seule le fonctionnement complet du projet.</Text>
      </> : null}
      <Text style={{ color: COLORS.text }}>Note de revue (facultative)</Text>
      <TextInput accessibilityLabel={`Note de revue pour l’exigence ${criterion.index + 1}`} multiline maxLength={2000}
        editable={!state.saving} value={draft.explanation} onChangeText={(explanation) => setDraft((value) => ({ ...value, explanation }))}
        placeholder="Ce que ces preuves permettent de vérifier" placeholderTextColor={COLORS.subtle}
        style={{ color: COLORS.text, backgroundColor: COLORS.background, borderColor: COLORS.border, borderWidth: 1, borderRadius: 10, padding: 12, minHeight: 80, textAlignVertical: "top" }} />
      <ActionButton label={draft.reviewed ? "Enregistrer la revue explicite" : "Enregistrer les liens de preuve"} busy={state.saving}
        disabled={readOnly || !currentDraft || !draft.fileIds.length && !draft.checkIds.length || draft.reviewed && !canReviewSelection}
        onPress={() => void save()} />
    </View> : null}
  </View>;
}

export function ProjectRequirementEvidence({ state, disabled = false }: { state: ProjectEvidenceState; disabled?: boolean }) {
  const { view } = state;
  const [showUnmatched, setShowUnmatched] = useState(false);
  const reviewed = view?.criteria.filter((criterion) => criterion.status === "reviewed").length ?? 0;
  const linked = view?.criteria.filter((criterion) => criterion.status === "linked").length ?? 0;
  return <Card testID="project-requirement-evidence">
    <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 17, fontWeight: "700" }}>Exigences et preuves</Text>
    <Text style={{ color: COLORS.muted, lineHeight: 20 }}>Reliez chaque exigence à des fichiers ou contrôles précis, puis indiquez votre revue explicitement.</Text>
    {state.stale ? <Text style={{ color: COLORS.warning }}>État non confirmé. Les liens conservés ne comptent pas comme une couverture actuelle.</Text>
      : view ? <Text style={{ color: COLORS.text }}>{reviewed}/{view.criteria.length} exigences revues explicitement · {linked} avec des preuves liées</Text> : null}
    <ErrorBanner message={state.error} />
    <ActionButton label="Actualiser les preuves des exigences" busy={state.busy} disabled={disabled || state.busy || state.saving} onPress={() => void state.refresh(true)} />
    {!view ? <Text style={{ color: COLORS.muted }}>Aucun relevé de couverture chargé.</Text> : <>
      {!view.criteria.length ? <Text style={{ color: COLORS.muted }}>Aucune exigence enregistrée pour ce but.</Text> : null}
      {view.criteria.map((criterion) => <CriterionEvidence key={criterion.id} criterion={criterion} view={view} state={state} disabled={disabled} />)}
      {view.unmatched_mappings.length ? <>
        <ActionButton label={`${showUnmatched ? "Masquer" : "Voir"} les liens d’anciennes exigences (${view.unmatched_mappings.length})`} onPress={() => setShowUnmatched(!showUnmatched)} />
        {showUnmatched ? view.unmatched_mappings.map((mapping) => <View key={mapping.id} style={{ gap: 5 }}>
          <Text style={{ color: COLORS.warning }}>Ancienne exigence · {mapping.criterion_text}</Text><SavedMapping mapping={mapping} />
        </View>) : null}
      </> : null}
    </>}
  </Card>;
}
