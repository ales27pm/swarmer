import { Stack } from "expo-router";

import { COLORS } from "@/components/swarm-ui";
import { ApplicationNetworkBridge } from "@/lib/application-api/network-bridge";
import { LiveSyncProvider } from "@/lib/sync/live-sync-provider";

export default function RootLayout() {
  return (
    <LiveSyncProvider>
      <ApplicationNetworkBridge />
      <Stack
        screenOptions={{
          contentStyle: { backgroundColor: COLORS.background },
          headerStyle: { backgroundColor: COLORS.background },
          headerTintColor: COLORS.text,
          headerShadowVisible: false,
        }}
      >
        <Stack.Screen name="(main)" options={{ headerShown: false }} />
        <Stack.Screen
          name="local-model"
          options={{ headerBackTitle: "Réglages", title: "Modèle local" }}
        />
        <Stack.Screen name="goal/[id]" options={{ headerBackTitle: "Retour", title: "Projet" }} />
        <Stack.Screen
          name="task/[id]"
          options={{ headerBackTitle: "Retour", title: "Tâche" }}
        />
      </Stack>
    </LiveSyncProvider>
  );
}
