import React, { useState } from "react";
import { FlatList, Pressable, Text, TextInput, View } from "react-native";
import { Brain, Pin, Plus, Search, Trash2 } from "lucide-react-native";
import { useDeleteMemory, useMemory, useRemember, useSearchMemory, useUpdateMemory } from "@/lib/hooks";
import type { MemoryItem } from "@/lib/types";
import { EmptyState, timeAgo } from "@/lib/ui";
import { cn } from "@/lib/cn";

function MemoryCard({ item }: { item: MemoryItem }) {
  const update = useUpdateMemory();
  const remove = useDeleteMemory();
  return (
    <View className="bg-zinc-900 border border-zinc-800 rounded-2xl p-4 mb-3" testID={`memory-card-${item.id}`}>
      <View className="flex-row items-center gap-2 mb-2">
        <View className="bg-zinc-800 rounded-md px-2 py-0.5">
          <Text className="text-zinc-400 text-[10px] font-semibold uppercase tracking-wide">{item.scope}</Text>
        </View>
        <View className="bg-zinc-800 rounded-md px-2 py-0.5">
          <Text className="text-zinc-500 text-[10px] uppercase tracking-wide">{item.kind}</Text>
        </View>
        <View className="flex-1" />
        {item.pinned ? <Pin size={13} color="#34d399" fill="#34d399" /> : null}
      </View>
      {item.summary ? <Text className="text-zinc-100 font-semibold text-[15px] mb-1">{item.summary}</Text> : null}
      <Text className="text-zinc-300 text-sm leading-5" selectable>
        {item.content}
      </Text>
      <View className="flex-row items-center mt-3 gap-4">
        <Text className="text-zinc-600 text-[11px] flex-1">{timeAgo(item.updated_at)}</Text>
        <Pressable
          testID={`pin-memory-${item.id}`}
          onPress={() => update.mutate({ id: item.id, pinned: !item.pinned })}
          hitSlop={8}
          className="active:opacity-50"
        >
          <Pin size={16} color={item.pinned ? "#34d399" : "#71717a"} />
        </Pressable>
        <Pressable
          testID={`delete-memory-${item.id}`}
          onPress={() => remove.mutate(item.id)}
          hitSlop={8}
          className="active:opacity-50"
        >
          <Trash2 size={16} color="#71717a" />
        </Pressable>
      </View>
    </View>
  );
}

export default function MemoryScreen() {
  const [query, setQuery] = useState<string>("");
  const [draft, setDraft] = useState<string>("");
  const [showAdd, setShowAdd] = useState<boolean>(false);

  const memory = useMemory();
  const search = useSearchMemory();
  const remember = useRemember();

  const searching = query.trim().length > 0;
  const data = searching ? (search.data ?? []) : (memory.data ?? []);

  return (
    <View className="flex-1 bg-zinc-950" testID="memory-screen">
      <View className="px-4 pt-3 pb-2 flex-row gap-2">
        <View className="flex-1 flex-row items-center bg-zinc-900 border border-zinc-800 rounded-xl px-3">
          <Search size={16} color="#71717a" />
          <TextInput
            testID="memory-search-input"
            className="flex-1 text-zinc-100 text-sm px-2 py-2.5"
            placeholder="Recherche sémantique…"
            placeholderTextColor="#52525b"
            value={query}
            onChangeText={(t) => {
              setQuery(t);
              if (t.trim().length > 1) search.mutate(t.trim());
            }}
            returnKeyType="search"
          />
        </View>
        <Pressable
          testID="add-memory-button"
          onPress={() => setShowAdd((v) => !v)}
          className={cn(
            "w-11 rounded-xl items-center justify-center border",
            showAdd ? "bg-emerald-500/15 border-emerald-500/40" : "bg-zinc-900 border-zinc-800"
          )}
        >
          <Plus size={18} color={showAdd ? "#34d399" : "#a1a1aa"} />
        </Pressable>
      </View>

      {showAdd ? (
        <View className="mx-4 mb-2 bg-zinc-900 border border-zinc-800 rounded-2xl p-3">
          <TextInput
            testID="remember-input"
            className="text-zinc-100 text-sm min-h-[60px]"
            placeholder="Quelque chose à retenir…"
            placeholderTextColor="#52525b"
            multiline
            value={draft}
            onChangeText={setDraft}
          />
          <Pressable
            testID="remember-submit"
            onPress={() => {
              if (!draft.trim()) return;
              remember.mutate(
                { content: draft.trim() },
                { onSuccess: () => { setDraft(""); setShowAdd(false); } }
              );
            }}
            className="bg-emerald-500 rounded-xl py-2.5 items-center mt-2 active:bg-emerald-400"
          >
            <Text className="text-zinc-950 font-bold text-sm">Mémoriser</Text>
          </Pressable>
        </View>
      ) : null}

      <FlatList
        testID="memory-list"
        data={data}
        keyExtractor={(m: MemoryItem) => m.id}
        renderItem={({ item }) => <MemoryCard item={item} />}
        contentContainerStyle={{ padding: 16, paddingTop: 8, paddingBottom: 24 }}
        ListEmptyComponent={
          memory.isLoading ? null : (
            <EmptyState
              icon={<Brain size={24} color="#71717a" />}
              title={searching ? "Aucun résultat" : "Mémoire vide"}
              subtitle={
                searching
                  ? "Aucune mémoire ne correspond à cette recherche."
                  : "La mémoire longue durée du swarm apparaît ici. Épingle ce qui compte."
              }
            />
          )
        }
      />
    </View>
  );
}
