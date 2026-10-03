import { Text, TextInput, View } from "react-native";
import { ActionButton, COLORS } from "./swarm-ui";

export type SymbolicSearchSelection = { scope: string; namespace: string; scheme_id: string };

/** Owner opt-in: catalogs are chosen explicitly, never inferred from memory text. */
export function SymbolicMemorySearchControls({ value, onChange }: {
  value: SymbolicSearchSelection | null;
  onChange: (value: SymbolicSearchSelection | null) => void;
}) {
  return (
    <View style={{ gap: 8 }}>
      <ActionButton
        label={value ? "Désactiver la recherche par concepts" : "Activer la recherche par concepts"}
        onPress={() => onChange(value ? null : { scope: "general", namespace: "", scheme_id: "" })}
        testID="memory-symbolic-toggle"
      />
      {value ? (
        <>
          <Text style={{ color: COLORS.muted }}>Choisissez la portée et le catalogue de concepts à ajouter à votre recherche.</Text>
          {([
            ["scope", "Portée", "general ou project:identifiant"],
            ["namespace", "Espace du catalogue", "software"],
            ["scheme_id", "Identifiant du catalogue", "engineering"],
          ] as const).map(([key, label, placeholder]) => (
            <View key={key} style={{ gap: 4 }}>
              <Text style={{ color: COLORS.muted }}>{label}</Text>
              <TextInput
                accessibilityLabel={label} autoCapitalize="none" autoCorrect={false}
                value={value[key]} onChangeText={(text) => onChange({ ...value, [key]: text })}
                placeholder={placeholder} placeholderTextColor={COLORS.subtle}
                testID={`memory-symbolic-${key}`}
                style={{ backgroundColor: COLORS.panel, borderColor: COLORS.border, borderWidth: 1,
                  borderRadius: 12, color: COLORS.text, minHeight: 46, paddingHorizontal: 13 }}
              />
            </View>
          ))}
        </>
      ) : null}
    </View>
  );
}
