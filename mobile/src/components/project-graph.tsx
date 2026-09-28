import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Platform, Pressable, Text, View } from "react-native";

import Svg, { Circle, Defs, G, Marker, Path } from "react-native-svg";

import { ActionButton, Card, COLORS, ErrorBanner } from "./swarm-ui";
import { ApiError, getProjectGraph, type PlanNode, type ProjectGraph } from "@/lib/application-api/server";
import { evidenceStaleReasons, type EvidenceMapping, type ProjectEvidenceView } from "@/lib/api/project-evidence";
import { subscribeConnectionChanges } from "@/lib/connection-events";
import { useLiveRefresh, useLiveSync } from "@/lib/sync/live-sync-context";

const NODE_LABELS: Record<PlanNode["status"], string> = {
  planned: "Planifiée", ready: "Prête", dispatched: "Distribuée", running: "En cours",
  waiting_permission: "Accord attendu", waiting_capability: "iPhone attendu", completed: "Terminée",
  failed: "Échouée", blocked: "Bloquée", cancelled: "Annulée", skipped: "Ignorée",
};
const EVALUATION_LABELS: Record<ProjectGraph["evaluations"][number]["status"], string> = {
  continue: "Poursuivre", replan: "Replanifier", done: "Terminer", failed: "Signaler un échec", needs_user: "Demander votre intervention",
};
const stamp = (value: string) => new Date(value).toLocaleString("fr-CA");
const nodeColor = (status: PlanNode["status"]) => status === "running" || status === "dispatched" ? COLORS.info
  : status === "completed" ? COLORS.accent : status === "failed" ? COLORS.danger
    : status === "blocked" || status.startsWith("waiting_") ? COLORS.warning : COLORS.muted;

export function useProjectGraph(goalId: string, enabled: boolean, refreshKey: string) {
  const [graph, setGraph] = useState<ProjectGraph | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(true);
  const epoch = useRef(0);
  const active = useRef(AppState.currentState === "active");
  const allowed = useRef(enabled);
  const connectionChanged = useRef(false);
  const { state: liveState } = useLiveSync();
  allowed.current = enabled;
  const refresh = useCallback(async (explicit = false) => {
    if (!allowed.current || !active.current || (connectionChanged.current && !explicit)) return;
    if (explicit) connectionChanged.current = false;
    const current = ++epoch.current;
    const accepts = () => epoch.current === current && allowed.current && active.current;
    setBusy(true); setStale(true); setError(null);
    try {
      const value = await getProjectGraph(goalId, accepts);
      if (accepts()) { setGraph(value); setStale(false); }
    } catch (cause) {
      if (accepts()) setError(cause instanceof ApiError && cause.status === 404
        ? "Le parcours détaillé n’est pas encore disponible sur ce serveur. Le plan enregistré reste consultable."
        : "Le parcours n’a pas pu être actualisé. Le relevé précédent reste en lecture seule.");
    } finally { if (current === epoch.current) setBusy(false); }
  }, [goalId]);
  useLiveRefresh(() => refresh());
  useEffect(() => {
    if (enabled) void refresh();
    else { epoch.current += 1; setStale(true); setBusy(false); }
    return () => { epoch.current += 1; };
  }, [enabled, refreshKey, refresh]);
  useEffect(() => subscribeConnectionChanges(() => {
    epoch.current += 1; connectionChanged.current = true;
    setGraph(null); setStale(true); setBusy(false);
    setError("Le jumelage a changé. Actualisez le parcours depuis cette connexion.");
  }), []);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      active.current = state === "active";
      if (active.current) void refresh();
      else { epoch.current += 1; setStale(true); setBusy(false); }
    });
    return () => subscription.remove();
  }, [refresh]);
  return { graph, busy, error, stale: stale || !enabled || liveState !== "connected", refresh };
}

export type ProjectGraphState = ReturnType<typeof useProjectGraph>;

function Decision({ value }: { value: ProjectGraph["evaluations"][number] }) {
  return (
    <View style={{ gap: 8 }}>
      <Text style={{ color: COLORS.text, fontSize: 15, fontWeight: "700" }}>Décision proposée : {EVALUATION_LABELS[value.status]}</Text>
      <Text selectable style={{ color: COLORS.text, lineHeight: 21 }}>{value.reason_summary}</Text>
      <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Évaluateur · modèle non renseigné · {stamp(value.created_at)}</Text>
      {value.missing_requirements.length ? <View style={{ gap: 4 }}>
        <Text style={{ color: COLORS.warning, fontSize: 13, fontWeight: "600" }}>À compléter selon cette évaluation</Text>
        {value.missing_requirements.map((text, index) => <Text key={index} selectable style={{ color: COLORS.muted, fontSize: 13 }}>• {text}</Text>)}
      </View> : null}
      {value.invalid_results.length ? <View style={{ gap: 4 }}>
        <Text style={{ color: COLORS.warning, fontSize: 13, fontWeight: "600" }}>Résultats à vérifier selon le modèle</Text>
        {value.invalid_results.map((text, index) => <Text key={index} selectable style={{ color: COLORS.muted, fontSize: 13 }}>• {text}</Text>)}
      </View> : null}
    </View>
  );
}

/** Layers express recorded dependencies, never scheduling promises or completion coverage. */
export function projectLayers(nodes: PlanNode[], edges: ProjectGraph["dependencies"]) {
  const remaining = new Set(nodes.map((node) => node.id));
  const layers: PlanNode[][] = [];
  while (remaining.size) {
    const ready = nodes.filter((node) => remaining.has(node.id)
      && !edges.some((edge) => edge.to_node_id === node.id && remaining.has(edge.from_node_id)));
    if (!ready.length) return null;
    layers.push(ready); ready.forEach((node) => remaining.delete(node.id));
  }
  return layers;
}

type GraphPoint = { x: number; y: number };
type GraphPosition = GraphPoint & { width: number; height: number; level: number };
const NODE_HEIGHT = 90;
const SIDE_GUTTER = 18;

type NodeEvidenceLink = { mapping: EvidenceMapping; historical: boolean; status: "linked" | "reviewed" | "stale" };

function revisionEvidenceSnapshot(revision: ProjectGraph["latest_revision"]) {
  return revision ? JSON.stringify([revision.id, revision.project_id, revision.goal_run_id, revision.node_id, revision.worker_job_id,
    revision.revision, revision.sha256, revision.created_at,
    revision.files.map((file) => [file.id, file.path, file.sha256, file.bytes]),
    revision.checks.map((check) => [check.id, check.index, check.command, check.status, check.exit_code, check.duration_ms, check.provenance])]) : null;
}

/** Join only persisted producer IDs. A completed node is never an evidence link. */
function nodeEvidenceLinks(graph: ProjectGraph | null, evidence: ProjectEvidenceView | null, stale: boolean) {
  const links = new Map<string, NodeEvidenceLink[]>();
  if (!graph || !evidence || evidence.goal_run_id !== graph.goal.id || evidence.project_id !== graph.project_id) return links;
  const unconfirmed = stale || graph.conversation_revision !== evidence.conversation_revision;
  // These endpoints refresh independently; equal source digests alone do not bind receipts or producers.
  const revisionChanged = revisionEvidenceSnapshot(graph.latest_revision) !== revisionEvidenceSnapshot(evidence.current_revision);
  const append = (mapping: EvidenceMapping, historical: boolean) => {
    if (mapping.goal_run_id !== graph.goal.id || !graph.nodes.some((node) => node.id === mapping.node_id)) return;
    historical ||= revisionChanged || mapping.revision_id !== graph.latest_revision?.id || mapping.revision_sha256 !== graph.latest_revision?.sha256
      || !graph.criteria.some((criterion) => criterion.index === mapping.criterion_index && criterion.text === mapping.criterion_text);
    const item: NodeEvidenceLink = { mapping, historical, status: historical || unconfirmed ? "stale" : mapping.review_status };
    links.set(mapping.node_id, [...(links.get(mapping.node_id) ?? []), item]);
  };
  for (const criterion of evidence.criteria) {
    if (criterion.mapping) append(criterion.mapping, criterion.status === "stale" || evidenceStaleReasons(criterion.mapping, evidence, criterion).length > 0);
  }
  for (const mapping of evidence.unmatched_mappings) append(mapping, true);
  return links;
}

function evidenceNodeLabel(links: NodeEvidenceLink[]) {
  const numbers = links.slice(0, 3).map(({ mapping }) => mapping.criterion_index + 1).join(", ");
  const suffix = links.length > 3 ? ` +${links.length - 3}` : "";
  return `Exigences ${numbers}${suffix}${links.every((link) => link.historical) ? " · historique" : ""}`;
}

export function ProjectGraphNodeEvidence({ graph, nodeId, evidence = null, stale = false, onOpenEvidence }: {
  graph: ProjectGraph | null; nodeId: string; evidence?: ProjectEvidenceView | null; stale?: boolean; onOpenEvidence: () => void;
}) {
  const links = nodeEvidenceLinks(graph, evidence, stale).get(nodeId) ?? [];
  const scopeConfirmed = graph && evidence && !stale && evidence.goal_run_id === graph.goal.id
    && evidence.project_id === graph.project_id && evidence.conversation_revision === graph.conversation_revision;
  return <View testID="project-node-evidence" style={{ gap: 8, borderTopWidth: 1, borderColor: COLORS.border, paddingTop: 12 }}>
    <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 16, fontWeight: "700" }}>Exigences et preuves de cette étape</Text>
    {links.length ? <>
      <Text style={{ color: COLORS.muted }}>Liées : {links.filter((link) => link.status === "linked").length} · Revues explicitement : {links.filter((link) => link.status === "reviewed").length} · À revoir : {links.filter((link) => link.status === "stale").length}</Text>
      {links.map(({ mapping, historical, status }) => <View key={mapping.id} style={{ gap: 4 }}>
        <Text selectable style={{ color: status === "stale" ? COLORS.warning : COLORS.text, lineHeight: 20 }}>
          Exigence {mapping.criterion_index + 1} : {mapping.criterion_text}
        </Text>
        <Text style={{ color: status === "stale" ? COLORS.warning : COLORS.muted, fontSize: 12 }}>
          {historical ? "Lien historique — à revoir" : status === "stale" ? "État du lien non confirmé" : status === "reviewed" ? "Revue explicite enregistrée" : "Preuves liées, sans revue explicite"}
        </Text>
        <Text selectable style={{ color: COLORS.subtle, fontSize: 12 }}>Révision liée : {mapping.revision_id} · {mapping.files.length} fichier(s) · {mapping.checks.length} contrôle(s)</Text>
      </View>)}
      <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Ces liens ne sont pas déduits du statut de l’étape et ne certifient pas la réussite du projet.</Text>
    </> : <Text style={{ color: COLORS.muted }}>{scopeConfirmed ? "Aucune exigence explicitement reliée à cette étape." : "Les associations actuelles ne sont pas confirmées dans ce relevé."}</Text>}
    <ActionButton label="Voir et modifier les preuves dans Résultats" onPress={onOpenEvidence} />
  </View>;
}

/** Orthogonal routes keep unrelated cards clear, including wrapped rows of one layer. */
export function projectDependencyLayout(nodes: PlanNode[], edges: ProjectGraph["dependencies"], width: number, nodeHeight = NODE_HEIGHT) {
  const layers = projectLayers(nodes, edges);
  if (!layers) return null;
  const columns = width >= 270 ? 2 : 1;
  const positions = new Map<string, GraphPosition>();
  let height = 0;
  layers.forEach((layer, level) => {
    const count = Math.min(columns, layer.length);
    const boxWidth = (width - SIDE_GUTTER * 2 - (count - 1) * 12) / count;
    layer.forEach((node, index) => positions.set(node.id, {
      x: SIDE_GUTTER + index % count * (boxWidth + 12), y: height + Math.floor(index / count) * (nodeHeight + 16),
      width: boxWidth, height: nodeHeight, level,
    }));
    height += Math.ceil(layer.length / count) * (nodeHeight + 16) + 30;
  });
  const intersects = (a: GraphPoint, b: GraphPoint, box: GraphPosition) => {
    // Four pixels of clearance prevents strokes from appearing attached to another card.
    const left = box.x - 4; const right = box.x + box.width + 4;
    const top = box.y - 4; const bottom = box.y + box.height + 4;
    return a.x === b.x
      ? a.x >= left && a.x <= right && Math.max(a.y, b.y) >= top && Math.min(a.y, b.y) <= bottom
      : a.y >= top && a.y <= bottom && Math.max(a.x, b.x) >= left && Math.min(a.x, b.x) <= right;
  };
  const routes = edges.map((edge, index) => {
    const from = positions.get(edge.from_node_id)!; const to = positions.get(edge.to_node_id)!;
    const start = { x: from.x + from.width / 2, y: from.y + from.height + 3 };
    const end = { x: to.x + to.width / 2, y: to.y - 3 };
    const exit = { x: start.x, y: from.y + from.height + 8 };
    const approach = { x: end.x, y: to.y - 8 };
    let points = [start, exit, { x: end.x, y: exit.y }, approach, end];
    const obstructed = [...positions].some(([id, box]) => id !== edge.from_node_id && id !== edge.to_node_id
      && points.slice(1).some((point, segment) => intersects(points[segment], point, box)));
    if (obstructed) {
      // All cards are inset; these lanes also remain clear for edges skipping whole levels.
      const lane = 4 + index % 3 * 4;
      const gutterX = start.x <= width / 2 ? lane : width - lane;
      points = [start, exit, { x: gutterX, y: exit.y }, { x: gutterX, y: approach.y }, approach, end];
    }
    return { edge, points, path: points.map((point, part) => `${part ? "L" : "M"}${point.x},${point.y}`).join(" ") };
  });
  return { positions, routes, height: Math.max(0, height - 30) };
}

function DependencyMap({ nodes, edges, selectedNodeId, onSelectNode, evidenceLinks }: {
  nodes: PlanNode[]; edges: ProjectGraph["dependencies"]; selectedNodeId: string | null; onSelectNode: (id: string | null) => void;
  evidenceLinks: Map<string, NodeEvidenceLink[]>;
}) {
  const [width, setWidth] = useState(280);
  const layout = projectDependencyLayout(nodes, edges, width, evidenceLinks.size ? 116 : NODE_HEIGHT);
  if (!layout) return <Text style={{ color: COLORS.warning }}>Les dépendances reçues sont incohérentes. Actualisez le parcours.</Text>;
  const { positions, routes, height } = layout;
  const drawnRoutes = edges.length > 12 && selectedNodeId
    ? routes.filter(({ edge }) => edge.from_node_id === selectedNodeId || edge.to_node_id === selectedNodeId) : routes;
  return <View style={{ gap: 8 }}>
    <Text style={{ color: COLORS.subtle, fontSize: 12, lineHeight: 18 }}>Flèche pleine : requise · pointillée : facultative.</Text>
    <View onLayout={(event) => setWidth(Math.max(160, event.nativeEvent.layout.width))} style={{ height, width: "100%" }} testID="project-dependency-map">
      <Svg width={width} height={height} style={{ position: "absolute" }} {...(Platform.OS === "web" ? { "aria-hidden": true } : { accessible: false })}>
        <Defs><Marker id="dependency-arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><Path d="M0,0 L6,3 L0,6" fill="none" stroke={COLORS.muted} strokeWidth="1.5" /></Marker></Defs>
        {drawnRoutes.map(({ edge, points, path }) => {
          const focused = edge.from_node_id === selectedNodeId || edge.to_node_id === selectedNodeId;
          const color = focused ? COLORS.accent : COLORS.border;
          return <G key={JSON.stringify([edge.from_node_id, edge.to_node_id])}>
            <Path d={path} fill="none" stroke={color} strokeWidth={focused ? 2 : 1.5}
              strokeDasharray={edge.dependency_type === "optional" ? "4 4" : undefined} markerEnd="url(#dependency-arrow)" />
            <Circle cx={points[0].x} cy={points[0].y} r={2} fill={color} />
          </G>;
        })}
      </Svg>
      {nodes.map((node) => {
        const position = positions.get(node.id)!; const selected = selectedNodeId === node.id;
        const links = evidenceLinks.get(node.id) ?? [];
        const evidenceLabel = links.length ? evidenceNodeLabel(links) : null;
        const parents = edges.filter((edge) => edge.to_node_id === node.id).map((edge) => `${edge.dependency_type === "optional" ? "apport facultatif" : "après"} ${nodes.find((item) => item.id === edge.from_node_id)?.title}`).join(" ; ");
        return <Pressable key={node.id} accessibilityRole="button" accessibilityLabel={`Étape : ${node.title}`}
          accessibilityHint={[parents || "Aucune dépendance entrante enregistrée", evidenceLabel, links.some((link) => link.status === "stale") ? "Liens à revoir" : null].filter(Boolean).join(". ")} accessibilityState={{ selected }}
          onPress={() => onSelectNode(node.id)} style={({ pressed }) => ({ position: "absolute", left: position.x, top: position.y,
            width: position.width, height: position.height, backgroundColor: selected ? COLORS.panelRaised : COLORS.background,
            borderWidth: 1, borderColor: selected ? COLORS.accent : COLORS.border, borderRadius: 12, padding: 10, gap: 5, opacity: pressed ? 0.7 : 1 })}>
          <Text style={{ color: nodeColor(node.status), fontSize: 11, fontWeight: "600" }}>{NODE_LABELS[node.status]}</Text>
          <Text numberOfLines={3} style={{ color: COLORS.text, fontSize: 13, lineHeight: 17, fontWeight: "600" }}>{node.title}</Text>
          {evidenceLabel ? <Text numberOfLines={1} style={{ color: links.some((link) => link.status === "stale") ? COLORS.warning : COLORS.muted, fontSize: 11 }}>{evidenceLabel}</Text> : null}
        </Pressable>;
      })}
    </View>
    <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Les étapes d’un même niveau n’ont pas de dépendance entre elles. La simultanéité dépend des ressources.</Text>
    {edges.length > 12 && selectedNodeId ? <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Liens de l’étape sélectionnée affichés. Effacez la sélection pour voir toutes les connexions.</Text> : null}
  </View>;
}

export function ProjectGraphPlan({ state, fallbackNodes, fallbackSummary, enabled, selectedNodeId, onSelectNode, evidence = null, evidenceStale = false }: {
  state: ProjectGraphState; fallbackNodes: PlanNode[]; fallbackSummary?: string | null; enabled: boolean;
  selectedNodeId: string | null; onSelectNode: (id: string | null) => void;
  evidence?: ProjectEvidenceView | null; evidenceStale?: boolean;
}) {
  const [history, setHistory] = useState(false);
  const { graph, stale, busy, error } = state;
  const nodes = graph?.nodes ?? fallbackNodes;
  const evaluation = graph?.evaluations[0];
  const planning = graph?.planning_decisions[0];
  const [attribution, setAttribution] = useState(false);
  const [fullExplanation, setFullExplanation] = useState(false);
  const [showEvaluation, setShowEvaluation] = useState(false);
  const evidenceLinks = nodeEvidenceLinks(graph, evidence, stale || evidenceStale);
  return (
    <Card testID="project-graph-plan">
      <View style={{ gap: 10 }}>
        <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 17, fontWeight: "700" }}>Parcours du projet</Text>
        <Text style={{ color: COLORS.muted, fontSize: 13, lineHeight: 19 }}>Choisissez une étape pour voir son objectif et ses résultats.</Text>
        {nodes.length === 0 ? <Text style={{ color: COLORS.muted }}>Aucune étape enregistrée pour l’instant.</Text> :
          graph ? <DependencyMap nodes={nodes} edges={graph.dependencies} selectedNodeId={selectedNodeId} onSelectNode={onSelectNode} evidenceLinks={evidenceLinks} /> : <View style={{ gap: 8 }}>
            <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Le type des dépendances n’est pas disponible dans ce relevé. Seules les étapes sont présentées.</Text>
            {nodes.map((node) => <Pressable key={node.id} accessibilityRole="button" accessibilityLabel={`Étape : ${node.title}`} accessibilityState={{ selected: selectedNodeId === node.id }}
              onPress={() => onSelectNode(node.id)} style={{ minHeight: 52, padding: 12, borderLeftWidth: 3, borderColor: nodeColor(node.status), backgroundColor: COLORS.background, borderRadius: 10, gap: 4 }}>
              <Text style={{ color: nodeColor(node.status), fontSize: 12 }}>{NODE_LABELS[node.status]}</Text><Text style={{ color: COLORS.text }}>{node.title}</Text>
            </Pressable>)}
          </View>}
        {selectedNodeId && graph ? graph.dependencies.filter((edge) => edge.to_node_id === selectedNodeId).map((edge) => <Text key={edge.from_node_id} style={{ color: COLORS.muted, fontSize: 13 }}>
          {edge.dependency_type === "optional" ? "Apport facultatif" : "Après"} : {nodes.find((node) => node.id === edge.from_node_id)?.title}
        </Text>) : null}
      </View>
      <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 17, fontWeight: "700" }}>Comprendre les décisions</Text>
      {planning ? <View style={{ gap: 8 }}>
        <Text style={{ color: COLORS.text, fontWeight: "600" }}>Pourquoi ce plan a été proposé</Text>
        <Text selectable numberOfLines={fullExplanation ? undefined : 3} style={{ color: COLORS.text, lineHeight: 21 }}>{planning.rationale_summary}</Text>
        <Pressable accessibilityRole="button" accessibilityState={{ expanded: fullExplanation }} onPress={() => setFullExplanation(!fullExplanation)} style={{ minHeight: 44, justifyContent: "center" }}><Text style={{ color: COLORS.accent, fontSize: 13 }}>{fullExplanation ? "Réduire l’explication" : "Lire l’explication complète"}</Text></Pressable>
        <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Explication publique du planificateur · {stamp(planning.created_at)}. Elle concerne la proposition de {planning.node_ids.length} étapes, pas une preuve d’exécution.</Text>
        <Pressable accessibilityRole="button" accessibilityState={{ expanded: attribution }} onPress={() => setAttribution(!attribution)} style={{ minHeight: 44, justifyContent: "center" }}>
          <Text style={{ color: COLORS.accent, fontSize: 13 }}>Source de l’explication {attribution ? "−" : "+"}</Text>
        </Pressable>
        {selectedNodeId && !planning.node_ids.includes(selectedNodeId) ? <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Cette explication ne porte pas sur l’étape sélectionnée.</Text> : null}
        {attribution ? <Text selectable style={{ color: COLORS.muted, fontSize: 12 }}>Planificateur : {planning.planner_source} · Modèle : {planning.model_id ?? "non renseigné"} · Révision de conversation : {planning.conversation_revision}</Text> : null}
      </View> : <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Aucune explication initiale du plan n’a été enregistrée.</Text>}
      {evaluation ? <>
        <Pressable accessibilityRole="button" accessibilityState={{ expanded: showEvaluation }} onPress={() => setShowEvaluation(!showEvaluation)} style={{ minHeight: 44, justifyContent: "center" }}><Text style={{ color: COLORS.accent, fontSize: 13 }}>Dernière évaluation : {EVALUATION_LABELS[evaluation.status]} {showEvaluation ? "−" : "+"}</Text></Pressable>
        {showEvaluation ? <Decision value={evaluation} /> : null}
        <Text style={{ color: COLORS.subtle, fontSize: 12, lineHeight: 18 }}>Explication enregistrée du modèle, distincte des fichiers et contrôles vérifiés.</Text>
      </> : fallbackSummary ? <>
        <Text selectable style={{ color: COLORS.text, lineHeight: 21 }}>{fallbackSummary}</Text>
        <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Résumé enregistré du projet ; origine et détails indisponibles dans ce relevé.</Text>
      </> : <Text style={{ color: COLORS.muted, lineHeight: 20 }}>Aucune explication d’évaluation enregistrée pour l’instant.</Text>}
      {(graph?.evaluations.length ?? 0) > 1 ? <>
        <ActionButton label={history ? "Masquer les décisions précédentes" : "Décisions précédentes"} onPress={() => setHistory((value) => !value)} />
        {history ? graph!.evaluations.slice(1).map((value) => <View key={value.id} style={{ borderTopWidth: 1, borderColor: COLORS.border, paddingTop: 12 }}><Decision value={value} /></View>) : null}
      </> : null}
      {graph?.evaluations_has_more ? <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Seules les cinq dernières évaluations sont présentées.</Text> : null}
      {selectedNodeId ? <ActionButton label="Effacer la sélection" onPress={() => onSelectNode(null)} /> : null}
      {graph ? <Text style={{ color: stale ? COLORS.warning : COLORS.subtle, fontSize: 12 }}>Relevé du {stamp(graph.observed_at)}{stale ? " · état actuel non confirmé" : ""}</Text> : null}
      <ErrorBanner message={error} />
      {busy && !graph ? <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Chargement du parcours enregistré…</Text> : null}
      {error || stale ? <ActionButton label="Actualiser le parcours" onPress={() => void state.refresh(true)} disabled={!enabled || busy} busy={busy} /> : null}
    </Card>
  );
}

export function ProjectGraphEvidence({ graph, stale, showCriteria = true }: { graph: ProjectGraph | null; stale: boolean; showCriteria?: boolean }) {
  const [details, setDetails] = useState(false);
  if (!graph) return <Text style={{ color: COLORS.muted }}>Aucun relevé détaillé de résultats chargé.</Text>;
  const revision = graph.latest_revision;
  return (
    <Card testID="project-graph-evidence">
      <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 17, fontWeight: "700" }}>Fichiers et contrôles enregistrés</Text>
      {stale ? <Text style={{ color: COLORS.warning }}>Dernier relevé conservé ; actualisez le projet pour confirmer son état.</Text> : null}
      {revision ? <>
        <Text style={{ color: COLORS.text }}>Révision {revision.revision} · {revision.files.length} fichiers · {revision.checks.length} contrôles</Text>
        {revision.goal_run_id !== graph.goal.id ? <Text style={{ color: COLORS.warning, lineHeight: 20 }}>Cette révision vient d’une étape antérieure du projet ; elle n’a pas été produite par ce but.</Text> : null}
        <Text style={{ color: COLORS.subtle, fontSize: 12, lineHeight: 18 }}>Les contrôles sont rattachés à cette révision. Leur actualité et la couverture des exigences ne sont pas établies par le seul état des étapes.</Text>
        {revision.checks.map((check) => <View key={check.id} style={{ gap: 4, paddingVertical: 6 }}>
          <Text style={{ color: check.status === "passed" ? COLORS.accent : check.status === "failed" ? COLORS.danger : COLORS.muted }}>
            Contrôle {check.index + 1} · {check.status === "passed" ? "Réussi" : check.status === "failed" ? "Échoué" : "Ignoré"}
          </Text>
          {check.command ? <Text selectable style={{ color: COLORS.muted, fontSize: 12 }}>{check.command.join(" ")}</Text> : null}
          {details ? <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Sortie : {check.exit_code ?? "non enregistrée"} · {check.duration_ms} ms</Text> : null}
        </View>)}
        <ActionButton label={details ? "Masquer les fichiers et empreintes" : "Voir les fichiers et empreintes"} onPress={() => setDetails((value) => !value)} />
        {details ? <View style={{ gap: 8 }}>
          <Text selectable style={{ color: COLORS.subtle, fontSize: 12 }}>Révision : {revision.id} · SHA-256 : {revision.sha256}</Text>
          {revision.files.map((file) => <Text key={file.id} selectable style={{ color: COLORS.muted, fontSize: 13 }}>{file.path} · {file.bytes} octets</Text>)}
        </View> : null}
      </> : <Text style={{ color: COLORS.muted }}>Aucune révision de fichiers enregistrée pour ce projet.</Text>}
      {showCriteria && graph.criteria.length ? <View style={{ gap: 8, borderTopWidth: 1, borderColor: COLORS.border, paddingTop: 12 }}>
        <Text style={{ color: COLORS.text, fontWeight: "600" }}>Exigences du projet</Text>
        <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Consultez « Exigences et preuves » pour les liens enregistrés et les revues explicites.</Text>
        {graph.criteria.map((criterion) => <Text key={criterion.id} selectable style={{ color: COLORS.muted }}>{criterion.index + 1}. {criterion.text}</Text>)}
      </View> : null}
    </Card>
  );
}
