import { useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";

import { ScreenShell } from "@/components/screen-shell";
import { createTask, planTask } from "@/lib/api/client";

export default function ChatScreen() {
  const [input, setInput] = useState("");
  const [status, setStatus] = useState("Prêt");
  const [busy, setBusy] = useState(false);

  async function submit() {
    const value = input.trim();
    if (!value || busy) return;

    setBusy(true);
    setStatus("Création de la tâche…");
    try {
      const task = await createTask(value);
      setInput("");
      setStatus(`Orchestration de ${task.id}…`);
      const result = await planTask(task.id);

      if ("status" in result && result.status === "waiting_permission") {
        setStatus(`Permission requise: ${result.summary}`);
      } else if ("status" in result && result.status === "completed") {
        setStatus(`Terminé: ${result.summary}`);
      } else {
        setStatus(`Tâche ${task.id} traitée par l'orchestrateur`);
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Erreur réseau");
    } finally {
      setBusy(false);
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
        editable={!busy}
        style={{ minHeight: 120, borderWidth: 1, borderRadius: 16, padding: 14, textAlignVertical: "top" }}
      />
      <View style={{ alignItems: "flex-start" }}>
        <Pressable
          disabled={busy}
          onPress={submit}
          style={{ borderWidth: 1, borderRadius: 999, paddingHorizontal: 18, paddingVertical: 12, opacity: busy ? 0.5 : 1 }}
        >
          <Text selectable>{busy ? "Orchestration…" : "Envoyer"}</Text>
        </Pressable>
      </View>
    </ScreenShell>
  );
}
