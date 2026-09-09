import { Tabs } from "expo-router";

import { COLORS } from "@/components/swarm-ui";

export default function MainLayout() {
  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        sceneStyle: { backgroundColor: COLORS.background },
        tabBarActiveTintColor: COLORS.accent,
        tabBarInactiveTintColor: COLORS.subtle,
        tabBarStyle: {
          backgroundColor: COLORS.panel,
          borderTopColor: COLORS.border,
        },
      }}
    >
      <Tabs.Screen name="index" options={{ title: "Chat" }} />
      <Tabs.Screen name="tasks" options={{ title: "Tâches" }} />
      <Tabs.Screen name="approvals" options={{ title: "Accords" }} />
      <Tabs.Screen name="memory" options={{ title: "Mémoire" }} />
      <Tabs.Screen name="swarm" options={{ title: "Swarm" }} />
      <Tabs.Screen name="agents" options={{ href: null, title: "Agents" }} />
      <Tabs.Screen name="settings" options={{ href: null, title: "Réglages" }} />
    </Tabs>
  );
}
