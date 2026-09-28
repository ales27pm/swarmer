import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Pressable, Text, View } from "react-native";

import { ActionButton, Card, COLORS, ErrorBanner } from "@/components/swarm-ui";
import { ApiError, getActivity, type ActivityItem, type ActivityPage, type ActivityScopeType, type PlanNode } from "@/lib/application-api/server";
import { useLiveRefresh, useLiveSync } from "@/lib/sync/live-sync-context";
import { subscribeConnectionChanges } from "@/lib/connection-events";

type Props = {
  scope: ActivityScopeType; id: string; enabled: boolean; refreshKey?: number | string;
  /** Follow persisted activity while the detailed history stays collapsed. */
  follow?: boolean;
  nodes?: PlanNode[];
  selectedNodeId?: string | null;
  onOpenTask?: (taskId: string) => void;
};
const KIND_LABELS = { model_call: "Modèle", worker_job: "Agent", tool_call: "Outil", project_revision: "Révision", project_check: "Vérification" };
const STATUS_LABELS = { queued: "En file", running: "En cours au dernier relevé", waiting: "En attente", completed: "Terminée", failed: "Échouée", cancelled: "Annulée", skipped: "Ignorée", recorded: "Enregistrée" };
const ROLE_LABELS = { planner: "Planification", evaluator: "Évaluation", summarizer: "Résumé", synthesizer: "Synthèse" };
const COVERAGE = "Seules les preuves enregistrées sont affichées, pas les opérations internes en direct. Les durées inconnues ne sont pas estimées.";
const KIND_FILTERS: { key: ActivityItem["kind"] | "all"; label: string }[] = [
  { key: "all", label: "Toutes" },
  { key: "model_call", label: "Modèles" },
  { key: "worker_job", label: "Agents" },
  { key: "tool_call", label: "Outils" },
  { key: "project_revision", label: "Fichiers" },
  { key: "project_check", label: "Vérifications" },
];

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

function ActivityAttribution({ item, node, onOpenTask, technical = true }: {
  item: ActivityItem; node?: PlanNode; onOpenTask?: Props["onOpenTask"]; technical?: boolean;
}) {
  return (
    <View style={{ gap: 5 }}>
      {node ? <Text style={{ color: COLORS.text, fontSize: 13 }}>Étape liée : {node.title}</Text> : null}
      {technical && item.model_id ? <Text selectable style={{ color: COLORS.info, fontSize: 12 }}>Modèle : {item.model_id}</Text> : null}
      {technical && item.agent_id ? <Text selectable style={{ color: COLORS.muted, fontSize: 12 }}>Agent : {item.agent_id}</Text> : null}
      {technical && item.tool_name ? <Text selectable style={{ color: COLORS.muted, fontSize: 12 }}>Outil / compétence : {item.tool_name}</Text> : null}
      {item.task_id && onOpenTask ? (
        <Pressable accessibilityRole="button" accessibilityLabel={`Ouvrir la tâche de ${item.title}`}
          onPress={() => onOpenTask(item.task_id!)} style={{ minHeight: 44, justifyContent: "center" }}>
          <Text style={{ color: COLORS.accent, fontSize: 13, fontWeight: "600" }}>Ouvrir la tâche liée</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

function ActivityRow({ item, node, onOpenTask, showAttribution }: {
  item: ActivityItem; node?: PlanNode; onOpenTask?: Props["onOpenTask"]; showAttribution?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const color = item.status === "failed" ? COLORS.danger : item.status === "completed" ? COLORS.accent : item.status === "running" ? COLORS.info : COLORS.muted;
  const durationLabel = item.duration_ms === null ? "Durée non enregistrée" : `Durée enregistrée : ${item.duration_ms.toLocaleString("fr-CA")} ms`;
  const details = [
    ...(!showAttribution ? [["Agent", item.agent_id], ["Modèle", item.model_id], ["Outil", item.tool_name]] : []),
    ["Opération", item.id], ["Projet", item.goal_run_id], ["Tâche", item.task_id], ["Étape", item.node_id],
    ["Révision", item.detail.revision_id],
    ["Vérification", item.detail.check_index === null ? null : String(item.detail.check_index + 1)],
    ["Début enregistré", item.started_at === null ? null : dateLabel(item.started_at)],
    ["Fin enregistrée", item.completed_at === null ? null : dateLabel(item.completed_at)],
  ].filter(([, value]) => value !== null);
  return (
    <View style={{ borderTopColor: COLORS.border, borderTopWidth: 0.5, gap: 8, paddingVertical: 12 }} testID={`activity-item-${item.id}`}>
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8, justifyContent: "space-between" }}>
        <Text style={{ color: COLORS.muted, fontSize: 12 }}>{KIND_LABELS[item.kind]}{item.role ? ` · ${ROLE_LABELS[item.role]}` : ""}</Text>
        <View style={{ alignItems: "center", flexDirection: "row", gap: 5 }}>
          <View accessible={false} style={{ backgroundColor: color, borderRadius: 3, height: 6, width: 6 }} />
          <Text style={{ color, fontSize: 12, fontWeight: "600" }}>{STATUS_LABELS[item.status]}</Text>
        </View>
      </View>
      <Text selectable numberOfLines={expanded ? undefined : 2} style={{ color: COLORS.text, fontSize: 15, fontWeight: "600", lineHeight: 21 }}>{item.title}</Text>
      {showAttribution ? <ActivityAttribution item={item} node={node} onOpenTask={onOpenTask} /> : null}
      <Text style={{ color: COLORS.muted, fontSize: 12 }}>{dateLabel(item.recorded_at)}</Text>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Détails de ${item.title}`}
        accessibilityHint={durationLabel}
        accessibilityState={{ expanded }}
        onPress={() => setExpanded((value) => !value)}
        style={({ pressed }) => ({ alignItems: "center", flexDirection: "row", flexWrap: "wrap", gap: 8, justifyContent: "space-between", minHeight: 44, opacity: pressed ? 0.6 : 1 })}
      >
        <Text style={{ color: COLORS.muted, fontSize: 12 }}>
          {durationLabel}
        </Text>
        <Text style={{ color: COLORS.accent, fontSize: 13, fontWeight: "600" }}>{expanded ? "Réduire −" : "Détails +"}</Text>
      </Pressable>
      {expanded ? (
        <View style={{ backgroundColor: COLORS.background, borderRadius: 12, gap: 8, padding: 12 }}>
          {item.detail.command !== null ? (
            <View style={{ gap: 3 }}>
              <Text style={{ color: COLORS.muted, fontSize: 12 }}>Commande enregistrée</Text>
              <Text selectable style={{ color: COLORS.text, fontSize: 13, fontFamily: "monospace" }}>{item.detail.command.join(" ")}</Text>
            </View>
          ) : null}
          {item.detail.exit_code !== null ? <Text style={{ color: COLORS.muted, fontSize: 13 }}>Code de sortie : {item.detail.exit_code}</Text> : null}
          {item.detail.file_count !== null ? <Text style={{ color: COLORS.muted, fontSize: 13 }}>Fichiers enregistrés : {item.detail.file_count}</Text> : null}
          {details.map(([label, value]) => <Text key={label} selectable style={{ color: COLORS.muted, fontSize: 12, lineHeight: 18 }}>{label} : {value}</Text>)}
        </View>
      ) : null}
    </View>
  );
}

/** Presentation-only filters never fetch, alter, or infer records outside the loaded pages. */
function ActivityRecords({ page, nodes = [], selectedNodeId, onOpenTask, follow }: {
  page: ActivityPage;
} & Pick<Props, "nodes" | "selectedNodeId" | "onOpenTask" | "follow">) {
  const [filter, setFilter] = useState<ActivityItem["kind"] | "all">("all");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const scopedItems = selectedNodeId ? page.items.filter((item) => item.node_id === selectedNodeId) : page.items;
  const visibleItems = filter === "all" ? scopedItems : scopedItems.filter((item) => item.kind === filter);
  const selectedLabel = KIND_FILTERS.find((item) => item.key === filter)?.label;
  return (
    <View style={{ gap: 8 }}>
      {selectedNodeId ? <Text style={{ color: COLORS.info, fontSize: 13 }}>Étape sélectionnée : {nodes.find((node) => node.id === selectedNodeId)?.title ?? selectedNodeId}</Text> : null}
      <View style={{ alignItems: "center", flexDirection: "row", flexWrap: "wrap", gap: 8, justifyContent: "space-between" }}>
        <Text accessibilityLiveRegion="polite" style={{ color: COLORS.muted, fontSize: 12 }}>
          {visibleItems.length} sur {scopedItems.length} opérations chargées
        </Text>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Filtrer les opérations"
          accessibilityState={{ expanded: filtersOpen }}
          accessibilityValue={{ text: selectedLabel }}
          onPress={() => setFiltersOpen((value) => !value)}
          style={({ pressed }) => ({ justifyContent: "center", minHeight: 44, opacity: pressed ? 0.6 : 1 })}
        >
          <Text style={{ color: COLORS.accent, fontSize: 13, fontWeight: "600" }}>
            {filter === "all" ? "Filtrer" : selectedLabel} {filtersOpen ? "−" : "+"}
          </Text>
        </Pressable>
      </View>
      {filtersOpen ? (
        <View style={{ gap: 8 }}>
          <Text style={{ color: COLORS.muted, fontSize: 12, lineHeight: 18 }}>
            Le filtre s’applique uniquement aux opérations déjà chargées.
          </Text>
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
            {KIND_FILTERS.map((option) => {
              const selected = filter === option.key;
              const count = option.key === "all" ? scopedItems.length : scopedItems.filter((item) => item.kind === option.key).length;
              return (
                <Pressable
                  key={option.key}
                  accessibilityRole="button"
                  accessibilityLabel={`${option.label}, ${count} opérations chargées`}
                  accessibilityState={{ selected }}
                  onPress={() => setFilter(option.key)}
                  style={({ pressed }) => ({ backgroundColor: selected ? `${COLORS.accent}1f` : COLORS.panelRaised, borderColor: selected ? COLORS.accent : COLORS.border, borderRadius: 999, borderWidth: 1, justifyContent: "center", minHeight: 44, opacity: pressed ? 0.6 : 1, paddingHorizontal: 12 })}
                  testID={`activity-filter-${option.key}`}
                >
                  <Text style={{ color: selected ? COLORS.accent : COLORS.muted, fontSize: 13, fontWeight: "600" }}>{option.label} · {count}</Text>
                </Pressable>
              );
            })}
          </View>
        </View>
      ) : null}
      <Text style={{ color: COLORS.muted, fontSize: 12 }}>Les plus récentes d’abord</Text>
      {page.items.length === 0 ? <Text style={{ color: COLORS.muted }}>Aucune opération enregistrée dans ce relevé.</Text>
        : visibleItems.length === 0 ? <Text style={{ color: COLORS.muted, lineHeight: 19 }}>Aucune opération de ce type parmi celles déjà chargées.</Text>
          : visibleItems.map((item) => <ActivityRow key={item.id} item={item} node={nodes.find((node) => node.id === item.node_id && node.goal_run_id === item.goal_run_id)} onOpenTask={onOpenTask} showAttribution={follow} />)}
    </View>
  );
}

/** Scope changes remount the session so no page or pending read crosses task/goal identity. */
export function ActivityTimeline(props: Props) {
  return <ActivitySession key={`${props.scope}:${props.id}`} {...props} />;
}

function ActivitySession({ scope, id, enabled, refreshKey, follow = false, nodes = [], selectedNodeId, onOpenTask }: Props) {
  const [expanded, setExpanded] = useState(false);
  const { state: liveState } = useLiveSync();
  const [page, setPage] = useState<ActivityPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [resetRequired, setResetRequired] = useState(false);
  const [receivedAt, setReceivedAt] = useState<string | null>(null);
  const epoch = useRef(0);
  const busyRef = useRef(false);
  const resetRef = useRef(false);
  const activeRef = useRef(AppState.currentState === "active");
  const enabledRef = useRef(enabled);
  const watching = expanded || follow;
  const expandedRef = useRef(watching);
  enabledRef.current = enabled;
  expandedRef.current = watching;

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
      setReceivedAt(new Date().toISOString());
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
    setPage(null); setReceivedAt(null); setBusy(false); setStale(true);
    setError("Le jumelage a changé. Actualisez les opérations depuis cette connexion.");
  }), []);

  useEffect(() => {
    if (!enabled || !watching) {
      epoch.current += 1; busyRef.current = false; setBusy(false); setStale(true);
    } else {
      void load();
    }
    return () => { epoch.current += 1; };
  }, [enabled, watching, refreshKey, load]);

  const isUnconfirmed = stale || !enabled || (follow && liveState !== "connected");
  const scopedItems = page?.items.filter((item) => !selectedNodeId || item.node_id === selectedNodeId) ?? [];
  const activeItems = scopedItems.filter((item) => item.status === "running" || item.status === "queued" || item.status === "waiting");

  return (
    <Card style={{ gap: 0 }}>
      {follow ? (
        <View style={{ gap: 10, paddingBottom: 12 }} testID="activity-live-summary">
          <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 17, fontWeight: "700" }}>Suivi des opérations</Text>
          {selectedNodeId ? <Text style={{ color: COLORS.muted, fontSize: 13 }}>Étape suivie : {nodes.find((node) => node.id === selectedNodeId)?.title ?? "étape sélectionnée"}</Text> : null}
          <Text accessibilityLiveRegion="polite" style={{ color: isUnconfirmed ? COLORS.warning : COLORS.accent, fontSize: 12 }}>
            {!enabled ? "Relevé hors ligne" : liveState === "connected" ? "Synchronisation connectée" : "Synchronisation interrompue"}
          </Text>
          <Text style={{ color: COLORS.muted, fontSize: 12, lineHeight: 18 }}>
            Les événements du serveur actualisent ce relevé. Les calculs internes et les jetons non enregistrés ne sont pas visibles.
          </Text>
          {receivedAt ? <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Relevé reçu : {dateLabel(receivedAt)}</Text> : null}
          {page ? <>
            <Text style={{ color: COLORS.text, fontWeight: "600" }}>
              {activeItems.length ? `${activeItems.length} opération${activeItems.length > 1 ? "s" : ""} non terminée${activeItems.length > 1 ? "s" : ""} dans ce relevé` : "Aucune opération active dans ce relevé"}
            </Text>
            {page.has_more ? <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Relevé partiel : d’autres opérations restent dans l’historique.</Text> : null}
            {!expanded ? (activeItems.length ? activeItems : scopedItems.slice(0, 1)).slice(0, 3).map((item) => (
              <View key={item.id} style={{ borderLeftWidth: 3, borderLeftColor: item.status === "failed" ? COLORS.danger : COLORS.info, paddingLeft: 10, gap: 6 }}>
                <Text style={{ color: COLORS.muted, fontSize: 12 }}>{KIND_LABELS[item.kind]}{item.role ? ` · ${ROLE_LABELS[item.role]}` : ""} · {STATUS_LABELS[item.status]}</Text>
                <Text selectable style={{ color: COLORS.text, fontSize: 14, fontWeight: "600" }}>{item.title}</Text>
                <ActivityAttribution technical={false} item={item} node={nodes.find((node) => node.id === item.node_id && node.goal_run_id === item.goal_run_id)} onOpenTask={onOpenTask} />
              </View>
            )) : null}
            {!expanded && activeItems.length > 3 ? <Text style={{ color: COLORS.muted, fontSize: 12 }}>{activeItems.length - 3} autres opérations dans les détails.</Text> : null}
          </> : null}
        </View>
      ) : null}
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
      {watching ? (
        <View style={{ gap: 12, paddingTop: 10 }}>
          {expanded ? <Text style={{ color: COLORS.muted, fontSize: 12, lineHeight: 18 }}>{COVERAGE}</Text> : null}
          {!enabled ? <Text accessibilityRole="alert" style={{ color: COLORS.muted }}>Connexion requise pour actualiser les opérations.</Text> : null}
          {page && isUnconfirmed ? <Text style={{ color: COLORS.warning, fontSize: 12 }}>Dernier relevé conservé ; son état n’est pas confirmé actuellement.</Text> : null}
          <ActionButton label="Actualiser les opérations" onPress={() => void load(undefined, true)} busy={busy} disabled={!enabled || busy} testID="activity-timeline-refresh" />
          <ErrorBanner message={error} />
          {!page && busy ? <Text style={{ color: COLORS.muted }}>Chargement des preuves enregistrées…</Text> : null}
          {page && expanded ? <>
            <ActivityRecords page={page} nodes={nodes} selectedNodeId={selectedNodeId} onOpenTask={onOpenTask} follow={follow} />
            {page.has_more ? <ActionButton label="Voir les opérations précédentes" disabled={!enabled || busy || resetRequired} onPress={() => void load(page.next_cursor ?? undefined)} testID="activity-timeline-more" /> : null}
          </> : null}
        </View>
      ) : null}
    </Card>
  );
}
