import React from "react";
import { Text, View } from "react-native";
import { cn } from "./cn";
import type { RiskLevel, TaskStatus } from "./types";

// Dark mission-control palette (used via NativeWind classes):
// bg zinc-950 · card zinc-900 · border zinc-800 · accent emerald-400

export const STATUS_STYLE: Record<TaskStatus, { label: string; classes: string }> = {
  queued: { label: "En file", classes: "bg-zinc-700/40 text-zinc-300" },
  running: { label: "En cours", classes: "bg-sky-500/15 text-sky-400" },
  waiting_permission: { label: "Permission", classes: "bg-amber-500/15 text-amber-400" },
  completed: { label: "Terminée", classes: "bg-emerald-500/15 text-emerald-400" },
  failed: { label: "Échouée", classes: "bg-red-500/15 text-red-400" },
  cancelled: { label: "Annulée", classes: "bg-zinc-700/40 text-zinc-400" },
};

export const RISK_STYLE: Record<RiskLevel, string> = {
  low: "text-emerald-400",
  medium: "text-amber-400",
  high: "text-red-400",
};

export function StatusBadge({ status }: { status: TaskStatus }) {
  const s = STATUS_STYLE[status] ?? STATUS_STYLE.queued;
  const [bg, text] = s.classes.split(" ");
  return (
    <View className={cn("rounded-full px-2.5 py-1", bg)}>
      <Text className={cn("text-[11px] font-semibold", text)}>{s.label}</Text>
    </View>
  );
}

export function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return "à l'instant";
  if (min < 60) return `il y a ${min} min`;
  const h = Math.floor(min / 60);
  if (h < 24) return `il y a ${h} h`;
  return `il y a ${Math.floor(h / 24)} j`;
}

export function SectionTitle({ title, right }: { title: string; right?: React.ReactNode }) {
  return (
    <View className="flex-row items-center justify-between mb-3 mt-6">
      <Text className="text-zinc-400 text-xs font-bold uppercase tracking-widest">{title}</Text>
      {right}
    </View>
  );
}

export function EmptyState({ icon, title, subtitle }: { icon: React.ReactNode; title: string; subtitle: string }) {
  return (
    <View className="items-center py-16 px-8">
      <View className="w-14 h-14 rounded-2xl bg-zinc-900 border border-zinc-800 items-center justify-center mb-4">
        {icon}
      </View>
      <Text className="text-zinc-200 font-semibold text-base">{title}</Text>
      <Text className="text-zinc-500 text-sm text-center mt-1">{subtitle}</Text>
    </View>
  );
}
