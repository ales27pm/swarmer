import { useCallback, useEffect, useState } from "react";
import { Pressable, Text, View } from "react-native";
import { useRouter } from "expo-router";

import { ScreenShell } from "@/components/screen-shell";
import { Card, COLORS, EmptyState, ErrorBanner, StatusBadge, timeAgo } from "@/components/swarm-ui";
import { listTasks, type Task, type TaskStatus } from "@/lib/api/client";

const FILTERS: { key: TaskStatus | "all"; label: string }[] = [
  { key: "all", label: "Toutes" },
  { key: "created", label: "Créées" },
  { key: "planned", label: "Planifiées" },
  { key: "waiting_permission", label: "Permission" },
  { key: "queued", label: "En file" },
  { key: "running", label: "En cours" },
  { key: "blocked", label: "Bloquées" },
  { key: "completed", label: "Terminées" },
  { key: "failed", label: "Échouées" },
  { key: "cancelled", label: "Annulées" },
];

export default function TasksScreen() {
  const router = useRouter();
  const [filter, setFilter] = useState<TaskStatus | "all">("all");
  const [items, setItems] = useState<Task[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      setItems(await listTasks(filter === "all" ? undefined : filter));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setRefreshing(false);
    }
  }, [filter]);

  useEffect(() => {
    void (async () => {
      await refresh();
    })();
  }, [refresh]);

  return (
    <ScreenShell
      title="Tâches"
      subtitle="Cycle de vie réel, résultats et erreurs du control plane."
      onRefresh={() => void refresh()}
      refreshing={refreshing}
      testID="tasks-screen"
    >
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
        {FILTERS.map((item) => {
          const selected = filter === item.key;
          return (
            <Pressable
              accessibilityRole="button"
              accessibilityState={{ selected }}
              key={item.key}
              onPress={() => setFilter(item.key)}
              style={{
                backgroundColor: selected ? `${COLORS.accent}1f` : COLORS.panel,
                borderColor: selected ? COLORS.accent : COLORS.border,
                borderRadius: 999,
                borderWidth: 1,
                minHeight: 44,
                justifyContent: "center",
                paddingHorizontal: 14,
              }}
              testID={`filter-${item.key}`}
            >
              <Text style={{ color: selected ? COLORS.accent : COLORS.muted, fontWeight: "700" }}>
                {item.label}
              </Text>
            </Pressable>
          );
        })}
      </View>
      <ErrorBanner message={error} />
      {!items.length && !refreshing && !error ? (
        <EmptyState
          title="Aucune tâche"
          subtitle="Les intentions envoyées depuis le chat apparaîtront ici sans statut simulé."
        />
      ) : null}
      <View style={{ gap: 12 }} testID="tasks-list">
        {items.map((task) => (
          <Pressable
            accessibilityRole="button"
            key={task.id}
            onPress={() => router.push({ pathname: "/task/[id]", params: { id: task.id } })}
            style={({ pressed }) => ({ opacity: pressed ? 0.75 : 1 })}
            testID={`task-row-${task.id}`}
          >
            <Card>
              <View style={{ alignItems: "center", flexDirection: "row", justifyContent: "space-between" }}>
                <StatusBadge status={task.status} />
                <Text style={{ color: COLORS.subtle, fontSize: 11 }}>{timeAgo(task.updated_at)}</Text>
              </View>
              <Text numberOfLines={2} style={{ color: COLORS.text, fontSize: 16, fontWeight: "700", lineHeight: 21 }}>
                {task.title || task.input}
              </Text>
              <Text selectable style={{ color: COLORS.subtle, fontSize: 11 }}>
                {task.mode} · priorité {task.priority} · {task.id}
              </Text>
            </Card>
          </Pressable>
        ))}
      </View>
    </ScreenShell>
  );
}
