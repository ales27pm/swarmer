import { Tabs } from "expo-router";

export default function MainLayout() {
  return (
    <Tabs screenOptions={{ headerShown: true }}>
      <Tabs.Screen name="index" options={{ title: "Chat" }} />
      <Tabs.Screen name="tasks" options={{ title: "Tasks" }} />
      <Tabs.Screen name="approvals" options={{ title: "Approvals" }} />
      <Tabs.Screen name="memory" options={{ title: "Memory" }} />
      <Tabs.Screen name="agents" options={{ title: "Agents" }} />
      <Tabs.Screen name="settings" options={{ title: "Settings" }} />
    </Tabs>
  );
}
