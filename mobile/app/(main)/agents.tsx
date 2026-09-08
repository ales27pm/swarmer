import { useCallback, useEffect, useState } from "react";
import { Text, View } from "react-native";

import { ScreenShell } from "@/components/screen-shell";
import { ActionButton, Card, COLORS, EmptyState, ErrorBanner, timeAgo } from "@/components/swarm-ui";
import { listAgents, type Agent } from "@/lib/api/client";
import { useLiveRefresh } from "@/lib/sync/live-sync-context";

const STATUS_COLOR = {
  online: COLORS.accent,
  busy: COLORS.warning,
  draining: COLORS.warning,
  offline: COLORS.subtle,
  unverified: COLORS.warning,
} as const satisfies Record<Agent["status"], string>;

const STATUS_LABEL = {
  online: "en ligne",
  busy: "occupé",
  draining: "en retrait",
  offline: "hors ligne",
  unverified: "non vérifié",
} as const satisfies Record<Agent["status"], string>;

export default function AgentsScreen() {
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<Agent[]>([]);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      setItems(await listAgents());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(refresh);
  }, [refresh]);
  useLiveRefresh(refresh);

  const active = items.filter((item) =>
    item.status === "online" || item.status === "busy" || item.status === "draining"
  ).length;
  const unverified = items.filter((item) => item.status === "unverified").length;

  return (
    <ScreenShell
      title="Agents"
      subtitle={`${active}/${items.length} agent${items.length === 1 ? "" : "s"} actif${active === 1 ? "" : "s"} · ${unverified} non vérifié${unverified === 1 ? "" : "s"} · lecture seule`}
      onRefresh={() => void refresh()}
      refreshing={refreshing}
      testID="agents-screen"
    >
      <ErrorBanner message={error} />
      <ActionButton
        busy={refreshing}
        label="Actualiser les heartbeats"
        onPress={() => void refresh()}
        testID="refresh-agents-button"
      />
      {unverified ? (
        <Text style={{ color: COLORS.warning, lineHeight: 19 }}>
          « Non vérifié » signifie que l’agent est enregistré, mais qu’aucun heartbeat authentifié n’a encore confirmé son activité.
        </Text>
      ) : null}
      {!items.length && !refreshing && !error ? (
        <EmptyState
          title="Aucun agent enregistré"
          subtitle="L’enregistrement et les heartbeats restent gérés par le control plane, pas par cette interface."
        />
      ) : null}
      <View style={{ gap: 12 }} testID="agents-list">
        {items.map((agent) => (
          <Card key={agent.id} testID={`agent-card-${agent.id}`}>
            <View style={{ alignItems: "center", flexDirection: "row", gap: 8 }}>
              <View style={{ backgroundColor: STATUS_COLOR[agent.status], borderRadius: 999, height: 9, width: 9 }} />
              <Text style={{ color: COLORS.text, flex: 1, fontSize: 16, fontWeight: "800" }}>
                {agent.name}
              </Text>
              <Text style={{ color: STATUS_COLOR[agent.status], fontSize: 11, fontWeight: "700", textTransform: "uppercase" }}>
                {STATUS_LABEL[agent.status]}
              </Text>
            </View>
            <Text selectable style={{ color: COLORS.subtle, fontSize: 11 }}>
              {agent.id} · v{agent.version}
            </Text>
            {agent.model_id ? (
              <Text selectable style={{ color: COLORS.accent, fontFamily: "Courier", fontSize: 12 }}>
                {agent.model_id}
              </Text>
            ) : null}
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
              {agent.skills.map((skill) => (
                <View key={skill} style={{ backgroundColor: COLORS.panelRaised, borderRadius: 7, paddingHorizontal: 8, paddingVertical: 5 }}>
                  <Text style={{ color: COLORS.muted, fontSize: 11 }}>{skill}</Text>
                </View>
              ))}
            </View>
            <Text style={{ color: COLORS.subtle, fontSize: 11 }}>
              Dernier heartbeat {timeAgo(agent.last_heartbeat_at)}
            </Text>
          </Card>
        ))}
      </View>
    </ScreenShell>
  );
}
