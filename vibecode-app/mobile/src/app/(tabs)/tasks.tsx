import React, { useState } from "react";
import { FlatList, Pressable, ScrollView, Text, View } from "react-native";
import { Link } from "expo-router";
import { ListChecks } from "lucide-react-native";
import { useTasks } from "@/lib/hooks";
import type { Task, TaskStatus } from "@/lib/types";
import { EmptyState, StatusBadge, timeAgo } from "@/lib/ui";
import { cn } from "@/lib/cn";

const FILTERS: { key: string; label: string }[] = [
  { key: "all", label: "Toutes" },
  { key: "running", label: "En cours" },
  { key: "waiting_permission", label: "Permission" },
  { key: "completed", label: "Terminées" },
  { key: "failed", label: "Échouées" },
];

function TaskRow({ task }: { task: Task }) {
  return (
    <Link href={`/task/${task.id}`} asChild>
      <Pressable
        testID={`task-row-${task.id}`}
        className="bg-zinc-900 border border-zinc-800 rounded-2xl p-4 mb-3 active:bg-zinc-800/70"
      >
        <View className="flex-row items-center justify-between mb-2">
          <StatusBadge status={task.status} />
          <Text className="text-zinc-600 text-[11px]">{timeAgo(task.created_at)}</Text>
        </View>
        <Text className="text-zinc-100 font-semibold text-[15px]" numberOfLines={2}>
          {task.title ?? task.input}
        </Text>
        <View className="flex-row items-center mt-2 gap-2">
          <View className="bg-zinc-800 rounded-md px-2 py-0.5">
            <Text className="text-zinc-400 text-[10px] font-medium uppercase tracking-wide">{task.mode}</Text>
          </View>
          <Text className="text-zinc-600 text-[11px]">{task.id}</Text>
        </View>
      </Pressable>
    </Link>
  );
}

export default function TasksScreen() {
  const [filter, setFilter] = useState<string>("all");
  const tasks = useTasks(filter === "all" ? undefined : filter);

  return (
    <View className="flex-1 bg-zinc-950" testID="tasks-screen">
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        style={{ flexGrow: 0 }}
        contentContainerStyle={{ paddingHorizontal: 16, paddingVertical: 12, gap: 8 }}
      >
        {FILTERS.map((f) => (
          <Pressable
            key={f.key}
            testID={`filter-${f.key}`}
            onPress={() => setFilter(f.key)}
            className={cn(
              "px-4 py-2 rounded-full border",
              filter === f.key ? "bg-emerald-500/15 border-emerald-500/40" : "bg-zinc-900 border-zinc-800"
            )}
          >
            <Text className={cn("text-xs font-semibold", filter === f.key ? "text-emerald-400" : "text-zinc-400")}>
              {f.label}
            </Text>
          </Pressable>
        ))}
      </ScrollView>

      <FlatList
        testID="tasks-list"
        data={tasks.data ?? []}
        keyExtractor={(t: Task) => t.id}
        renderItem={({ item }) => <TaskRow task={item} />}
        contentContainerStyle={{ paddingHorizontal: 16, paddingBottom: 24 }}
        ListEmptyComponent={
          tasks.isLoading ? null : (
            <EmptyState
              icon={<ListChecks size={24} color="#71717a" />}
              title="Aucune tâche"
              subtitle="Les tâches créées depuis la console apparaissent ici avec leur cycle de vie complet."
            />
          )
        }
      />
    </View>
  );
}
