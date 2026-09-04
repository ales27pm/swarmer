import { useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";

import { ScreenShell } from "@/components/screen-shell";
import { createTask } from "@/lib/api/client";

export default function ChatScreen() {
  const [input, setInput] = useState("");
  const [status, setStatus] = useState("Prêt");

  async function submit() {
    const value = input.trim();
    if (!value) return;

    setStatus("Envoi…");
    try {
      const task = await createTask(value);
      setInput("");
      setStatus(`Task ${task.id} créée`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Erreur réseau");
    }
  }

  return (
    <ScreenShell title="monGARS">
      <Text selectable>{status}</Text>
      <TextInput
        multiline
        value={input}
        onChangeText={setInput}
        placeholder="Qu'est-ce qu'on fait?"
        style={{ minHeight: 120, borderWidth: 1, borderRadius: 16, padding: 14, textAlignVertical: "top" }}
      />
      <View style={{ alignItems: "flex-start" }}>
        <Pressable onPress={submit} style={{ borderWidth: 1, borderRadius: 999, paddingHorizontal: 18, paddingVertical: 12 }}>
          <Text selectable>Envoyer</Text>
        </Pressable>
      </View>
    </ScreenShell>
  );
}
