import React from "react";
import { FlatList, Pressable, Text, View } from "react-native";
import { ShieldCheck } from "lucide-react-native";
import { useApprovalDecision, useApprovals } from "@/lib/hooks";
import type { Approval } from "@/lib/types";
import { EmptyState, RISK_STYLE, timeAgo } from "@/lib/ui";
import { cn } from "@/lib/cn";

const RISK_LABEL: Record<string, string> = { low: "faible", medium: "moyen", high: "élevé" };

function ApprovalCard({ approval }: { approval: Approval }) {
  const decide = useApprovalDecision();
  const pending = approval.status === "pending";

  let diff: string | null = null;
  try {
    diff = JSON.parse(approval.request_json)?.diff_preview ?? null;
  } catch {
    diff = null;
  }

  return (
    <View className="bg-zinc-900 border border-zinc-800 rounded-2xl p-4 mb-3" testID={`approval-card-${approval.id}`}>
      <View className="flex-row items-center justify-between mb-2">
        <Text className="text-emerald-500 text-[11px] font-bold uppercase tracking-wider">
          {approval.agent_id ?? "agent"}
        </Text>
        <Text className="text-zinc-600 text-[11px]">{timeAgo(approval.created_at)}</Text>
      </View>

      <Text className="text-zinc-100 font-bold text-base">{approval.action_type}</Text>
      {approval.target ? (
        <Text className="text-zinc-400 text-xs mt-1 font-mono" selectable numberOfLines={2}>
          {approval.target}
        </Text>
      ) : null}
      {approval.reason ? <Text className="text-zinc-300 text-sm mt-2 leading-5">{approval.reason}</Text> : null}

      <View className="flex-row items-center mt-3 gap-3">
        <Text className={cn("text-xs font-semibold", RISK_STYLE[approval.risk])}>
          Risque {RISK_LABEL[approval.risk] ?? approval.risk}
        </Text>
        <Text className="text-zinc-600 text-[11px]">audit: {approval.id}</Text>
      </View>

      {diff ? (
        <View className="bg-zinc-950 border border-zinc-800 rounded-xl p-3 mt-3">
          <Text className="text-zinc-400 text-[11px] font-mono" selectable>
            {diff}
          </Text>
        </View>
      ) : null}

      {pending ? (
        <View className="flex-row gap-2 mt-4">
          <Pressable
            testID={`allow-button-${approval.id}`}
            onPress={() => decide.mutate({ id: approval.id, decision: "allow_once" })}
            disabled={decide.isPending}
            className="flex-1 bg-emerald-500 rounded-xl py-3 items-center active:bg-emerald-400"
          >
            <Text className="text-zinc-950 font-bold text-sm">Autoriser</Text>
          </Pressable>
          <Pressable
            testID={`deny-button-${approval.id}`}
            onPress={() => decide.mutate({ id: approval.id, decision: "deny" })}
            disabled={decide.isPending}
            className="flex-1 bg-zinc-800 border border-zinc-700 rounded-xl py-3 items-center active:bg-zinc-700"
          >
            <Text className="text-red-400 font-bold text-sm">Refuser</Text>
          </Pressable>
        </View>
      ) : (
        <View className="mt-4 bg-zinc-950 rounded-xl py-2.5 items-center border border-zinc-800">
          <Text className={cn("text-sm font-semibold", approval.status === "allowed" ? "text-emerald-400" : "text-red-400")}>
            {approval.status === "allowed" ? "Autorisée" : "Refusée"} · {timeAgo(approval.decided_at ?? approval.created_at)}
          </Text>
        </View>
      )}
    </View>
  );
}

export default function ApprovalsScreen() {
  const pending = useApprovals("pending");
  const decided = useApprovals("allowed");

  const data = [...(pending.data ?? []), ...(decided.data ?? [])];

  return (
    <View className="flex-1 bg-zinc-950" testID="approvals-screen">
      <FlatList
        testID="approvals-list"
        data={data}
        keyExtractor={(a: Approval) => a.id}
        renderItem={({ item }) => <ApprovalCard approval={item} />}
        contentContainerStyle={{ padding: 16, paddingBottom: 24 }}
        ListHeaderComponent={
          <Text className="text-zinc-500 text-xs mb-3 leading-4">
            La Permission Gateway bloque chaque action sensible jusqu'à ta décision. Rien ne s'exécute sans toi.
          </Text>
        }
        ListEmptyComponent={
          pending.isLoading ? null : (
            <EmptyState
              icon={<ShieldCheck size={24} color="#71717a" />}
              title="Aucune approbation"
              subtitle="Quand un agent demande une action sensible, elle apparaît ici avec le risque et la cible exacte."
            />
          )
        }
      />
    </View>
  );
}
