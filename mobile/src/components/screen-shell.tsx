import type { PropsWithChildren } from "react";
import { ScrollView, Text, View } from "react-native";

export function ScreenShell({ title, children }: PropsWithChildren<{ title: string }>) {
  return (
    <ScrollView
      contentInsetAdjustmentBehavior="automatic"
      contentContainerStyle={{ padding: 20, gap: 16 }}
    >
      <View style={{ gap: 6 }}>
        <Text selectable style={{ fontSize: 28, fontWeight: "700" }}>
          {title}
        </Text>
      </View>
      {children}
    </ScrollView>
  );
}
