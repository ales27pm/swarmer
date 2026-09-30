import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Pressable, Text, View } from "react-native";
import { ActionButton, Card, COLORS, ErrorBanner } from "@/components/swarm-ui";
import { ApiError, getGoalMemoryUsage, type MemoryUsageEntry, type MemoryUsageItem, type MemoryUsagePage } from "@/lib/application-api/server";
import { subscribeConnectionChanges } from "@/lib/connection-events";

const STAGES = { retrieved: "Souvenirs récupérés", attached_to_model_call: "Joints à un appel enregistré", included_in_worker_job: "Inclus dans un travail d’agent" };
const MODES = { lexical: "lexical", semantic: "sémantique", hybrid: "hybride", unknown: "non enregistré" };
const VERIFICATIONS = { user_asserted: "Déclaration de l’utilisateur", assistant_claim: "Déclaration de l’assistant", recorded_outcome: "Résultat d’opération enregistré", unknown: "Auteur ou preuve non établi" };
const SOURCE_STATES = { available: "Version de source confirmée", changed: "Source modifiée", missing: "Source supprimée ou introuvable", redacted: "Source masquée", unknown: "Version de source non confirmée" };
function date(value: string): string { return new Date(value).toLocaleString("fr-CA"); }

function MemoryExcerpt({ item }: { item: MemoryUsageItem }) {
  const [expanded, setExpanded] = useState(false);
  const long = item.summary !== null && Array.from(item.summary).length > 280;
  return <View style={{ gap: 6, borderLeftWidth: 2, borderColor: COLORS.border, paddingLeft: 10 }}>
    <Text style={{ color: COLORS.accent, fontSize: 12 }}>{item.scope === "general" ? "Mémoire générale" : "Mémoire du projet"} · {VERIFICATIONS[item.verification]}</Text>
    {item.summary !== null ? <Text selectable style={{ color: COLORS.text, lineHeight: 21 }}>{expanded || !long ? item.summary : `${Array.from(item.summary).slice(0, 280).join("")}…`}</Text>
      : <Text style={{ color: COLORS.muted }}>L’extrait n’est pas consultable depuis ce reçu.</Text>}
    {long ? <Pressable accessibilityRole="button" accessibilityLabel={`Extrait mémoire ${item.id}`} accessibilityState={{ expanded }} onPress={() => setExpanded(!expanded)} style={{ minHeight: 44, justifyContent: "center" }}>
      <Text style={{ color: COLORS.accent }}>{expanded ? "Réduire l’extrait" : "Lire l’extrait complet"}</Text>
    </Pressable> : null}
    <Text style={{ color: COLORS.subtle, fontSize: 12 }}>{SOURCE_STATES[item.source_state]}</Text>
    {item.source_at ? <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Source enregistrée : {date(item.source_at)}</Text> : null}
  </View>;
}
function Receipt({ entry }: { entry: MemoryUsageEntry }) {
  const [details, setDetails] = useState(false);
  return <View style={{ gap: 10, paddingVertical: 14, borderTopWidth: 1, borderColor: COLORS.border }}>
    <Text accessibilityRole="header" style={{ color: COLORS.text, fontWeight: "700", fontSize: 16 }}>{STAGES[entry.evidence_stage]}</Text>
    <Text style={{ color: COLORS.muted, fontSize: 12 }}>Reçu enregistré : {date(entry.recorded_at)}</Text>
    <Text style={{ color: COLORS.muted }}>Classement : {MODES[entry.retrieval.mode]} · {entry.items.length} référence{entry.items.length > 1 ? "s" : ""} affichée{entry.items.length > 1 ? "s" : ""}</Text>
    <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Révision des consignes dans ce reçu : {entry.conversation_revision ?? "non enregistrée"}</Text>
    {entry.items.map(item => <MemoryExcerpt key={item.id} item={item} />)}
    {!entry.items.length ? <Text style={{ color: COLORS.muted }}>Ce reçu ne contient aucun extrait actuellement consultable.</Text> : null}
    {entry.omitted_item_count > 0 ? <Text style={{ color: COLORS.warning, fontSize: 12 }}>{entry.omitted_item_count} référence{entry.omitted_item_count > 1 ? "s" : ""} omise{entry.omitted_item_count > 1 ? "s" : ""} : portée, preuve ou limite d’affichage.</Text> : null}
    <Pressable accessibilityRole="button" accessibilityLabel={`Diagnostic du reçu ${entry.id}`} accessibilityState={{ expanded: details }} onPress={() => setDetails(!details)} style={{ minHeight: 44, justifyContent: "center" }}>
      <Text style={{ color: COLORS.accent }}>{details ? "Réduire le diagnostic −" : "Diagnostic du reçu +"}</Text>
    </Pressable>
    {details ? <View style={{ gap: 6, backgroundColor: COLORS.background, padding: 12, borderRadius: 10 }}>
      {[["Reçu", entry.id], ["État enregistré", entry.status], ["Rôle ou compétence", entry.purpose], ["Modèle", entry.model_id], ["Tâche", entry.task_id], ["Étape", entry.node_id], ["Appel modèle", entry.model_call_id], ["Travail d’agent", entry.worker_job_id], ["Contexte", entry.context_id], ["Mode : code enregistré", entry.retrieval.reason], ["Empreinte du fournisseur", entry.retrieval.provider_fingerprint]].filter(([, value]) => value !== null).map(([label, value]) => <Text key={label} selectable style={{ color: COLORS.muted, fontSize: 12 }}>{label} : {value}</Text>)}
      {entry.completed_at ? <Text style={{ color: COLORS.muted, fontSize: 12 }}>Fin enregistrée : {date(entry.completed_at)}</Text> : null}
      {entry.items.map(item => <Text key={item.id} selectable style={{ color: COLORS.muted, fontSize: 12 }}>Élément : {item.id} · Source : {item.source_id ?? "non établie"} · Type : {item.source_kind}{item.source_goal_id ? ` · But : ${item.source_goal_id}` : ""}{item.source_revision_id ? ` · Révision : ${item.source_revision_id}` : ""}</Text>)}
    </View> : null}
  </View>;
}

export function MemoryUsage({ goalId, enabled }: { goalId: string; enabled: boolean }) {
  const [open, setOpen] = useState(false);
  return <Card>
    <Pressable accessibilityRole="button" accessibilityLabel="Mémoire utilisée" accessibilityState={{ expanded: open }} onPress={() => setOpen(!open)} style={{ minHeight: 48, justifyContent: "center" }}>
      <Text style={{ color: COLORS.text, fontSize: 17, fontWeight: "700" }}>Mémoire utilisée {open ? "−" : "+"}</Text>
    </Pressable>
    <Text style={{ color: COLORS.muted, lineHeight: 20 }}>Consulter les reçus de récupération et de transmission enregistrés pour ce projet.</Text>
    {open ? <MemoryUsageSession key={goalId} goalId={goalId} enabled={enabled} /> : null}
  </Card>;
}
function MemoryUsageSession({ goalId, enabled }: { goalId: string; enabled: boolean }) {
  const [page, setPage] = useState<MemoryUsagePage | null>(null);
  const [receivedAt, setReceivedAt] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const [resetRequired, setResetRequired] = useState(false);
  const epoch = useRef(0), busyRef = useRef(false), active = useRef(AppState.currentState === "active"), enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const load = useCallback(async (cursor?: string) => {
    if (!enabledRef.current || !active.current || busyRef.current) return;
    const request = ++epoch.current;
    const current = () => epoch.current === request && enabledRef.current && active.current;
    busyRef.current = true; setBusy(true); setError(null);
    try {
      const next = await getGoalMemoryUsage(goalId, cursor, current);
      if (!current()) return;
      setPage(previous => cursor && previous ? { ...next, entries: [...new Map([...previous.entries, ...next.entries].map(entry => [entry.id, entry])).values()] } : next);
      setReceivedAt(new Date().toISOString()); setStale(false); setResetRequired(false);
    } catch (cause) {
      if (!current()) return;
      setStale(true);
      const reset = cause instanceof ApiError && cause.status === 400;
      setResetRequired(reset);
      setError(reset ? "L’historique a changé. Actualisez les reçus depuis le début."
        : cause instanceof ApiError && cause.status === 404 ? "Les reçus mémoire ne sont pas disponibles pour ce projet sur ce serveur."
          : "Les reçus mémoire n’ont pas pu être chargés. Actualisez pour réessayer.");
    } finally {
      if (epoch.current === request) { busyRef.current = false; setBusy(false); }
    }
  }, [goalId]);
  useEffect(() => {
    void load();
    const pairing = subscribeConnectionChanges(() => {
      epoch.current++; busyRef.current = false; setBusy(false); setPage(null); setReceivedAt(null); setStale(true); setResetRequired(true);
      setError("Le jumelage a changé. Actualisez explicitement les reçus depuis cette connexion.");
    });
    const lifecycle = AppState.addEventListener("change", state => {
      active.current = state === "active";
      if (!active.current) { epoch.current++; busyRef.current = false; setBusy(false); setStale(true); }
    });
    const requestEpoch = epoch;
    return () => { requestEpoch.current++; pairing(); lifecycle.remove(); };
  }, [load]);
  useEffect(() => { if (!enabled) { epoch.current++; busyRef.current = false; setBusy(false); setStale(true); } }, [enabled]);
  const retained = stale || !enabled;
  return <View style={{ gap: 12 }}>
    <Text style={{ color: COLORS.subtle, lineHeight: 19 }}>Ces reçus prouvent une récupération ou une présence dans une requête enregistrée, pas que le modèle a lu, compris ou correctement utilisé ces éléments. Cette consultation ne lance aucune recherche ni indexation.</Text>
    <ActionButton label="Actualiser les reçus mémoire" onPress={() => void load()} disabled={!enabled || busy} busy={busy} />
    <ErrorBanner message={error} />
    {busy && !page ? <Text style={{ color: COLORS.muted }}>Chargement des reçus enregistrés…</Text> : null}
    {!enabled ? <Text style={{ color: COLORS.warning }}>Connexion requise pour consulter les reçus.</Text> : null}
    {page ? <>
      <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Dernière page servie par le serveur : {date(page.observed_at)}</Text>
      {receivedAt ? <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Dernière page reçue sur cet appareil : {date(receivedAt)}</Text> : null}
      <Text style={{ color: COLORS.muted, fontSize: 12 }}>Révision actuelle des consignes : {page.current_conversation_revision}</Text>
      {retained ? <Text style={{ color: COLORS.warning }}>Dernier relevé conservé ; les sources et leur état actuel ne sont pas confirmés.</Text> : null}
      {page.project_id === null ? <Text style={{ color: COLORS.subtle }}>Aucun rattachement de projet enregistré pour ce but.</Text> : null}
      {page.availability === "no_records" && !page.entries.length ? <Text style={{ color: COLORS.muted }}>Aucun reçu mémoire consultable. Cela ne prouve pas qu’aucune mémoire n’a été utilisée dans l’historique.</Text> : null}
      {page.entries.map(entry => <Receipt key={entry.id} entry={entry} />)}
      {page.next_cursor && page.entries.length < 200 ? <ActionButton label="Voir les reçus précédents" onPress={() => void load(page.next_cursor!)} disabled={!enabled || busy || resetRequired} /> : null}
      {page.next_cursor && page.entries.length >= 200 ? <Text style={{ color: COLORS.subtle }}>200 reçus chargés. Actualisez pour revenir au relevé le plus récent.</Text> : null}
      <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Historique limité aux reçus conservés ; chaque page est contrôlée au moment de sa lecture. Les extraits citent ces reçus ; une source actuelle ne remplace jamais le texte historique.</Text>
    </> : null}
  </View>;
}
