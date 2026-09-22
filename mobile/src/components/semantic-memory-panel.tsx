import { useState } from "react";
import { Text, TextInput, View } from "react-native";
import { ActionButton, Card, COLORS, ErrorBanner, SectionTitle } from "./swarm-ui";
import { invokeApplicationCommand } from "@/lib/application-api/registry";
import type { LocalEmbeddingResult, LocalEmbeddingStatus } from "@/lib/local-embeddings";

const E5 = { modelId: "intfloat/multilingual-e5-small", revision: "614241f622f53c4eeff9890bdc4f31cfecc418b3", experimental: true } as const;
const labels = { disabled: "Déchargé", loading: "Chargement", ready: "Prêt sur l’iPhone", embedding: "Calcul local", unloading: "Déchargement", failed: "Échec" };

export function SemanticMemoryPanel() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<LocalEmbeddingStatus | null>(null);
  const [server, setServer] = useState<Record<string, unknown> | null>(null);
  const [text, setText] = useState("");
  const [receipt, setReceipt] = useState<string | null>(null);
  async function run(action: () => Promise<void>) {
    setBusy(true); setError(null);
    try { await action(); } catch (cause) { setError(cause instanceof Error ? cause.message : "Opération indisponible."); }
    finally { setBusy(false); }
  }
  return <View testID="semantic-memory-panel">
    <SectionTitle title="Mémoire sémantique" />
    <Card>
      <ErrorBanner message={error} />
      <Text style={{ color: COLORS.muted }}>La mémoire partagée et les embeddings de l’iPhone ont des états distincts.</Text>
      <ActionButton label="Vérifier la mémoire serveur" disabled={busy} onPress={() => void run(async () => {
        setServer(await invokeApplicationCommand("memory.status", {}));
      })} testID="memory-status-refresh" />
      {server ? <Text selectable style={{ color: COLORS.text }}>
        {server.embedding_configured ? `Modèle configuré : ${String(server.embedding_model)}. Réponse du modèle à vérifier par une recherche.` : "Recherche lexicale : aucun fournisseur d’embeddings configuré."}
        {`\nContexte durable : ${server.context_enabled ? "activé" : "désactivé"}. Recherche hybride : ${server.hybrid_enabled ? "activée" : "désactivée"}.`}
      </Text> : null}
      <Text style={{ color: COLORS.text }}>E5 multilingue · 384 dimensions · expérimental</Text>
      <Text style={{ color: COLORS.muted }}>Le chargement télécharge une copie durable si nécessaire. Décharge le modèle de génération avant ce test. Ces vecteurs locaux ne remplacent pas automatiquement l’index du serveur.</Text>
      <Text style={{ color: COLORS.text }}>{status ? labels[status.state] : "État local non vérifié"}</Text>
      <ActionButton label="Vérifier le modèle local" disabled={busy} onPress={() => void run(async () => setStatus(await invokeApplicationCommand("embeddings.status", {})))} testID="embedding-status-refresh" />
      <ActionButton label="Charger E5 expérimental" disabled={busy || status?.state === "ready"} onPress={() => void run(async () => setStatus(await invokeApplicationCommand("embeddings.load", E5)))} testID="embedding-load" />
      <TextInput accessibilityLabel="Texte du test d’embeddings" value={text} onChangeText={setText} multiline maxLength={2000}
        style={{ color: COLORS.text, backgroundColor: COLORS.background, padding: 12, minHeight: 80 }} testID="embedding-test-text" />
      <ActionButton label="Tester les embeddings locaux" disabled={busy || status?.state !== "ready" || !text.trim()}
        onPress={() => void run(async () => {
          const result = await invokeApplicationCommand<LocalEmbeddingResult>("embeddings.generate", { texts: [text], kind: "query" });
          setReceipt(`${result.vectors.length} vecteur · ${result.dimensions} dimensions · calcul local terminé.`);
        })} testID="embedding-generate" />
      {receipt ? <Text style={{ color: COLORS.accent }}>{receipt}</Text> : null}
      <ActionButton label="Décharger E5" disabled={busy || !status || status.state === "disabled"}
        onPress={() => void run(async () => {
          await invokeApplicationCommand("embeddings.unload", {});
          setStatus(await invokeApplicationCommand("embeddings.status", {})); setReceipt(null);
        })} testID="embedding-unload" />
    </Card>
  </View>;
}
