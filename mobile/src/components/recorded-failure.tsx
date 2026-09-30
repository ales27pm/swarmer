import { useState } from "react";
import { Text, View } from "react-native";
import { ActionButton, COLORS } from "@/components/swarm-ui";
import { workOutcome } from "@/lib/work-outcome";

export function RecordedFailure({ reason }: { reason: string }) {
  const [expanded, setExpanded] = useState(false);
  const outcome = workOutcome(reason);
  if (!outcome) return <Text selectable style={{ color: COLORS.danger, lineHeight: 20 }}>{reason}</Text>;
  return <View style={{ gap: 8 }}>
    <Text style={{ color: COLORS.warning, fontWeight: "700" }}>{outcome.label}</Text>
    <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>{outcome.description}</Text>
    <ActionButton label={expanded ? "Masquer le diagnostic" : "Voir le diagnostic"} onPress={() => setExpanded(!expanded)} />
    {expanded ? <Text selectable style={{ color: COLORS.subtle }}>Code enregistré : {reason}</Text> : null}
  </View>;
}
