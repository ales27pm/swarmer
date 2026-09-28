import { Children, useState, type PropsWithChildren } from "react";
import { Pressable, Text, View } from "react-native";
import Svg, { Circle, Ellipse, Path, Rect } from "react-native-svg";

import { Card, COLORS } from "@/components/swarm-ui";

type SettingsIcon = "server" | "model" | "memory" | "team" | "authorization" | "journal";

function RowIcon({ name }: { name: SettingsIcon }) {
  return <View accessibilityElementsHidden importantForAccessibility="no-hide-descendants">
    <Svg width={24} height={24} viewBox="0 0 24 24" fill="none" stroke={COLORS.muted} strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
      {name === "server" ? <><Rect x={3} y={3} width={18} height={7} rx={2} /><Rect x={3} y={14} width={18} height={7} rx={2} /><Path d="M7 7h.01M7 18h.01" /></> : null}
      {name === "model" ? <><Rect x={6} y={6} width={12} height={12} rx={2} /><Path d="M9 3v3m6-3v3M9 18v3m6-3v3M3 9h3m-3 6h3m12-6h3m-3 6h3" /></> : null}
      {name === "memory" ? <><Ellipse cx={12} cy={5} rx={8} ry={3} /><Path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0" /></> : null}
      {name === "team" ? <><Circle cx={9} cy={7} r={3} /><Path d="M3 21v-3a6 6 0 0 1 12 0v3M17 4a3 3 0 0 1 0 6m1 3a5 5 0 0 1 3 5v3" /></> : null}
      {name === "authorization" ? <><Path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6l8-4Z" /><Path d="m8 12 3 3 5-6" /></> : null}
      {name === "journal" ? <><Path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8m-8 4h6" /></> : null}
    </Svg>
  </View>;
}

function SettingsRowContent({ title, description, expanded, icon }: { title: string; description?: string; expanded?: boolean; icon?: SettingsIcon }) {
  return <>
    {icon ? <RowIcon name={icon} /> : null}
    <View style={{ flex: 1, gap: 4 }}>
      <Text style={{ color: COLORS.text, fontSize: 16, fontWeight: "500" }}>{title}</Text>
      {description ? <Text style={{ color: COLORS.muted, fontSize: 12, lineHeight: 18 }}>{description}</Text> : null}
    </View>
    <View accessibilityElementsHidden importantForAccessibility="no-hide-descendants">
      <Svg width={18} height={18} viewBox="0 0 24 24" fill="none" stroke={COLORS.muted} strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
        <Path d={expanded ? "m6 9 6 6 6-6" : "m9 6 6 6-6 6"} />
      </Svg>
    </View>
  </>;
}

/** One container per category; rows share separators rather than nested cards. */
export function SettingsGroup({ children }: PropsWithChildren) {
  return <Card style={{ padding: 0, gap: 0, overflow: "hidden", borderRadius: 16 }}>
    {Children.toArray(children).map((child, index) => <View key={index} style={index ? { borderTopColor: COLORS.border, borderTopWidth: 0.5 } : undefined}>{child}</View>)}
  </Card>;
}

export function SettingsDisclosure({
  children, description, expanded: controlledExpanded, grouped = false, icon, onToggle, testID, title,
}: PropsWithChildren<{
  description?: string;
  expanded?: boolean;
  grouped?: boolean;
  icon?: SettingsIcon;
  onToggle?: () => void;
  testID: string;
  title: string;
}>) {
  const [open, setOpen] = useState(false);
  const expanded = controlledExpanded ?? open;
  const content = <>
    <Pressable
      accessibilityHint={description}
      accessibilityLabel={title}
      accessibilityRole="button"
      accessibilityState={{ expanded }}
      onPress={onToggle ?? (() => setOpen((value) => !value))}
      style={({ pressed }) => ({ alignItems: "center", flexDirection: "row", gap: 14, minHeight: 64, opacity: pressed ? 0.7 : 1, padding: 16 })}
      testID={testID}
    >
      <SettingsRowContent title={title} description={description} expanded={expanded} icon={icon} />
    </Pressable>
    <View
      accessibilityElementsHidden={!expanded}
      importantForAccessibility={expanded ? "auto" : "no-hide-descendants"}
      style={{ borderTopColor: COLORS.border, borderTopWidth: 0.5, display: expanded ? "flex" : "none", gap: 12, padding: 16 }}
    >
      {children}
    </View>
  </>;
  return grouped ? <View>{content}</View> : <Card style={{ gap: 0, padding: 0 }}>{content}</Card>;
}

export function SettingsNavigationRow({ title, description, onPress, testID, grouped = false, icon }: {
  title: string;
  description?: string;
  onPress: () => void;
  testID: string;
  grouped?: boolean;
  icon?: SettingsIcon;
}) {
  return <Pressable
    accessibilityHint={description}
    accessibilityLabel={title}
    accessibilityRole="button"
    onPress={onPress}
    style={({ pressed }) => ({ alignItems: "center", backgroundColor: COLORS.panel, borderColor: COLORS.border, borderRadius: grouped ? 0 : 16, borderWidth: grouped ? 0 : 0.5, flexDirection: "row", gap: 14, minHeight: 64, opacity: pressed ? 0.7 : 1, padding: 16 })}
    testID={testID}
  >
    <SettingsRowContent title={title} description={description} icon={icon} />
  </Pressable>;
}
