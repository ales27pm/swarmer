import React from "react";
import { Tabs } from "expo-router";
import { Bot, Brain, ListChecks, MessageSquare, ShieldCheck } from "lucide-react-native";

export default function TabLayout() {
  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: "#09090b" },
        headerTintColor: "#fafafa",
        headerTitleStyle: { fontWeight: "700" },
        headerShadowVisible: false,
        tabBarStyle: { backgroundColor: "#09090b", borderTopColor: "#27272a" },
        tabBarActiveTintColor: "#34d399",
        tabBarInactiveTintColor: "#71717a",
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "Console",
          tabBarIcon: ({ color }: { color: string }) => <MessageSquare size={22} color={color} />,
        }}
      />
      <Tabs.Screen
        name="tasks"
        options={{
          title: "Tâches",
          tabBarIcon: ({ color }: { color: string }) => <ListChecks size={22} color={color} />,
        }}
      />
      <Tabs.Screen
        name="approvals"
        options={{
          title: "Approbations",
          tabBarIcon: ({ color }: { color: string }) => <ShieldCheck size={22} color={color} />,
        }}
      />
      <Tabs.Screen
        name="memory"
        options={{
          title: "Mémoire",
          tabBarIcon: ({ color }: { color: string }) => <Brain size={22} color={color} />,
        }}
      />
      <Tabs.Screen
        name="agents"
        options={{
          title: "Agents",
          tabBarIcon: ({ color }: { color: string }) => <Bot size={22} color={color} />,
        }}
      />
    </Tabs>
  );
}
