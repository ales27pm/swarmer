import type { PropsWithChildren } from "react";
import { RefreshControl, ScrollView, Text, View } from "react-native";

import { COLORS } from "@/components/swarm-ui";

export function ScreenShell({
  title,
  subtitle,
  children,
  onRefresh,
  refreshing = false,
  testID,
}: PropsWithChildren<{
  title: string;
  subtitle?: string;
  onRefresh?: () => void;
  refreshing?: boolean;
  testID?: string;
}>) {
  return (
    <ScrollView
      accessibilityLanguage="fr-FR"
      style={{ backgroundColor: COLORS.background }}
      testID={testID}
      contentInsetAdjustmentBehavior="automatic"
      contentContainerStyle={{ gap: 16, padding: 16, paddingBottom: 40 }}
      keyboardShouldPersistTaps="handled"
      refreshControl={
        onRefresh ? (
          <RefreshControl
            refreshing={refreshing}
            onRefresh={onRefresh}
            tintColor={COLORS.accent}
          />
        ) : undefined
      }
    >
      <View style={{ gap: 6 }}>
        <Text accessibilityRole="header" selectable style={{ color: COLORS.text, fontSize: 28, fontWeight: "800" }}>
          {title}
        </Text>
        {subtitle ? (
          <Text selectable style={{ color: COLORS.muted, fontSize: 14, lineHeight: 20 }}>
            {subtitle}
          </Text>
        ) : null}
      </View>
      {children}
    </ScrollView>
  );
}
