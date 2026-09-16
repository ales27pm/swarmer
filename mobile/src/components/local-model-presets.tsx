import { Linking, Text, View } from "react-native";

import { ActionButton, Card, COLORS, SectionTitle } from "@/components/swarm-ui";
import { LOCAL_MODEL_PRESETS, presetSourceUrl } from "@/lib/local-model-presets";
import type { LocalInferenceRuntime } from "@/lib/application-api/local-inference";

export function LocalModelPresets({ runtime, disabled, onApply, onError }: {
  runtime: LocalInferenceRuntime;
  disabled: boolean;
  onApply: () => void;
  onError: (message: string) => void;
}) {
  const preset = LOCAL_MODEL_PRESETS[runtime];
  return (
    <>
      <SectionTitle title="Dolphin pour l’iPhone" />
      <Card>
        <Text selectable style={{ color: COLORS.text, fontSize: 16, fontWeight: "800" }}>{preset.name}</Text>
        <Text selectable style={{ color: COLORS.accent, fontWeight: "700" }}>
          {preset.quantization} · {preset.downloadSize}
        </Text>
        <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>{preset.detail}</Text>
        {preset.filename ? <Text selectable style={{ color: COLORS.subtle, fontSize: 12 }}>{preset.filename}</Text> : null}
        <Text selectable style={{ color: COLORS.subtle, fontSize: 12, lineHeight: 18 }}>
          Dolphin est un modèle non censuré, sans ablitération déclarée. La mémoire utilisée dépasse la taille des poids; un seul modèle reste chargé à la fois.
        </Text>
        <View style={{ gap: 8 }}>
          {runtime === "mlx" ? (
            <ActionButton disabled={disabled} label="Utiliser Dolphin MLX par défaut" onPress={onApply} />
          ) : null}
          <ActionButton
            label="Voir le modèle et sa licence sur Hugging Face"
            onPress={() => void Linking.openURL(presetSourceUrl(preset)).catch(() => onError("Impossible d’ouvrir la fiche Hugging Face."))}
          />
        </View>
      </Card>
    </>
  );
}
