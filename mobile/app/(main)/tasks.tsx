import { useCallback, useEffect, useRef, useState } from "react";
import { Image, Pressable, Text, View } from "react-native";
import { useRouter } from "expo-router";

import { ScreenShell } from "@/components/screen-shell";
import { ActionButton, Card, COLORS, ErrorBanner, StatusBadge, timeAgo } from "@/components/swarm-ui";
import { getServerUrl, listTasks, type Task, type TaskStatus } from "@/lib/application-api/server";
import { localTasks } from "@/lib/state/replica";
import { useLiveRefresh } from "@/lib/sync/live-sync-context";

const FILTERS: { key: TaskStatus | "all"; label: string }[] = [
  { key: "all", label: "Toutes" },
  { key: "running", label: "En cours" },
  { key: "waiting_permission", label: "Permission" },
  { key: "completed", label: "Terminées" },
  { key: "created", label: "Créées" },
  { key: "planned", label: "Planifiées" },
  { key: "queued", label: "En file" },
  { key: "blocked", label: "Bloquées" },
  { key: "failed", label: "Échouées" },
  { key: "cancelled", label: "Annulées" },
];

type TaskLoad = { tasks: Task[]; offline: boolean; error: string | null };
const SERVER_CHANGED = "Le serveur a changé. Actualise l’activité.";

async function cachedTasks(scope: string | null, filter: TaskStatus | undefined, message: string): Promise<TaskLoad> {
  const unavailable = { tasks: [], offline: false, error: message };
  try {
    if (!scope || await getServerUrl() !== scope) return unavailable;
    const tasks = await localTasks(scope, filter);
    if (await getServerUrl() !== scope) return { ...unavailable, error: SERVER_CHANGED };
    return { tasks, offline: true, error: tasks.length ? `Hors ligne — affichage du cache local. ${message}` : message };
  } catch {
    return unavailable;
  }
}

async function loadTasks(filter: TaskStatus | undefined, isCurrent: () => boolean): Promise<TaskLoad | null> {
  let scope: string | null = null;
  try {
    scope = await getServerUrl();
    if (!isCurrent()) return null;
    const tasks = await listTasks(filter);
    if (await getServerUrl() !== scope) return { tasks: [], offline: false, error: SERVER_CHANGED };
    return { tasks, offline: false, error: null };
  } catch (cause) {
    if (!isCurrent()) return null;
    const message = cause instanceof Error ? cause.message : String(cause);
    return cachedTasks(scope, filter, message);
  }
}

export default function TasksScreen() {
  const router = useRouter();
  const refreshEpoch = useRef(0);
  const [filter, setFilter] = useState<TaskStatus | "all">("all");
  const [moreFilters, setMoreFilters] = useState(false);
  const [items, setItems] = useState<Task[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);

  const refresh = useCallback(async () => {
    const epoch = ++refreshEpoch.current;
    const isCurrent = () => refreshEpoch.current === epoch;
    setRefreshing(true);
    setError(null);
    setItems([]);
    setOffline(false);
    try {
      const result = await loadTasks(filter === "all" ? undefined : filter, isCurrent);
      if (!isCurrent() || !result) return;
      setItems(result.tasks);
      setOffline(result.offline);
      setError(result.error);
    } finally {
      if (isCurrent()) setRefreshing(false);
    }
  }, [filter]);

  useEffect(() => {
    void refresh();
    return () => { refreshEpoch.current += 1; };
  }, [refresh]);
  useLiveRefresh(refresh);

  return (
    <ScreenShell
      showTitle={false}
      title="Activité"
      subtitle="Retrouve les demandes en cours et leurs résultats."
      onRefresh={() => void refresh()}
      refreshing={refreshing}
      testID="tasks-screen"
    >
      <ActionButton
        label="Autorisations à vérifier"
        onPress={() => router.push("/approvals")}
        testID="tasks-approvals-button"
      />
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
        {FILTERS.slice(0, moreFilters ? FILTERS.length : 4).map((item) => {
          const selected = filter === item.key;
          return (
            <Pressable
              accessibilityRole="button"
              accessibilityState={{ selected }}
              key={item.key}
              onPress={() => {
                if (filter !== item.key) {
                  refreshEpoch.current += 1;
                  setItems([]);
                  setFilter(item.key);
                }
              }}
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
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ expanded: moreFilters }}
        onPress={() => setMoreFilters((expanded) => !expanded)}
        style={{ minHeight: 44, justifyContent: "center" }}
        testID="tasks-more-filters"
      >
        <Text style={{ color: COLORS.muted, fontWeight: "600" }}>
          {moreFilters ? "Masquer les autres filtres" : "Autres filtres"}
          {FILTERS.slice(4).find((item) => item.key === filter)
            ? ` · ${FILTERS.find((item) => item.key === filter)?.label}` : ""}
        </Text>
      </Pressable>
      <ErrorBanner message={error} />
      {offline ? (
        <Text style={{ color: COLORS.warning }}>Les statuts affichés peuvent être périmés.</Text>
      ) : null}
      {!items.length && !refreshing && !error ? (
        <View style={{ alignItems: "center", gap: 10, paddingVertical: 20 }}>
          <Image
            accessible={false}
            source={require("../../assets/illustrations/activity-ready.png")}
            resizeMode="contain"
            style={{ width: 160, height: 160 }}
          />
          <Text style={{ color: COLORS.text, fontSize: 18, fontWeight: "700" }}>
            {filter === "all" ? "Aucune tâche" : "Aucune tâche dans ce filtre"}
          </Text>
          <Text style={{ color: COLORS.muted, lineHeight: 20, textAlign: "center" }}>
            {filter === "all"
              ? "Tes demandes et leurs résultats apparaîtront ici."
              : "Choisis un autre filtre pour retrouver tes demandes."}
          </Text>
        </View>
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
            </Card>
          </Pressable>
        ))}
      </View>
    </ScreenShell>
  );
}
