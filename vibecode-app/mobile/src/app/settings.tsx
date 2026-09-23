import React, { useState } from "react";
import { Pressable, ScrollView, Text, TextInput, View } from "react-native";
import { Stack } from "expo-router";
import { CheckCircle2, KeyRound, ScrollText } from "lucide-react-native";
import { useAudit, useBootstrap, usePairing } from "@/lib/hooks";
import { SectionTitle, timeAgo } from "@/lib/ui";
import { cn } from "@/lib/cn";

export default function SettingsScreen() {
  const bootstrap = useBootstrap();
  const audit = useAudit();
  const pairing = usePairing();

  const [deviceName, setDeviceName] = useState<string>("iPhone de Alexis");
  const [code, setCode] = useState<string>("");
  const [token, setToken] = useState<string | null>(null);

  const counts = bootstrap.data?.counts;
  const backendUrl = process.env.EXPO_PUBLIC_BACKEND_URL ?? "";

  return (
    <View className="flex-1 bg-zinc-950" testID="settings-screen">
      <Stack.Screen
        options={{ title: "Réglages", headerStyle: { backgroundColor: "#09090b" }, headerTintColor: "#fafafa" }}
      />
      <ScrollView contentContainerStyle={{ padding: 16, paddingBottom: 40 }}>
        {/* Control plane */}
        <SectionTitle title="Control plane" />
        <View className="bg-zinc-900 border border-zinc-800 rounded-2xl p-4">
          <View className="flex-row items-center mb-2">
            <View className={cn("w-2 h-2 rounded-full mr-2", bootstrap.isError ? "bg-red-500" : "bg-emerald-400")} />
            <Text className="text-zinc-100 font-semibold">
              {bootstrap.isError ? "Injoignable" : "Connecté"}
            </Text>
          </View>
          <Text className="text-zinc-500 text-[11px] font-mono" selectable numberOfLines={1}>
            {backendUrl}
          </Text>
          {counts ? (
            <View className="flex-row mt-3 gap-2">
              {[
                ["Tâches", counts.tasks],
                ["Agents", counts.agents],
                ["Mémoires", counts.memory_items],
                ["Audit", counts.audit_events],
              ].map(([label, value]) => (
                <View key={label as string} className="flex-1 bg-zinc-950 border border-zinc-800 rounded-xl py-2 items-center">
                  <Text className="text-emerald-400 font-bold text-base">{value}</Text>
                  <Text className="text-zinc-500 text-[10px] mt-0.5">{label}</Text>
                </View>
              ))}
            </View>
          ) : null}
        </View>

        {/* Pairing */}
        <SectionTitle title="Appairage de l'appareil" />
        <View className="bg-zinc-900 border border-zinc-800 rounded-2xl p-4">
          {token ? (
            <View className="flex-row items-center gap-2">
              <CheckCircle2 size={18} color="#34d399" />
              <View className="flex-1">
                <Text className="text-zinc-100 font-semibold">Appareil appairé</Text>
                <Text className="text-zinc-500 text-[11px] font-mono" selectable numberOfLines={1}>
                  {token}
                </Text>
              </View>
            </View>
          ) : (
            <>
              <Text className="text-zinc-400 text-xs mb-3 leading-4">
                Génère un code sur le control plane, puis confirme-le ici pour lier cet iPhone.
              </Text>
              <TextInput
                testID="device-name-input"
                className="bg-zinc-950 border border-zinc-800 rounded-xl px-3 py-2.5 text-zinc-100 text-sm mb-2"
                placeholder="Nom de l'appareil"
                placeholderTextColor="#52525b"
                value={deviceName}
                onChangeText={setDeviceName}
              />
              {pairing.start.data ? (
                <View className="bg-zinc-950 border border-emerald-500/30 rounded-xl p-4 items-center mb-2">
                  <Text className="text-zinc-500 text-[10px] uppercase tracking-widest mb-1">Code d'appairage</Text>
                  <Text className="text-emerald-400 text-3xl font-bold tracking-[8px]" selectable testID="pairing-code">
                    {pairing.start.data.code}
                  </Text>
                </View>
              ) : null}
              {pairing.start.data ? (
                <View className="flex-row gap-2">
                  <TextInput
                    testID="pairing-code-input"
                    className="flex-1 bg-zinc-950 border border-zinc-800 rounded-xl px-3 py-2.5 text-zinc-100 text-sm"
                    placeholder="Code à 6 chiffres"
                    placeholderTextColor="#52525b"
                    keyboardType="number-pad"
                    maxLength={6}
                    value={code}
                    onChangeText={setCode}
                  />
                  <Pressable
                    testID="pairing-confirm-button"
                    onPress={() =>
                      pairing.confirm.mutate(
                        { code, device_id: "iphone-ales", device_name: deviceName },
                        { onSuccess: (res) => setToken(res.device_token) }
                      )
                    }
                    disabled={code.length !== 6 || pairing.confirm.isPending}
                    className={cn(
                      "px-5 rounded-xl items-center justify-center",
                      code.length === 6 ? "bg-emerald-500 active:bg-emerald-400" : "bg-zinc-800"
                    )}
                  >
                    <KeyRound size={16} color={code.length === 6 ? "#09090b" : "#52525b"} />
                  </Pressable>
                </View>
              ) : (
                <Pressable
                  testID="pairing-start-button"
                  onPress={() => pairing.start.mutate()}
                  className="bg-emerald-500 rounded-xl py-3 items-center active:bg-emerald-400"
                >
                  <Text className="text-zinc-950 font-bold text-sm">Générer un code</Text>
                </Pressable>
              )}
              {pairing.confirm.isError ? (
                <Text className="text-red-400 text-xs mt-2">Code invalide ou expiré. Réessaie.</Text>
              ) : null}
            </>
          )}
        </View>

        {/* Audit ledger */}
        <SectionTitle title="Journal d'audit" right={<ScrollText size={14} color="#71717a" />} />
        <View className="bg-zinc-900 border border-zinc-800 rounded-2xl overflow-hidden">
          {(audit.data ?? []).slice(0, 15).map((e, i) => (
            <View
              key={e.id}
              className={cn("px-4 py-3 flex-row items-center", i > 0 && "border-t border-zinc-800/60")}
            >
              <View className="flex-1">
                <Text className="text-zinc-200 text-xs font-semibold">{e.event_type}</Text>
                <Text className="text-zinc-600 text-[10px] font-mono mt-0.5" numberOfLines={1}>
                  {e.actor_id} · {e.hash?.slice(0, 12)}
                </Text>
              </View>
              <Text className="text-zinc-600 text-[10px]">{timeAgo(e.created_at)}</Text>
            </View>
          ))}
          {(audit.data ?? []).length === 0 ? (
            <Text className="text-zinc-600 text-xs text-center py-6">Aucun événement pour l'instant.</Text>
          ) : null}
        </View>

        <Text className="text-zinc-700 text-[10px] text-center mt-6">
          monGARS Swarm App · Phase 1 · Ubuntu garde la vérité officielle
        </Text>
      </ScrollView>
    </View>
  );
}
