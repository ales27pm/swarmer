import { Tabs, useRouter } from "expo-router";
import { Pressable, Text } from "react-native";
import { NavigationIcon, type NavigationIconName } from "@/components/navigation-icon";
import { COLORS } from "@/components/swarm-ui";

const icon = (name: NavigationIconName) => function TabIcon({ color, size }: { color: string; size: number }) {
  return <NavigationIcon name={name} color={color} size={size} />;
};

export default function MainLayout() {
  const router = useRouter();
  const back = (path: "/swarm" | "/tasks" | "/settings") => function BackToSection() {
    return <Pressable accessibilityRole="button" accessibilityLabel="Retour" onPress={() => router.replace(path)} style={{ padding: 12, minHeight: 44 }}><Text style={{ color: COLORS.accent, fontSize: 16 }}>‹ Retour</Text></Pressable>;
  };
  return (
    <Tabs screenOptions={{
      headerShown: true,
      headerStyle: { backgroundColor: COLORS.background },
      headerTintColor: COLORS.text,
      headerShadowVisible: false,
      headerTitleAlign: "left",
      headerTitleStyle: { fontSize: 24, fontWeight: "800" },
      sceneStyle: { backgroundColor: COLORS.background },
      tabBarActiveTintColor: COLORS.accent,
      tabBarInactiveTintColor: COLORS.subtle,
      tabBarLabelStyle: { fontSize: 11, fontWeight: "600" },
      tabBarItemStyle: { paddingTop: 4 },
      tabBarStyle: { minHeight: 64, backgroundColor: COLORS.panel, borderTopColor: "#2c3e43" },
    }}>
      <Tabs.Screen name="index" options={{ title: "monGARS", tabBarLabel: "Assistant", tabBarIcon: icon("assistant") }} />
      <Tabs.Screen name="tasks" options={{ title: "Activité", tabBarIcon: icon("activity") }} />
      <Tabs.Screen name="swarm" options={{ title: "Équipe", tabBarIcon: icon("team") }} />
      <Tabs.Screen name="settings" options={{ title: "Réglages", tabBarIcon: icon("settings") }} />
      <Tabs.Screen name="approvals" options={{ href: null, title: "Autorisations", headerLeft: back("/tasks") }} />
      <Tabs.Screen name="memory" options={{ href: null, title: "Mémoire", headerLeft: back("/settings") }} />
      <Tabs.Screen name="agents" options={{ href: null, title: "Agents connectés", headerLeft: back("/swarm") }} />
      <Tabs.Screen name="catalog" options={{ href: null, title: "Compétences", headerLeft: back("/swarm") }} />
    </Tabs>
  );
}
