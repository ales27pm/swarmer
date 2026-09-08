import { Stack } from "expo-router";

import { COLORS } from "@/components/swarm-ui";
import { LiveSyncProvider } from "@/lib/sync/live-sync-provider";

export default function RootLayout() {
  return (
    <LiveSyncProvider>
      <Stack
        screenOptions={{
          contentStyle: { backgroundColor: COLORS.background },
          headerStyle: { backgroundColor: COLORS.background },
          headerTintColor: COLORS.text,
        }}
      >
        <Stack.Screen name="(main)" options={{ headerShown: false }} />
        <Stack.Screen
          name="local-model"
          options={{ headerBackTitle: "Réglages", title: "Modèle local" }}
        />
        <Stack.Screen
          name="task/[id]"
          options={{ headerBackTitle: "Retour", title: "Tâche" }}
        />
      </Stack>
    </LiveSyncProvider>
  );
}
