import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Pressable, Text, View } from "react-native";

import { ActionButton, Card, COLORS, ErrorBanner } from "@/components/swarm-ui";
import { ApiError, getActivity, type ActivityItem, type ActivityPage, type ActivityScopeType } from "@/lib/application-api/server";
import { useLiveRefresh } from "@/lib/sync/live-sync-context";
import { subscribeConnectionChanges } from "@/lib/connection-events";

type Props = { scope: ActivityScopeType; id: string; enabled: boolean; refreshKey?: number | string };
const KIND_LABELS = { model_call: "Modèle", worker_job: "Agent", tool_call: "Outil", project_revision: "Révision", project_check: "Vérification" };
const STATUS_LABELS = { queued: "En file", running: "En cours au dernier relevé", waiting: "En attente", completed: "Terminée", failed: "Échouée", cancelled: "Annulée", skipped: "Ignorée", recorded: "Enregistrée" };
const ROLE_LABELS = { planner: "Planification", evaluator: "Évaluation", summarizer: "Résumé", synthesizer: "Synthèse" };
const COVERAGE = "Seules les preuves enregistrées sont affichées, pas les opérations internes en direct. Les durées inconnues ne sont pas estimées.";

function activityReadError(cause: unknown): { message: string; resetRequired: boolean } {
  if (cause instanceof ApiError && cause.status === 409) {
    return { message: "L’historique a changé. Actualisez les opérations pour repartir du relevé le plus récent.", resetRequired: true };
  }
  if (cause instanceof ApiError && cause.status === 404) {
    return { message: "Les opérations détaillées ne sont pas disponibles pour ce travail sur ce serveur.", resetRequired: false };
  }
  return { message: "Les opérations n’ont pas pu être chargées. Actualisez pour réessayer.", resetRequired: false };
}

function appendActivityPage(previous: ActivityPage | null, next: ActivityPage): ActivityPage {
  if (!previous) return next;
  // Stable IDs identify persisted records. Overlapping pages update one row, never duplicate it.
  const items = new Map(previous.items.map((item) => [item.id, item]));
  next.items.forEach((item) => items.set(item.id, item));
  return { ...next, items: [...items.values()] };
}

function dateLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("fr-CA");
}

function ActivityRow({ item }: { item: ActivityItem }) {
  const [expanded, setExpanded] = useState(false);
  const color = item.status === "failed" ? COLORS.danger : item.status === "completed" ? COLORS.accent : COLORS.muted;
  const details = [
    ["Opération", item.id], ["But", item.goal_run_id], ["Tâche", item.task_id], ["Étape", item.node_id],
    ["Révision", item.detail.revision_id],
    ["Vérification", item.detail.check_index === null ? null : String(item.detail.check_index + 1)],
    ["Début enregistré", item.started_at === null ? null : dateLabel(item.started_at)],
    ["Fin enregistrée", item.completed_at === null ? null : dateLabel(item.completed_at)],
  ].filter(([, value]) => value !== null);
  return (
    <View style={{ borderTopColor: COLORS.border, borderTopWidth: 0.5, gap: 7, paddingVertical: 14 }} testID={`activity-item-${item.id}`}>
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8, justifyContent: "space-between" }}>
        <Text style={{ color: COLORS.muted, fontSize: 12 }}>{KIND_LABELS[item.kind]}{item.role ? ` · ${ROLE_LABELS[item.role]}` : ""}</Text>
        <Text style={{ color, fontSize: 12, fontWeight: "600" }}>{STATUS_LABELS[item.status]}</Text>
      </View>
      <Text selectable style={{ color: COLORS.text, fontSize: 15, fontWeight: "600" }}>{item.title}</Text>
      <Text style={{ color: COLORS.muted, fontSize: 12 }}>{dateLabel(item.recorded_at)}</Text>
      {item.agent_id ? <Text selectable style={{ color: COLORS.muted, fontSize: 13 }}>Agent : {item.agent_id}</Text> : null}
      {item.model_id ? <Text selectable style={{ color: COLORS.muted, fontSize: 13 }}>Modèle : {item.model_id}</Text> : null}
      {item.tool_name ? <Text selectable style={{ color: COLORS.muted, fontSize: 13 }}>Outil : {item.tool_name}</Text> : null}
      <Text style={{ color: COLORS.muted, fontSize: 13 }}>
        {item.duration_ms === null ? "Durée non enregistrée" : `Durée enregistrée : ${item.duration_ms.toLocaleString("fr-CA")} ms`}
      </Text>
      {item.detail.command !== null ? (
        <View style={{ gap: 3 }}>
          <Text style={{ color: COLORS.muted, fontSize: 12 }}>Commande enregistrée</Text>
          <Text selectable style={{ color: COLORS.text, fontSize: 13, fontFamily: "monospace" }}>{item.detail.command.join(" ")}</Text>
        </View>
      ) : null}
      {item.detail.exit_code !== null ? <Text style={{ color: COLORS.muted, fontSize: 13 }}>Code de sortie : {item.detail.exit_code}</Text> : null}
      {item.detail.file_count !== null ? <Text style={{ color: COLORS.muted, fontSize: 13 }}>Fichiers enregistrés : {item.detail.file_count}</Text> : null}
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Détails de ${item.title}`}
        accessibilityState={{ expanded }}
        onPress={() => setExpanded((value) => !value)}
        style={({ pressed }) => ({ justifyContent: "center", minHeight: 44, opacity: pressed ? 0.6 : 1 })}
      >
        <Text style={{ color: COLORS.muted, fontSize: 13 }}>{expanded ? "Masquer les détails −" : "Identifiants et horodatages +"}</Text>
      </Pressable>
      {expanded ? <View style={{ gap: 4 }}>{details.map(([label, value]) => <Text key={label} selectable style={{ color: COLORS.muted, fontSize: 12 }}>{label} : {value}</Text>)}</View> : null}
    </View>
  );
}

/** Scope changes remount the session so no page or pending read crosses task/goal identity. */
export function ActivityTimeline(props: Props) {
  return <ActivitySession key={`${props.scope}:${props.id}`} {...props} />;
}

function ActivitySession({ scope, id, enabled, refreshKey }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [page, setPage] = useState<ActivityPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [resetRequired, setResetRequired] = useState(false);
  const epoch = useRef(0);
  const busyRef = useRef(false);
  const resetRef = useRef(false);
  const activeRef = useRef(AppState.currentState === "active");
  const enabledRef = useRef(enabled);
  const expandedRef = useRef(expanded);
  enabledRef.current = enabled;
  expandedRef.current = expanded;

  const load = useCallback(async (cursor?: string, explicit = false) => {
    if (!activeRef.current || !enabledRef.current || !expandedRef.current || (resetRef.current && !explicit) || (cursor && busyRef.current)) return;
    if (explicit) { resetRef.current = false; setResetRequired(false); }
    const current = ++epoch.current;
    const shouldAccept = () => current === epoch.current && activeRef.current && enabledRef.current && expandedRef.current;
    busyRef.current = true;
    setBusy(true); setError(null); setStale(true);
    try {
      const result = await getActivity(scope, id, cursor, shouldAccept);
      if (!shouldAccept()) return;
      if (cursor && result.next_cursor === cursor) throw new Error("Le curseur d’activité n’a pas avancé.");
      setPage((previous) => cursor ? appendActivityPage(previous, result) : result);
      setStale(false);
    } catch (cause) {
      if (!shouldAccept()) return;
      const failure = activityReadError(cause);
      if (failure.resetRequired) { resetRef.current = true; setResetRequired(true); }
      setError(failure.message);
    } finally {
      if (current === epoch.current) { busyRef.current = false; setBusy(false); }
    }
  }, [scope, id]);

  useLiveRefresh(() => load());
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      activeRef.current = state === "active";
      if (activeRef.current) void load();
      else {
        epoch.current += 1; busyRef.current = false;
        setBusy(false); setStale(true);
      }
    });
    return () => subscription.remove();
  }, [load]);

  useEffect(() => subscribeConnectionChanges(() => {
    epoch.current += 1; busyRef.current = false;
    // A cursor and its rows are private to one authenticated connection.
    resetRef.current = true; setResetRequired(true);
    setPage(null); setBusy(false); setStale(true);
    setError("Le jumelage a changé. Actualisez les opérations depuis cette connexion.");
  }), []);

  useEffect(() => {
    if (!enabled || !expanded) {
      epoch.current += 1; busyRef.current = false; setBusy(false); setStale(true);
    } else {
      void load();
    }
    return () => { epoch.current += 1; };
  }, [enabled, expanded, refreshKey, load]);

  return (
    <Card style={{ gap: 0 }}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel="Opérations détaillées"
        accessibilityHint="Afficher les appels et vérifications enregistrés pour ce travail."
        accessibilityState={{ expanded }}
        onPress={() => setExpanded((value) => !value)}
        testID="activity-timeline-toggle"
        style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8, minHeight: 44, opacity: pressed ? 0.7 : 1 })}
      >
        <Text style={{ color: COLORS.text, fontSize: 15, fontWeight: "600", flex: 1 }}>Opérations détaillées</Text>
        <Text accessible={false} style={{ color: COLORS.muted, fontSize: 20 }}>{expanded ? "−" : "+"}</Text>
      </Pressable>
      {expanded ? (
        <View style={{ gap: 12, paddingTop: 10 }}>
          <Text style={{ color: COLORS.muted, fontSize: 12, lineHeight: 18 }}>{COVERAGE}</Text>
          {!enabled ? <Text accessibilityRole="alert" style={{ color: COLORS.muted }}>Connexion requise pour actualiser les opérations.</Text> : null}
          {page && (stale || !enabled) ? <Text style={{ color: COLORS.warning, fontSize: 12 }}>Dernier relevé conservé ; son état n’est pas confirmé actuellement.</Text> : null}
          <ActionButton label="Actualiser les opérations" onPress={() => void load(undefined, true)} busy={busy} disabled={!enabled || busy} testID="activity-timeline-refresh" />
          <ErrorBanner message={error} />
          {!page && busy ? <Text style={{ color: COLORS.muted }}>Chargement des preuves enregistrées…</Text> : null}
          {page ? <>
            <Text style={{ color: COLORS.muted, fontSize: 12 }}>Les plus récentes d’abord · {page.items.length} opération{page.items.length > 1 ? "s" : ""} affichée{page.items.length > 1 ? "s" : ""}</Text>
            {page.items.length === 0 ? <Text style={{ color: COLORS.muted }}>Aucune opération enregistrée dans ce relevé.</Text> : page.items.map((item) => <ActivityRow key={item.id} item={item} />)}
            {page.has_more ? <ActionButton label="Voir les opérations précédentes" disabled={!enabled || busy || resetRequired} onPress={() => void load(page.next_cursor ?? undefined)} testID="activity-timeline-more" /> : null}
          </> : null}
        </View>
      ) : null}
    </Card>
  );
}
