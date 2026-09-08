import React from "react";
import { ActivityIndicator, Pressable, ScrollView, Text, View } from "react-native";
import { Stack, useLocalSearchParams } from "expo-router";
import { ThumbsDown, ThumbsUp, XCircle } from "lucide-react-native";
import { useApprovalDecision, useCancelTask, useFeedback, useTask } from "@/lib/hooks";
import type { Approval, Message } from "@/lib/types";
import { RISK_STYLE, StatusBadge, timeAgo } from "@/lib/ui";
import { cn } from "@/lib/cn";

function InlineApproval({ approval }: { approval: Approval }) {
  const decide = useApprovalDecision();
  if (approval.status !== "pending") return null;
  return (
    <View className="bg-amber-500/10 border border-amber-500/30 rounded-2xl p-4 mb-3">
      <Text className="text-amber-400 text-[11px] font-bold uppercase tracking-wider mb-1">
        Permission demandée
      </Text>
      <Text className="text-zinc-100 font-semibold">{approval.action_type}</Text>
      {approval.target ? (
        <Text className="text-zinc-400 text-xs font-mono mt-1" selectable>
          {approval.target}
        </Text>
      ) : null}
      <Text className={cn("text-xs font-semibold mt-2", RISK_STYLE[approval.risk])}>
        Risque: {approval.risk}
      </Text>
      <View className="flex-row gap-2 mt-3">
        <Pressable
          testID="detail-allow-button"
          onPress={() => decide.mutate({ id: approval.id, decision: "allow_once" })}
          className="flex-1 bg-emerald-500 rounded-xl py-2.5 items-center active:bg-emerald-400"
        >
          <Text className="text-zinc-950 font-bold text-sm">Autoriser</Text>
        </Pressable>
        <Pressable
          testID="detail-deny-button"
          onPress={() => decide.mutate({ id: approval.id, decision: "deny" })}
          className="flex-1 bg-zinc-800 border border-zinc-700 rounded-xl py-2.5 items-center"
        >
          <Text className="text-red-400 font-bold text-sm">Refuser</Text>
        </Pressable>
      </View>
    </View>
  );
}

export default function TaskDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const detail = useTask(id ?? "");
  const cancel = useCancelTask();
  const feedback = useFeedback();

  const task = detail.data?.task;
  const active = task && (task.status === "running" || task.status === "queued" || task.status === "waiting_permission");

  return (
    <View className="flex-1 bg-zinc-950" testID="task-detail-screen">
      <Stack.Screen options={{ title: "Tâche", headerStyle: { backgroundColor: "#09090b" }, headerTintColor: "#fafafa" }} />
      {!task ? (
        <View className="flex-1 items-center justify-center">
          <ActivityIndicator color="#34d399" testID="loading-indicator" />
        </View>
      ) : (
        <ScrollView contentContainerStyle={{ padding: 16, paddingBottom: 40 }}>
          <View className="flex-row items-center justify-between mb-3">
            <StatusBadge status={task.status} />
            <Text className="text-zinc-600 text-[11px]">{timeAgo(task.created_at)}</Text>
          </View>
          <Text className="text-zinc-100 text-lg font-bold leading-6" selectable>
            {task.input}
          </Text>
          <Text className="text-zinc-500 text-xs mt-2 font-mono" selectable>
            {task.id} · mode {task.mode}
          </Text>

          {active ? (
            <Pressable
              testID="cancel-task-button"
              onPress={() => cancel.mutate(task.id)}
              className="flex-row items-center justify-center gap-2 mt-4 bg-zinc-900 border border-zinc-800 rounded-xl py-3 active:bg-zinc-800"
            >
              <XCircle size={16} color="#f87171" />
              <Text className="text-red-400 font-semibold text-sm">Annuler la tâche</Text>
            </Pressable>
          ) : null}

          {(detail.data?.approvals ?? []).map((a) => (
            <View key={a.id} className="mt-4">
              <InlineApproval approval={a} />
            </View>
          ))}

          <Text className="text-zinc-400 text-xs font-bold uppercase tracking-widest mt-6 mb-3">Timeline</Text>
          {(detail.data?.messages ?? []).map((m: Message) => (
            <View key={m.id} className="bg-zinc-900 border border-zinc-800 rounded-xl p-3 mb-2">
              <View className="flex-row justify-between mb-1">
                <Text className="text-emerald-500 text-[10px] font-bold uppercase tracking-wider">
                  {m.agent_id ?? m.role}
                </Text>
                <Text className="text-zinc-600 text-[10px]">{timeAgo(m.created_at)}</Text>
              </View>
              <Text className="text-zinc-200 text-sm leading-5" selectable>
                {m.content}
              </Text>
            </View>
          ))}

          {task.status === "completed" ? (
            <View className="mt-4">
              <Text className="text-zinc-400 text-xs font-bold uppercase tracking-widest mb-3">Feedback</Text>
              <View className="flex-row gap-2">
                <Pressable
                  testID="feedback-up-button"
                  onPress={() => feedback.mutate({ task_id: task.id, score: 5, label: "good" })}
                  className="flex-1 flex-row items-center justify-center gap-2 bg-zinc-900 border border-zinc-800 rounded-xl py-3 active:bg-zinc-800"
                >
                  <ThumbsUp size={16} color="#34d399" />
                  <Text className="text-zinc-200 text-sm font-semibold">Utile</Text>
                </Pressable>
                <Pressable
                  testID="feedback-down-button"
                  onPress={() => feedback.mutate({ task_id: task.id, score: 1, label: "bad" })}
                  className="flex-1 flex-row items-center justify-center gap-2 bg-zinc-900 border border-zinc-800 rounded-xl py-3 active:bg-zinc-800"
                >
                  <ThumbsDown size={16} color="#f87171" />
                  <Text className="text-zinc-200 text-sm font-semibold">À revoir</Text>
                </Pressable>
              </View>
              {feedback.isSuccess ? (
                <Text className="text-emerald-500 text-xs text-center mt-2">
                  Feedback enregistré pour les evals futures.
                </Text>
              ) : null}
            </View>
          ) : null}
        </ScrollView>
      )}
    </View>
  );
}
