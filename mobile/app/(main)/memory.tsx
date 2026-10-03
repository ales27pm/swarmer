import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, AppState, Text, TextInput, View } from "react-native";

import { ScreenShell } from "@/components/screen-shell";
import { SymbolicMemoryEvidence } from "@/components/symbolic-memory-evidence";
import { SymbolicMemorySearchControls, type SymbolicSearchSelection } from "@/components/symbolic-memory-search-controls";
import { memorySearchRequest } from "@/lib/api/memory-search";
import {
  ActionButton,
  Card,
  COLORS,
  EmptyState,
  ErrorBanner,
  SectionTitle,
  timeAgo,
  useAccessibilityAnnouncement,
} from "@/components/swarm-ui";
import {
  deleteMemory,
  listMemory,
  rememberMemory,
  searchMemory,
  updateMemory,
  type MemoryItem,
} from "@/lib/application-api/server";
import { useLiveRefresh, useLiveSync } from "@/lib/sync/live-sync-context";
import { subscribeConnectionChanges } from "@/lib/connection-events";
import { memoryPresentation } from "@/lib/api/memory-presentation";

function memoryIdentity(item: MemoryItem): string {
  const display = memoryPresentation(item) ?? item;
  const value = display.summary?.trim() || display.content.trim();
  return value.length > 60 ? `${value.slice(0, 57)}…` : value;
}

function MemorySearchControls({
  query,
  searching,
  onChangeQuery,
  onClear,
  onSearch,
  mode,
  loadedQuery,
}: {
  query: string;
  searching: boolean;
  onChangeQuery: (value: string) => void;
  onClear: () => void;
  onSearch: () => void;
  mode: MemoryItem["search_kind"] | "mixed" | null;
  loadedQuery: string | null;
}) {
  return (
    <>
      <Text style={{ color: COLORS.muted, fontSize: 12, fontWeight: "700" }}>
        Recherche dans les éléments enregistrés
      </Text>
      <View style={{ flexDirection: "row", gap: 8 }}>
        <TextInput
          accessibilityLabel="Rechercher dans la mémoire"
          autoCapitalize="none"
          onChangeText={onChangeQuery}
          onSubmitEditing={onSearch}
          placeholder="Mots-clés…"
          placeholderTextColor={COLORS.subtle}
          returnKeyType="search"
          style={{
            backgroundColor: COLORS.panel,
            borderColor: COLORS.border,
            borderRadius: 12,
            borderWidth: 1,
            color: COLORS.text,
            flex: 1,
            minHeight: 46,
            paddingHorizontal: 13,
          }}
          testID="memory-search-input"
          value={query}
        />
        <ActionButton
          busy={searching}
          label="Chercher"
          onPress={onSearch}
          testID="memory-search-button"
        />
      </View>
      <Text style={{ color: COLORS.subtle, fontSize: 11 }}>
        {loadedQuery === null ? "Le mode de recherche sera indiqué d’après les résultats reçus."
          : !loadedQuery ? "Ce catalogue ne couvre pas toutes les traces de projets ni les épisodes."
            : mode === "hybrid" ? "Dernière recherche : classement hybride déclaré par le serveur."
              : mode === "vector" ? "Dernière recherche : classement vectoriel déclaré par le serveur."
                : mode === "lexical" ? "Dernière recherche : classement lexical déclaré par le serveur."
                  : mode === "symbolic" ? "Dernière recherche : classement symbolique déclaré par le serveur."
                  : mode === "mixed" ? "Dernière recherche : modes différents selon les résultats."
                    : "Dernière recherche : mode non renseigné dans les résultats reçus."}
      </Text>
      {query.trim() ? (
        <ActionButton
          label="Effacer la recherche"
          onPress={onClear}
          testID="clear-memory-search"
        />
      ) : null}
    </>
  );
}

function MemoryComposer({
  draft,
  mutating,
  showAdd,
  onChangeDraft,
  onRemember,
  onToggle,
}: {
  draft: string;
  mutating: string | null;
  showAdd: boolean;
  onChangeDraft: (value: string) => void;
  onRemember: () => void;
  onToggle: () => void;
}) {
  return (
    <>
      <ActionButton
        label={showAdd ? "Fermer" : "Ajouter une mémoire"}
        onPress={onToggle}
        testID="add-memory-button"
      />
      {showAdd ? (
        <Card>
          <SectionTitle title="Nouvelle mémoire" />
          <Text style={{ color: COLORS.muted, fontSize: 12, fontWeight: "700" }}>
            Contenu à mémoriser
          </Text>
          <TextInput
            accessibilityLabel="Contenu à mémoriser"
            multiline
            onChangeText={onChangeDraft}
            placeholder="Quelque chose à retenir…"
            placeholderTextColor={COLORS.subtle}
            style={{
              backgroundColor: COLORS.background,
              borderColor: COLORS.border,
              borderRadius: 12,
              borderWidth: 1,
              color: COLORS.text,
              minHeight: 84,
              padding: 12,
              textAlignVertical: "top",
            }}
            testID="remember-input"
            value={draft}
          />
          <ActionButton
            busy={mutating === "new"}
            disabled={!draft.trim() || Boolean(mutating)}
            label="Mémoriser"
            onPress={onRemember}
            testID="remember-submit"
            variant="accent"
          />
        </Card>
      ) : null}
    </>
  );
}

function MemoryRecordCard({
  item,
  mutating,
  onDelete,
  onTogglePinned,
}: {
  item: MemoryItem;
  mutating: string | null;
  onDelete: (item: MemoryItem) => void;
  onTogglePinned: (item: MemoryItem) => void;
}) {
  const identity = memoryIdentity(item);
  const presentation = memoryPresentation(item);
  const [showCanonical, setShowCanonical] = useState(false);
  const display = showCanonical || !presentation ? item : presentation;
  return (
    <Card testID={`memory-card-${item.id}`}>
      <View style={{ alignItems: "center", flexDirection: "row", gap: 8 }}>
        <Text style={{ color: COLORS.accent, fontSize: 11, fontWeight: "800", textTransform: "uppercase" }}>
          {item.scope}
        </Text>
        <Text style={{ color: COLORS.subtle, fontSize: 11 }}>{item.kind}</Text>
        <Text style={{ color: COLORS.subtle, flex: 1, fontSize: 11, textAlign: "right" }}>
          {item.pinned ? "Épinglée · " : ""}{timeAgo(item.updated_at)}
        </Text>
      </View>
      {display.summary ? (
        <Text selectable style={{ color: COLORS.text, fontWeight: "700" }}>{display.summary}</Text>
      ) : null}
      <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>{display.content}</Text>
      {presentation ? (
        <>
          <Text style={{ color: COLORS.subtle, fontSize: 11 }}>
            {showCanonical ? "Version anglaise enregistrée." : presentation.mode === "original"
              ? "Texte français d’origine · version anglaise disponible."
              : "Traduction française temporaire · version enregistrée en anglais."}
          </Text>
          <ActionButton
            label={showCanonical ? "Revenir au français" : "Voir la version anglaise"}
            onPress={() => setShowCanonical((value) => !value)}
            testID={`canonical-memory-${item.id}`}
          />
        </>
      ) : null}
      <SymbolicMemoryEvidence item={item} />
      <View style={{ flexDirection: "row", gap: 8 }}>
        <ActionButton
          accessibilityHint="Modifie l’état épinglé de cette mémoire uniquement."
          accessibilityLabel={`${item.pinned ? "Désépingler" : "Épingler"} : ${identity}`}
          busy={mutating === `pin:${item.id}`}
          disabled={Boolean(mutating)}
          label={item.pinned ? "Désépingler" : "Épingler"}
          onPress={() => onTogglePinned(item)}
          style={{ flex: 1 }}
          testID={`pin-memory-${item.id}`}
        />
        <ActionButton
          accessibilityHint="Ouvre une confirmation avant de supprimer cette mémoire."
          accessibilityLabel={`Supprimer : ${identity}`}
          busy={mutating === `delete:${item.id}`}
          disabled={Boolean(mutating)}
          label="Supprimer"
          onPress={() => onDelete(item)}
          style={{ flex: 1 }}
          testID={`delete-memory-${item.id}`}
          variant="danger"
        />
      </View>
    </Card>
  );
}

function MemoryResults({
  error,
  items,
  mutating,
  query,
  refreshing,
  searching,
  onDelete,
  onTogglePinned,
}: {
  error: string | null;
  items: MemoryItem[];
  mutating: string | null;
  query: string;
  refreshing: boolean;
  searching: boolean;
  onDelete: (item: MemoryItem) => void;
  onTogglePinned: (item: MemoryItem) => void;
}) {
  const hasQuery = Boolean(query.trim());
  const showEmpty = !items.length && !refreshing && !searching && !error;
  return (
    <>
      {showEmpty ? (
        <EmptyState
          title={hasQuery ? "Aucun résultat dans ce catalogue" : "Aucun élément dans ce catalogue"}
          subtitle={hasQuery ? "La dernière recherche n’a renvoyé aucun élément. Son mode n’est pas connu sans reçu." : "Les traces de projets et les épisodes peuvent exister en dehors de ce catalogue."}
        />
      ) : null}
      <View style={{ gap: 12 }} testID="memory-list">
        {items.map((item) => (
          <MemoryRecordCard
            item={item}
            key={item.id}
            mutating={mutating}
            onDelete={onDelete}
            onTogglePinned={onTogglePinned}
          />
        ))}
      </View>
    </>
  );
}

export default function MemoryScreen() {
  const { state: liveState } = useLiveSync();
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [query, setQuery] = useState("");
  const [symbolicSelection, setSymbolicSelection] = useState<SymbolicSearchSelection | null>(null);
  const [draft, setDraft] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [searching, setSearching] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [mutating, setMutating] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [receivedAt, setReceivedAt] = useState<string | null>(null);
  const [loadedQuery, setLoadedQuery] = useState<string | null>(null);
  const [stale, setStale] = useState(true);
  const [connectionBlocked, setConnectionBlocked] = useState(false);
  const searchSequence = useRef(0);
  const connectionEpoch = useRef(0);
  const blocked = useRef(false);
  const mounted = useRef(true);
  const active = useRef(AppState.currentState === "active");
  const queryRef = useRef("");
  const symbolicRef = useRef<SymbolicSearchSelection | null>(null);
  useAccessibilityAnnouncement(notice);

  function updateQuery(value: string) {
    searchSequence.current += 1;
    setSearching(false); setRefreshing(false); setStale(true);
    queryRef.current = value;
    setQuery(value);
  }

  function updateSymbolic(value: SymbolicSearchSelection | null) {
    symbolicRef.current = value; setSymbolicSelection(value);
    // A changed catalog invalidates an in-flight result even if its query is unchanged.
    updateQuery(queryRef.current);
  }

  const refresh = useCallback(async (explicit = false) => {
    if (!mounted.current || !active.current || (blocked.current && !explicit)) return;
    if (explicit) { blocked.current = false; setConnectionBlocked(false); }
    const sequence = ++searchSequence.current;
    const connection = connectionEpoch.current;
    setRefreshing(true);
    const activeQuery = queryRef.current.trim();
    const accepts = () => mounted.current && active.current && !blocked.current
      && sequence === searchSequence.current && connection === connectionEpoch.current
      && activeQuery === queryRef.current.trim();
    setSearching(Boolean(activeQuery)); setStale(true); setError(null);
    try {
      const selected = symbolicRef.current;
      const options = selected ? { scope: selected.scope, symbolic: {
        catalogs: [{ namespace: selected.namespace, scheme_id: selected.scheme_id }],
      } } : undefined;
      if (activeQuery) memorySearchRequest(activeQuery, options);
      const result = await (activeQuery ? options ? searchMemory(activeQuery, options) : searchMemory(activeQuery) : listMemory());
      if (accepts()) { setItems(result); setLoadedQuery(activeQuery); setReceivedAt(new Date().toISOString()); setStale(false); }
    } catch (cause) {
      if (accepts()) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (accepts()) { setRefreshing(false); setSearching(false); }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const unsubscribe = subscribeConnectionChanges(() => {
      connectionEpoch.current += 1; searchSequence.current += 1; blocked.current = true;
      setConnectionBlocked(true); setItems([]); setLoadedQuery(null); setReceivedAt(null);
      symbolicRef.current = null; setSymbolicSelection(null);
      setRefreshing(false); setSearching(false); setMutating(null); setStale(true); setNotice(null);
      setError("Le jumelage a changé. Actualisez la mémoire depuis cette connexion.");
    });
    const subscription = AppState.addEventListener("change", (state) => {
      active.current = state === "active";
      if (active.current) void refresh();
      else { searchSequence.current += 1; setRefreshing(false); setSearching(false); setStale(true); }
    });
    return () => { mounted.current = false; searchSequence.current += 1; connectionEpoch.current += 1; unsubscribe(); subscription.remove(); };
  }, [refresh]);
  useLiveRefresh(() => refresh());

  async function runSearch() {
    await refresh(true);
  }

  async function remember() {
    const content = draft.trim();
    if (!content || mutating || blocked.current || !active.current) return;
    const connection = connectionEpoch.current;
    const originalDraft = draft;
    setMutating("new");
    setError(null);
    setNotice(null);
    try {
      await rememberMemory({ content });
      if (!mounted.current || connection !== connectionEpoch.current) return;
      setDraft((current) => current === originalDraft ? "" : current);
      updateQuery("");
      setShowAdd(false);
      await refresh();
      if (!mounted.current || connection !== connectionEpoch.current) return;
      setNotice("Mémoire ajoutée au control plane.");
    } catch (cause) {
      if (mounted.current && connection === connectionEpoch.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (mounted.current && connection === connectionEpoch.current) setMutating(null);
    }
  }

  async function togglePinned(item: MemoryItem) {
    if (mutating || blocked.current || !active.current) return;
    const connection = connectionEpoch.current;
    setMutating(`pin:${item.id}`);
    setError(null);
    setNotice(null);
    try {
      await updateMemory(item.id, { pinned: !item.pinned });
      if (!mounted.current || connection !== connectionEpoch.current) return;
      await refresh();
      if (!mounted.current || connection !== connectionEpoch.current) return;
      setNotice(
        item.pinned
          ? `Mémoire « ${memoryIdentity(item)} » désépinglée.`
          : `Mémoire « ${memoryIdentity(item)} » épinglée.`,
      );
    } catch (cause) {
      if (mounted.current && connection === connectionEpoch.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (mounted.current && connection === connectionEpoch.current) setMutating(null);
    }
  }

  function confirmDelete(item: MemoryItem) {
    const connection = connectionEpoch.current;
    const identity = memoryIdentity(item);
    Alert.alert(`Supprimer « ${identity} »?`, "Cette suppression modifie la vérité du control plane.", [
      { text: "Annuler", style: "cancel" },
      {
        text: "Supprimer",
        style: "destructive",
        onPress: () => {
          void (async () => {
            if (mutating || blocked.current || !active.current || !mounted.current || connection !== connectionEpoch.current) return;
            setMutating(`delete:${item.id}`);
            setError(null);
            setNotice(null);
            try {
              await deleteMemory(item.id);
              if (!mounted.current || connection !== connectionEpoch.current) return;
              await refresh();
              if (!mounted.current || connection !== connectionEpoch.current) return;
              setNotice(`Mémoire « ${identity} » supprimée du control plane.`);
            } catch (cause) {
              if (mounted.current && connection === connectionEpoch.current) setError(cause instanceof Error ? cause.message : String(cause));
            } finally {
              if (mounted.current && connection === connectionEpoch.current) setMutating(null);
            }
          })();
        },
      },
    ]);
  }

  const modes = new Set(items.map((item) => item.search_kind));
  const mode = items.length && [...modes].every((value) => value === "lexical" || value === "hybrid" || value === "vector" || value === "symbolic")
    ? modes.size === 1 ? items[0].search_kind! : "mixed" : null;
  return (
    <ScreenShell
      showTitle={false}
      title="Mémoire"
      subtitle="Catalogue des éléments mémorisés et résultats de recherche."
      onRefresh={() => void refresh(true)}
      refreshing={refreshing}
      testID="memory-screen"
    >
      <ActionButton label="Actualiser les éléments mémorisés" onPress={() => void refresh(true)} busy={refreshing} />
      {receivedAt ? <Text style={{ color: stale ? COLORS.warning : COLORS.subtle, fontSize: 12 }}>
        Relevé reçu : {new Date(receivedAt).toLocaleString("fr-CA")}{stale ? " · dernier relevé conservé, non actualisé" : ""}
      </Text> : null}
      {receivedAt && liveState !== "connected" ? <Text style={{ color: COLORS.warning, fontSize: 12 }}>
        Le suivi des changements n’est pas connecté. Actualisez pour obtenir un nouveau relevé.
      </Text> : null}
      <MemorySearchControls
        onChangeQuery={updateQuery}
        onClear={() => {
          updateQuery("");
          void refresh();
        }}
        onSearch={() => void runSearch()}
        query={query}
        searching={searching}
        mode={mode}
        loadedQuery={loadedQuery}
      />
      <SymbolicMemorySearchControls value={symbolicSelection} onChange={updateSymbolic} />
      <MemoryComposer
        draft={draft}
        mutating={connectionBlocked ? "connection" : mutating}
        onChangeDraft={setDraft}
        onRemember={() => void remember()}
        onToggle={() => setShowAdd((value) => !value)}
        showAdd={showAdd}
      />
      <ErrorBanner message={error} />
      {notice ? (
        <Text accessibilityLiveRegion="polite" selectable style={{ color: COLORS.accent }}>
          {notice}
        </Text>
      ) : null}
      <MemoryResults
        error={error}
        items={items}
        mutating={connectionBlocked ? "connection" : mutating}
        onDelete={confirmDelete}
        onTogglePinned={(item) => void togglePinned(item)}
        query={query}
        refreshing={refreshing || stale}
        searching={searching}
      />
    </ScreenShell>
  );
}
