import { useEffect, useRef, useState } from "react";
import { Text, TextInput, View } from "react-native";

import { ActionButton, Card, COLORS, ErrorBanner, SectionTitle } from "@/components/swarm-ui";
import {
  cancelHuggingFaceModelDownload,
  downloadHuggingFaceModel,
  getModelDownloadProgress,
  type LocalInferenceRuntime,
  type LocalModel,
} from "@/lib/application-api/local-inference";
import { resolveHuggingFaceModels, type HuggingFaceModelChoice } from "@/lib/hugging-face-models";

function size(bytes: number): string {
  if (bytes < 1_000_000) return `${Math.ceil(bytes / 1_000)} ko`;
  if (bytes < 1_000_000_000) return `${(bytes / 1_000_000).toFixed(1)} Mo`;
  return `${(bytes / 1_000_000_000).toFixed(2)} Go`;
}

type Progress = Awaited<ReturnType<typeof getModelDownloadProgress>>;

/** Resolve a public repository before starting a user-selected, pinned download. */
export function HuggingFaceModelDownload({ runtime, disabled, onBusyChange, onImported }: {
  runtime: LocalInferenceRuntime;
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
  onImported: (model: LocalModel) => void;
}) {
  const [address, setAddress] = useState("");
  const [choices, setChoices] = useState<HuggingFaceModelChoice[]>([]);
  const [phase, setPhase] = useState<"idle" | "resolving" | "downloading">("idle");
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const live = useRef(true);
  const operation = useRef<"resolving" | "downloading" | null>(null);
  const resolver = useRef<AbortController | null>(null);
  const callbacks = useRef({ onBusyChange, onImported });
  callbacks.current = { onBusyChange, onImported };

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      resolver.current?.abort();
      if (operation.current) callbacks.current.onBusyChange(false);
      // A native download owns its own lifetime and commits only a complete import.
      // Navigating away must not cancel a later operation owned by another screen.
    };
  }, []);

  useEffect(() => {
    if (phase !== "downloading") return;
    let active = true;
    let reading = false;
    const refresh = async () => {
      if (reading) return;
      reading = true;
      try {
        const next = await getModelDownloadProgress();
        if (active) setProgress(next);
      } catch {
        // Progress is optional feedback; the actual download promise is authoritative.
      } finally { reading = false; }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 500);
    return () => { active = false; clearInterval(timer); };
  }, [phase]);

  function finish() {
    operation.current = null;
    if (!live.current) return;
    setPhase("idle");
    setCancelling(false);
    callbacks.current.onBusyChange(false);
  }

  async function resolve() {
    if (disabled || operation.current || !address.trim()) return;
    operation.current = "resolving";
    const controller = new AbortController();
    resolver.current = controller;
    setPhase("resolving");
    setError(null); setNotice(null); setChoices([]);
    callbacks.current.onBusyChange(true);
    try {
      const found = await resolveHuggingFaceModels(address.trim(), runtime, controller.signal);
      if (live.current && !controller.signal.aborted) setChoices(found);
    } catch (cause) {
      if (live.current && !controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Impossible de lire ce dépôt Hugging Face.");
    } finally {
      resolver.current = null;
      finish();
    }
  }

  async function download(choice: HuggingFaceModelChoice) {
    if (disabled || operation.current) return;
    operation.current = "downloading";
    setPhase("downloading"); setProgress(null); setCancelling(false);
    setError(null); setNotice(null);
    callbacks.current.onBusyChange(true);
    try {
      const imported = await downloadHuggingFaceModel(choice.plan);
      if (!live.current) return;
      if (imported.runtime !== runtime) throw new Error("Le format importé ne correspond pas au moteur sélectionné.");
      callbacks.current.onImported(imported);
      setChoices([]);
      setNotice(imported.purpose === "embeddings"
        ? `${imported.displayName} est téléchargé et vérifié. C’est un modèle d’embeddings, pas un modèle de génération.`
        : `${imported.displayName} est téléchargé et vérifié. Tu peux maintenant le charger.`);
    } catch (cause) {
      if (!live.current) return;
      const message = cause instanceof Error ? cause.message : String(cause);
      if (/cancel|annul/i.test(message)) setNotice("Téléchargement annulé.");
      else setError(message);
    } finally { finish(); }
  }

  async function cancel() {
    if (operation.current === "resolving") {
      resolver.current?.abort();
      setNotice("Recherche annulée.");
      return;
    }
    if (operation.current !== "downloading" || cancelling) return;
    setCancelling(true);
    try {
      await cancelHuggingFaceModelDownload();
      if (live.current) setNotice("Annulation demandée…");
    } catch (cause) {
      if (live.current) {
        setCancelling(false);
        setError(cause instanceof Error ? cause.message : "Impossible d’annuler le téléchargement.");
      }
    }
  }

  const active = phase !== "idle";
  const percent = progress && progress.totalBytes > 0
    ? Math.min(100, Math.floor(progress.downloadedBytes / progress.totalBytes * 100)) : null;
  return <>
    <SectionTitle title="Ajouter depuis Hugging Face" />
    <Card testID="hugging-face-model-download">
      <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
        Colle l’adresse d’un dépôt public ou d’un modèle. Choisis ensuite la variante à télécharger pour {runtime === "llama.cpp" ? "GGUF" : runtime === "coreml" ? "Core ML" : "MLX"}.
      </Text>
      <TextInput
        accessibilityLabel="Adresse Hugging Face du modèle"
        placeholder="https://huggingface.co/organisation/modele"
        placeholderTextColor={COLORS.subtle}
        autoCapitalize="none" autoCorrect={false} keyboardType="url"
        editable={!disabled && !active} value={address} maxLength={2048}
        onChangeText={(text) => { setAddress(text); setChoices([]); setError(null); setNotice(null); }}
        style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 12, borderWidth: 1, color: COLORS.text, minHeight: 48, paddingHorizontal: 12 }}
      />
      {!active ? <ActionButton
        label="Rechercher les modèles" disabled={disabled || !address.trim()}
        onPress={() => void resolve()}
      /> : null}
      {phase === "resolving" ? <Text style={{ color: COLORS.muted }}>Lecture du dépôt Hugging Face…</Text> : null}
      {phase === "downloading" ? <View style={{ gap: 8 }}>
        <Text accessibilityLiveRegion="polite" style={{ color: COLORS.text }}>
          {cancelling ? "Annulation en cours…" : progress?.state === "verifying" ? "Vérification des fichiers…" : progress?.state === "importing" ? "Importation dans les modèles locaux…" : `Téléchargement${percent !== null ? ` : ${percent} %` : "…"}`}
        </Text>
        {progress && progress.totalBytes > 0 ? <>
          <View accessibilityRole="progressbar" accessibilityValue={{ min: 0, max: 100, now: percent ?? 0 }} style={{ height: 6, backgroundColor: COLORS.border, borderRadius: 3 }}>
            <View style={{ height: 6, width: `${percent ?? 0}%`, backgroundColor: COLORS.accent, borderRadius: 3 }} />
          </View>
          <Text style={{ color: COLORS.muted }}>{size(progress.downloadedBytes)} / {size(progress.totalBytes)} · {progress.completedFiles}/{progress.totalFiles} fichiers</Text>
        </> : null}
        <Text style={{ color: COLORS.subtle }}>Garde l’app au premier plan jusqu’à la fin de l’importation.</Text>
      </View> : null}
      {active ? <ActionButton label={phase === "resolving" ? "Annuler la recherche" : "Annuler le téléchargement Hugging Face"} disabled={cancelling} onPress={() => void cancel()} /> : null}
      {!active ? choices.map((choice) => <View key={choice.id} style={{ gap: 6, paddingVertical: 8 }}>
        <Text selectable style={{ color: COLORS.text, fontWeight: "700" }}>{choice.label}</Text>
        <Text style={{ color: COLORS.muted }}>{size(choice.sizeBytes)} · {choice.plan.files.length} fichiers</Text>
        <ActionButton accessibilityLabel={`Télécharger ${choice.label}`} label="Télécharger cette variante" disabled={disabled} onPress={() => void download(choice)} />
      </View>) : null}
      {choices.length > 0 && !active ? <Text style={{ color: COLORS.subtle, lineHeight: 19 }}>Format détecté. La compatibilité avec ton iPhone sera confirmée au chargement. Aucun modèle ne sera chargé automatiquement.</Text> : null}
      {notice ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.accent }}>{notice}</Text> : null}
      {error ? <ErrorBanner message={error} /> : null}
    </Card>
  </>;
}
