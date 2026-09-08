import React from "react";
import { FlatList, Text, View } from "react-native";
import { Bot } from "lucide-react-native";
import { useAgents } from "@/lib/hooks";
import type { Agent } from "@/lib/types";
import { EmptyState, timeAgo } from "@/lib/ui";
import { cn } from "@/lib/cn";

const STATUS_DOT: Record<string, string> = {
  online: "bg-emerald-400",
  busy: "bg-amber-400",
  offline: "bg-zinc-600",
};

function AgentCard({ agent }: { agent: Agent }) {
  let skills: string[] = [];
  try {
    skills = JSON.parse(agent.skills_json);
  } catch {
    skills = [];
  }
  return (
    <View className="bg-zinc-900 border border-zinc-800 rounded-2xl p-4 mb-3" testID={`agent-card-${agent.id}`}>
      <View className="flex-row items-center mb-1">
        <View className={cn("w-2.5 h-2.5 rounded-full mr-2", STATUS_DOT[agent.status] ?? "bg-zinc-600")} />
        <Text className="text-zinc-100 font-bold text-base flex-1">{agent.name}</Text>
        <Text className="text-zinc-500 text-[11px] uppercase tracking-wide">{agent.status}</Text>
      </View>
      <Text className="text-zinc-500 text-[11px] font-mono mb-2" selectable>
        {agent.id} · v{agent.version}
      </Text>
      {agent.model_id ? (
        <View className="bg-zinc-950 border border-zinc-800 rounded-lg px-2.5 py-1.5 mb-3 self-start">
          <Text className="text-emerald-400/90 text-[11px] font-mono" selectable>
            {agent.model_id}
          </Text>
        </View>
      ) : null}
      <View className="flex-row flex-wrap gap-1.5">
        {skills.map((s) => (
          <View key={s} className="bg-zinc-800 rounded-md px-2 py-1">
            <Text className="text-zinc-300 text-[11px]">{s}</Text>
          </View>
        ))}
      </View>
      <Text className="text-zinc-600 text-[11px] mt-3">
        {agent.last_heartbeat_at ? `Heartbeat ${timeAgo(agent.last_heartbeat_at)}` : "Jamais vu en ligne"}
      </Text>
    </View>
  );
}

export default function AgentsScreen() {
  const agents = useAgents();
  const online = (agents.data ?? []).filter((a) => a.status === "online").length;

  return (
    <View className="flex-1 bg-zinc-950" testID="agents-screen">
      <FlatList
        testID="agents-list"
        data={agents.data ?? []}
        keyExtractor={(a: Agent) => a.id}
        renderItem={({ item }) => <AgentCard agent={item} />}
        contentContainerStyle={{ padding: 16, paddingBottom: 24 }}
        ListHeaderComponent={
          <Text className="text-zinc-500 text-xs mb-3">
            {online}/{agents.data?.length ?? 0} agents en ligne · registry du control plane
          </Text>
        }
        ListEmptyComponent={
          agents.isLoading ? null : (
            <EmptyState
              icon={<Bot size={24} color="#71717a" />}
              title="Aucun agent"
              subtitle="Les workers enregistrés auprès du control plane apparaissent ici."
            />
          )
        }
      />
    </View>
  );
}
