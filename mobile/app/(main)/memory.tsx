import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Text, TextInput, View } from "react-native";

import { ScreenShell } from "@/components/screen-shell";
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
} from "@/lib/api/client";

function memoryIdentity(item: MemoryItem): string {
  const value = item.summary?.trim() || item.content.trim();
  return value.length > 60 ? `${value.slice(0, 57)}…` : value;
}

function MemorySearchControls({
  query,
  searching,
  onChangeQuery,
  onClear,
  onSearch,
}: {
  query: string;
  searching: boolean;
  onChangeQuery: (value: string) => void;
  onClear: () => void;
  onSearch: () => void;
}) {
  return (
    <>
      <Text style={{ color: COLORS.muted, fontSize: 12, fontWeight: "700" }}>
        Recherche lexicale
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
        Classement lexical uniquement — aucun score vectoriel ou sémantique n’est revendiqué.
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
            disabled={!draft.trim()}
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
      {item.summary ? (
        <Text selectable style={{ color: COLORS.text, fontWeight: "700" }}>{item.summary}</Text>
      ) : null}
      <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>{item.content}</Text>
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
          title={hasQuery ? "Aucun résultat" : "Mémoire vide"}
          subtitle={hasQuery ? "Aucun élément ne contient ces mots-clés." : "Ajoute une mémoire durable quand elle doit faire partie du contexte local."}
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
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [searching, setSearching] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [mutating, setMutating] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const searchSequence = useRef(0);
  const queryRef = useRef("");
  useAccessibilityAnnouncement(notice);

  function updateQuery(value: string) {
    queryRef.current = value;
    setQuery(value);
  }

  const refresh = useCallback(() => {
    searchSequence.current += 1;
    setRefreshing(true);
    setSearching(false);
    setError(null);
    const activeQuery = queryRef.current.trim();
    const load = activeQuery ? searchMemory(activeQuery) : listMemory();
    return load
      .then(setItems)
      .catch((cause: unknown) => {
        setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => setRefreshing(false));
  }, []);

  useEffect(() => {
    void (async () => {
      await refresh();
    })();
  }, [refresh]);

  async function runSearch() {
    const value = query.trim();
    if (!value) {
      await refresh();
      return;
    }
    const sequence = ++searchSequence.current;
    setSearching(true);
    setError(null);
    try {
      const results = await searchMemory(value);
      if (searchSequence.current === sequence) setItems(results);
    } catch (cause) {
      if (searchSequence.current === sequence) {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    } finally {
      if (searchSequence.current === sequence) setSearching(false);
    }
  }

  async function remember() {
    const content = draft.trim();
    if (!content || mutating) return;
    setMutating("new");
    setError(null);
    setNotice(null);
    try {
      await rememberMemory({ content });
      setDraft("");
      updateQuery("");
      setShowAdd(false);
      await refresh();
      setNotice("Mémoire ajoutée au control plane.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setMutating(null);
    }
  }

  async function togglePinned(item: MemoryItem) {
    if (mutating) return;
    setMutating(`pin:${item.id}`);
    setError(null);
    setNotice(null);
    try {
      await updateMemory(item.id, { pinned: !item.pinned });
      await refresh();
      setNotice(
        item.pinned
          ? `Mémoire « ${memoryIdentity(item)} » désépinglée.`
          : `Mémoire « ${memoryIdentity(item)} » épinglée.`,
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setMutating(null);
    }
  }

  function confirmDelete(item: MemoryItem) {
    const identity = memoryIdentity(item);
    Alert.alert(`Supprimer « ${identity} »?`, "Cette suppression modifie la vérité du control plane.", [
      { text: "Annuler", style: "cancel" },
      {
        text: "Supprimer",
        style: "destructive",
        onPress: () => {
          void (async () => {
            if (mutating) return;
            setMutating(`delete:${item.id}`);
            setError(null);
            setNotice(null);
            try {
              await deleteMemory(item.id);
              await refresh();
              setNotice(`Mémoire « ${identity} » supprimée du control plane.`);
            } catch (cause) {
              setError(cause instanceof Error ? cause.message : String(cause));
            } finally {
              setMutating(null);
            }
          })();
        },
      },
    ]);
  }

  return (
    <ScreenShell
      title="Mémoire"
      subtitle="Recherche lexicale locale et éléments épinglés du control plane."
      onRefresh={() => void refresh()}
      refreshing={refreshing}
      testID="memory-screen"
    >
      <MemorySearchControls
        onChangeQuery={updateQuery}
        onClear={() => {
          updateQuery("");
          void refresh();
        }}
        onSearch={() => void runSearch()}
        query={query}
        searching={searching}
      />
      <MemoryComposer
        draft={draft}
        mutating={mutating}
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
        mutating={mutating}
        onDelete={confirmDelete}
        onTogglePinned={(item) => void togglePinned(item)}
        query={query}
        refreshing={refreshing}
        searching={searching}
      />
    </ScreenShell>
  );
}
