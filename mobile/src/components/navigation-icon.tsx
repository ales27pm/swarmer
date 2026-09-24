import { View } from "react-native";
import Svg, { Circle, Path, Rect } from "react-native-svg";

export type NavigationIconName = "assistant" | "activity" | "team" | "settings";
/** Local vector icons remain legible at tab-bar size, including on Android. */
export function NavigationIcon({ name, color, size = 24 }: { name: NavigationIconName; color: string; size?: number }) {
  return (
    <View accessibilityElementsHidden importantForAccessibility="no-hide-descendants"><Svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
      {name === "assistant" ? <><Path d="M20 11.5a8 8 0 0 1-8 8H5l-3 2v-10a8 8 0 1 1 18 0Z" /><Path d="m12 6 1.3 3.7L17 11l-3.7 1.3L12 16l-1.3-3.7L7 11l3.7-1.3Z" /></> : null}
      {name === "activity" ? <><Rect x={4} y={3} width={16} height={18} rx={4}/><Path d="m7 13 3-3 3 5 4-6M8 6h4" /></> : null}
      {name === "team" ? <><Circle cx={12} cy={7} r={3}/><Path d="M6 21v-3a6 6 0 0 1 12 0v3M4 6a3 3 0 0 0 0 6M20 6a3 3 0 0 1 0 6M2 20v-3a4 4 0 0 1 3-4M22 20v-3a4 4 0 0 0-3-4"/></> : null}
      {name === "settings" ? <><Path d="M4 6h16M4 12h16M4 18h16"/><Circle cx={8} cy={6} r={2} fill={color}/><Circle cx={16} cy={12} r={2} fill={color}/><Circle cx={10} cy={18} r={2} fill={color}/></> : null}
    </Svg></View>
  );
}
