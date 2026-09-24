import { useState, type PropsWithChildren } from "react";
import { Pressable, Text, View } from "react-native";

import { Card, COLORS } from "@/components/swarm-ui";

function SettingsRowContent({ title, description, indicator }: { title: string; description: string; indicator: string }) {
  return <>
    <View style={{ flex: 1, gap: 4 }}>
      <Text style={{ color: COLORS.text, fontSize: 15, fontWeight: "600" }}>{title}</Text>
      <Text style={{ color: COLORS.muted, fontSize: 13, lineHeight: 18 }}>{description}</Text>
    </View>
    <Text accessible={false} style={{ color: COLORS.muted, fontSize: 22 }}>{indicator}</Text>
  </>;
}

export function SettingsDisclosure({
  children,
  description,
  expanded: controlledExpanded,
  onToggle,
  testID,
  title,
}: PropsWithChildren<{
  description: string;
  expanded?: boolean;
  onToggle?: () => void;
  testID: string;
  title: string;
}>) {
  const [open, setOpen] = useState(false);
  const expanded = controlledExpanded ?? open;
  return (
    <Card style={{ gap: 0, padding: 0 }}>
      <Pressable
        accessibilityHint={description}
        accessibilityLabel={title}
        accessibilityRole="button"
        accessibilityState={{ expanded }}
        onPress={onToggle ?? (() => setOpen((value) => !value))}
        style={({ pressed }) => ({ alignItems: "center", flexDirection: "row", gap: 12, minHeight: 64, opacity: pressed ? 0.7 : 1, padding: 16 })}
        testID={testID}
      >
        <SettingsRowContent title={title} description={description} indicator={expanded ? "−" : "+"} />
      </Pressable>
      <View
        accessibilityElementsHidden={!expanded}
        importantForAccessibility={expanded ? "auto" : "no-hide-descendants"}
        style={{ borderTopColor: COLORS.border, borderTopWidth: 0.5, display: expanded ? "flex" : "none", gap: 12, padding: 16 }}
      >
        {children}
      </View>
    </Card>
  );
}

export function SettingsNavigationRow({ title, description, onPress, testID }: {
  title: string;
  description: string;
  onPress: () => void;
  testID: string;
}) {
  return (
    <Pressable
      accessibilityHint={description}
      accessibilityLabel={title}
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => ({ alignItems: "center", backgroundColor: COLORS.panel, borderColor: COLORS.border, borderRadius: 16, borderWidth: 0.5, flexDirection: "row", gap: 12, minHeight: 72, opacity: pressed ? 0.7 : 1, padding: 16 })}
      testID={testID}
    >
      <SettingsRowContent title={title} description={description} indicator="›" />
    </Pressable>
  );
}
