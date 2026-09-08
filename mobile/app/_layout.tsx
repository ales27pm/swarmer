import { Stack } from "expo-router";

import { COLORS } from "@/components/swarm-ui";

export default function RootLayout() {
  return (
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
      <Stack.Screen name="task/[id]" options={{ title: "Tâche" }} />
    </Stack>
  );
}
