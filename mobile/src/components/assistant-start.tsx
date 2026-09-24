import { Image, Pressable, Text, View, useWindowDimensions } from "react-native";
import Svg, { Path, Rect } from "react-native-svg";

import { KeyboardInputGroup, KeyboardTextInput } from "@/components/screen-shell";
import { ActionButton, COLORS, SectionTitle } from "@/components/swarm-ui";

const SUGGESTIONS = [
  { label: "Organiser ma semaine", draft: "Aide-moi à organiser ma semaine.", icon: "calendar" },
  { label: "Résumer un projet", draft: "Prépare un résumé clair de mon projet.", icon: "document" },
] as const;

export function AssistantWelcome() {
  const { width, fontScale } = useWindowDimensions();
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: 14 }}>
      <View style={{ flex: 1, gap: 8 }}>
        <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 23, lineHeight: 29, fontWeight: "700" }}>
          Qu’est-ce qu’on avance aujourd’hui ?
        </Text>
        <Text style={{ color: COLORS.muted, lineHeight: 20 }}>Une idée, une recherche, un projet.</Text>
      </View>
      {width >= 360 && fontScale < 1.4 ? (
        <Image source={require("../../assets/illustrations/assistant-compass.png")} style={{ width: 76, height: 76, borderRadius: 18 }} resizeMode="cover" accessibilityIgnoresInvertColors accessible={false} />
      ) : null}
    </View>
  );
}

function SuggestionIcon({ name }: { name: "calendar" | "document" }) {
  return (
    <View accessibilityElementsHidden importantForAccessibility="no-hide-descendants">
      <Svg width={22} height={22} viewBox="0 0 24 24" fill="none" stroke={COLORS.muted} strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
        {name === "calendar" ? <><Rect x={3} y={5} width={18} height={16} rx={3} /><Path d="M7 3v4M17 3v4M3 11h18" /></> : <><Path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Z M14 3v6h6M8 13h8M8 17h5" /></>}
      </Svg>
    </View>
  );
}

export function AssistantSuggestions({ onSelect, disabled }: { onSelect: (draft: string) => void; disabled: boolean }) {
  return (
    <View style={{ gap: 8 }} testID="assistant-suggestions">
      <SectionTitle title="Pour commencer" />
      {SUGGESTIONS.map(({ label, draft, icon }) => (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={label}
          accessibilityHint="Prépare un brouillon que tu peux modifier avant de l’envoyer."
          accessibilityState={{ disabled }}
          disabled={disabled}
          key={label}
          onPress={() => onSelect(draft)}
          style={({ pressed }) => ({
            backgroundColor: COLORS.panel, borderColor: COLORS.border, borderRadius: 14, borderWidth: 1,
            flexDirection: "row", alignItems: "center", gap: 12, minHeight: 48,
            opacity: disabled ? 0.5 : pressed ? 0.75 : 1, paddingHorizontal: 14, paddingVertical: 12,
          })}
        >
          <SuggestionIcon name={icon} />
          <Text style={{ color: COLORS.text, flex: 1, lineHeight: 20 }}>{label}</Text>
          <Text accessible={false} style={{ color: COLORS.subtle, fontSize: 22 }}>›</Text>
        </Pressable>
      ))}
    </View>
  );
}

type IntentComposerProps = {
  input: string;
  interactionMode: "chat" | "task";
  busy: boolean;
  authenticated: boolean;
  notice: string;
  onChangeInput: (input: string) => void;
  onChangeMode: (mode: "chat" | "task") => void;
  onSubmit: () => void;
  onOpenSettings: () => void;
};

export function IntentComposer({ input, interactionMode, busy, authenticated, notice, onChangeInput, onChangeMode, onSubmit, onOpenSettings }: IntentComposerProps) {
  return (
    <View style={{ backgroundColor: COLORS.panel, borderRadius: 20, borderColor: COLORS.border, borderWidth: 1, padding: 16, gap: 12 }} testID="assistant-composer">
      <View style={{ flexDirection: "row", gap: 4, backgroundColor: COLORS.background, padding: 4, borderRadius: 14 }}>
        {(["chat", "task"] as const).map((mode) => (
          <Pressable
            accessibilityRole="button"
            accessibilityState={{ selected: interactionMode === mode, disabled: busy }}
            disabled={busy}
            key={mode}
            onPress={() => onChangeMode(mode)}
            style={({ pressed }) => ({
              backgroundColor: interactionMode === mode ? COLORS.panelRaised : "transparent",
              borderColor: interactionMode === mode ? COLORS.accent : "transparent",
              borderRadius: 10, borderWidth: 1, flex: 1, minHeight: 44,
              justifyContent: "center", paddingVertical: 10, paddingHorizontal: 6, opacity: pressed || busy ? 0.6 : 1,
            })}
            testID={`interaction-mode-${mode}`}
          >
            <Text style={{ color: interactionMode === mode ? COLORS.accent : COLORS.muted, fontWeight: "700", textAlign: "center" }}>
              {mode === "chat" ? "Discuter" : "Confier une tâche"}
            </Text>
          </Pressable>
        ))}
      </View>
      <Text style={{ color: COLORS.muted, fontSize: 13, lineHeight: 19 }}>
        {interactionMode === "chat" ? "Échange avec ton assistant, sans lancer de tâche." : "Décris le résultat souhaité. Ton équipe préparera un plan."}
      </Text>
      <KeyboardInputGroup dismissKeyboard testID="chat-composer-controls">
        <KeyboardTextInput
          accessibilityLabel="Demande pour l’assistant"
          editable={!busy}
          multiline
          onChangeText={onChangeInput}
          placeholder={interactionMode === "chat" ? "Écris ta demande…" : "Qu’aimerais-tu accomplir ?"}
          placeholderTextColor={COLORS.subtle}
          style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 14, borderWidth: 1, color: COLORS.text, minHeight: 112, fontSize: 16, lineHeight: 22, padding: 14, textAlignVertical: "top" }}
          testID="chat-input"
          value={input}
        />
        <ActionButton busy={busy} disabled={!input.trim() || !authenticated} label="Envoyer" onPress={onSubmit} testID="send-button" variant="accent" />
      </KeyboardInputGroup>
      {!authenticated ? (
        <Pressable accessibilityRole="button" onPress={onOpenSettings} style={{ minHeight: 44, justifyContent: "center", alignItems: "center" }}>
          <Text style={{ color: COLORS.accent, fontSize: 13, lineHeight: 19, textAlign: "center", textDecorationLine: "underline" }}>Connecter le serveur pour envoyer</Text>
        </Pressable>
      ) : null}
      {notice ? <Text accessibilityLiveRegion="polite" selectable style={{ color: COLORS.muted, lineHeight: 19 }}>{notice}</Text> : null}
    </View>
  );
}
