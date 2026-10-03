import { useEffect, useRef, useState, useSyncExternalStore } from "react";
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

type DownloadSnapshot = {
  pending: boolean;
  cancelling: boolean;
  progress: Progress | null;
  progressError: string | null;
  error: string | null;
  notice: string | null;
  imported: LocalModel | null;
};
type DownloadSession = { snapshot: DownloadSnapshot; delivered: boolean };

// Native downloads outlive a screen. Keep the original promise's receipt (at
// most one) and let screens observe it, rather than launching/adopting another
// import from global byte counts, which contain no model or operation identity.
let downloadSession: DownloadSession | null = null;
const observers = new Set<() => void>();
const downloadSnapshot = () => downloadSession?.snapshot ?? null;
const downloadActive = () => Boolean(downloadSession?.snapshot.pending || downloadSession?.snapshot.cancelling);
function subscribeDownload(observer: () => void) {
  observers.add(observer);
  return () => { observers.delete(observer); };
}
function publishDownload(session: DownloadSession, update: Partial<DownloadSnapshot>) {
  if (downloadSession !== session) return;
  session.snapshot = { ...session.snapshot, ...update };
  observers.forEach((observer) => observer());
}
function clearFinishedDownload() {
  if (downloadActive()) return;
  downloadSession = null;
  observers.forEach((observer) => observer());
}
async function startDownload(choice: HuggingFaceModelChoice) {
  const session: DownloadSession = { delivered: false, snapshot: {
    pending: true, cancelling: false, progress: null, progressError: null,
    error: null, notice: null, imported: null,
  } };
  downloadSession = session;
  publishDownload(session, {});
  try {
    const imported = await downloadHuggingFaceModel(choice.plan);
    if (imported.runtime !== choice.plan.runtime) throw new Error("Le format importé ne correspond pas au moteur demandé.");
    publishDownload(session, { imported, error: null, notice: imported.purpose === "embeddings"
      ? `${imported.displayName} est téléchargé et vérifié. C’est un modèle d’embeddings, pas un modèle de génération.`
      : `${imported.displayName} est téléchargé et vérifié. Tu peux maintenant le charger.` });
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : String(cause);
    publishDownload(session, /cancel|annul/i.test(message)
      ? { notice: "Téléchargement annulé." } : { error: message });
  } finally {
    publishDownload(session, { pending: false, progressError: null });
  }
}

/** Resolve a public repository before starting a user-selected, pinned download. */
export function HuggingFaceModelDownload({ runtime, disabled, onBusyChange, onImported }: {
  runtime: LocalInferenceRuntime;
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
  onImported: (model: LocalModel) => void;
}) {
  const [address, setAddress] = useState("");
  const [choices, setChoices] = useState<HuggingFaceModelChoice[]>([]);
  const [resolving, setResolving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const download = useSyncExternalStore(subscribeDownload, downloadSnapshot, downloadSnapshot);
  const downloading = Boolean(download?.pending || download?.cancelling);
  const phase = resolving ? "resolving" : downloading ? "downloading" : "idle";
  const progress = download?.progress;
  const cancelling = download?.cancelling ?? false;
  const live = useRef(true);
  const resolver = useRef<AbortController | null>(null);
  const callbacks = useRef({ onBusyChange, onImported });
  callbacks.current = { onBusyChange, onImported };

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      resolver.current?.abort();
      callbacks.current.onBusyChange(false);
      // A native download owns its own lifetime and commits only a complete import.
      // Navigating away must not cancel a later operation owned by another screen.
    };
  }, []);

  useEffect(() => {
    callbacks.current.onBusyChange(resolving || downloading);
  }, [resolving, downloading]);

  useEffect(() => {
    const session = downloadSession;
    if (!session || downloading || session.delivered || !session.snapshot.imported) return;
    // Consume before notifying: a callback can synchronously change the runtime
    // (and remount this component) or another screen can observe the same result.
    session.delivered = true;
    setChoices([]);
    callbacks.current.onImported(session.snapshot.imported);
  }, [download, downloading]);

  useEffect(() => {
    const session = downloadSession;
    if (!downloading || !session) return;
    let active = true;
    let reading = false;
    const refresh = async () => {
      if (reading) return;
      reading = true;
      try {
        const next = await getModelDownloadProgress();
        if (active && session.snapshot.pending) publishDownload(session, { progress: next, progressError: null });
      } catch {
        // Losing progress is not completion. Keep the original promise and
        // cancellation available; a later read can recover without a new import.
        if (active && session.snapshot.pending) publishDownload(session, {
          progressError: "Le suivi du téléchargement est temporairement indisponible. Le téléchargement n’est pas relancé.",
        });
      } finally { reading = false; }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 500);
    return () => { active = false; clearInterval(timer); };
  }, [downloading]);

  async function resolve() {
    if (disabled || resolver.current || downloadActive() || !address.trim()) return;
    const controller = new AbortController();
    resolver.current = controller;
    clearFinishedDownload();
    setResolving(true);
    setError(null); setNotice(null); setChoices([]);
    try {
      const found = await resolveHuggingFaceModels(address.trim(), runtime, controller.signal);
      if (live.current && resolver.current === controller && !controller.signal.aborted) setChoices(found);
    } catch (cause) {
      if (live.current && resolver.current === controller && !controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Impossible de lire ce dépôt Hugging Face.");
    } finally {
      if (resolver.current === controller) {
        resolver.current = null;
        if (live.current) setResolving(false);
      }
    }
  }

  function downloadChoice(choice: HuggingFaceModelChoice) {
    if (disabled || resolver.current || downloadActive()) return;
    setError(null); setNotice(null);
    void startDownload(choice);
  }

  async function cancel() {
    if (resolver.current) {
      resolver.current?.abort();
      setNotice("Recherche annulée.");
      return;
    }
    const session = downloadSession;
    if (!session?.snapshot.pending || session.snapshot.cancelling) return;
    publishDownload(session, { cancelling: true, error: null });
    try {
      await cancelHuggingFaceModelDownload();
      if (session.snapshot.pending) publishDownload(session, { notice: "Annulation demandée…" });
    } catch (cause) {
      if (session.snapshot.pending) publishDownload(session, { error: cause instanceof Error ? cause.message : "Impossible d’annuler le téléchargement." });
    } finally {
      // Do not admit a successor until this cancellation call has settled.
      publishDownload(session, { cancelling: false });
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
        <ActionButton accessibilityLabel={`Télécharger ${choice.label}`} label="Télécharger cette variante" disabled={disabled} onPress={() => downloadChoice(choice)} />
      </View>) : null}
      {choices.length > 0 && !active ? <Text style={{ color: COLORS.subtle, lineHeight: 19 }}>Format détecté. La compatibilité avec ton iPhone sera confirmée au chargement. Aucun modèle ne sera chargé automatiquement.</Text> : null}
      {notice || download?.notice ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.accent }}>{notice ?? download?.notice}</Text> : null}
      {error || download?.error || download?.progressError ? <ErrorBanner message={error ?? download?.error ?? download?.progressError ?? null} /> : null}
    </Card>
  </>;
}
