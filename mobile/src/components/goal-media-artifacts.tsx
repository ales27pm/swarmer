import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Image, Text, View } from "react-native";
import { useFocusEffect } from "expo-router";
import { setAudioModeAsync, useAudioPlayer, useAudioPlayerStatus } from "expo-audio";
import { ActionButton, Card, COLORS, ErrorBanner } from "@/components/swarm-ui";
import { getGoalMediaArtifacts, getGoalMediaBytes, type PlanNode } from "@/lib/api/client";
import type { GoalMediaArtifact } from "@/lib/api/goal-media";
import { createGoalMediaCache } from "@/lib/goal-media-cache";
import { subscribeConnectionChanges } from "@/lib/connection-events";

const isMediaNode = (node: PlanNode) => node.required_skill === "image.generate" || node.required_skill === "audio.synthesize";
let stopActiveMedia: (() => void) | undefined;

function AudioPreview({ uri }: { uri: string }) {
  const player = useAudioPlayer({ uri }, { updateInterval: 250 });
  const status = useAudioPlayerStatus(player);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const pending = useRef(false);
  const pause = useCallback(() => { try { player.pause(); } catch { /* Player may already be released. */ } }, [player]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      pause();
      if (stopActiveMedia === pause) stopActiveMedia = undefined;
    };
  }, [pause]);
  useEffect(() => {
    if (status.isLoaded) return;
    const timeout = setTimeout(() => setError("Cet audio ne peut pas être lu. Ouvrez-le de nouveau pour réessayer."), 15000);
    return () => clearTimeout(timeout);
  }, [status.isLoaded]);

  const toggle = async () => {
    if (pending.current || !status.isLoaded) return;
    if (player.playing) { player.pause(); return; }
    pending.current = true; setBusy(true); setError(null);
    try {
      await setAudioModeAsync({ allowsRecording: false, shouldPlayInBackground: false, playsInSilentMode: true, interruptionMode: "doNotMix" });
      if (!mounted.current) return;
      if (status.didJustFinish || player.currentTime >= player.duration) await player.seekTo(0);
      if (!mounted.current) return;
      stopActiveMedia?.();
      stopActiveMedia = pause;
      player.play();
    } catch {
      if (mounted.current) setError("La lecture audio n’a pas pu démarrer.");
    } finally {
      pending.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const failed = status.playbackState === "error" || status.playbackState === "failed";
  return <View style={{ gap: 8 }}>
    <Text style={{ color: COLORS.muted }} accessibilityLiveRegion="polite">
      {status.isLoaded ? `${Math.floor(status.currentTime)} / ${Math.ceil(status.duration)} s` : "Préparation de la lecture…"}
    </Text>
    <ActionButton label={status.playing ? "Pause" : "Lire l’audio"} onPress={() => void toggle()} disabled={!status.isLoaded || busy || failed} busy={busy} />
    <ErrorBanner message={failed ? "Cet audio ne peut pas être lu." : error} />
  </View>;
}

function MediaPreview({ artifact }: { artifact: GoalMediaArtifact }) {
  const [uri, setUri] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    const abort = new AbortController();
    let cache: ReturnType<typeof createGoalMediaCache> | undefined;
    setUri(null); setError(null);
    void (async () => {
      try {
        const bytes = await getGoalMediaBytes(artifact, () => current, abort.signal);
        if (!current) return;
        cache = createGoalMediaCache();
        setUri(cache.put(bytes, artifact));
      } catch {
        if (current) setError("Le média n’a pas pu être chargé et vérifié. Réessayez lorsque le serveur est disponible.");
      }
    })();
    return () => {
      current = false; abort.abort();
      try { cache?.dispose(); } catch { /* A leftover cache is removed on the next app launch. */ }
    };
  }, [artifact, retry]);
  return <View style={{ gap: 10 }}>
    {!uri && !error ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.muted }}>Chargement du média…</Text> : null}
    {uri && artifact.media_type === "image/png" ? <Image accessible accessibilityRole="image" accessibilityLabel="Image générée par ce projet"
      source={{ uri }} resizeMode="contain" style={{ width: "100%", aspectRatio: artifact.width! / artifact.height! }}
      onError={() => setError("L’image n’a pas pu être affichée.")} /> : null}
    {uri && artifact.media_type === "audio/wav" ? <AudioPreview key={uri} uri={uri} /> : null}
    <ErrorBanner message={error} />
    {error ? <ActionButton label="Réessayer ce média" onPress={() => setRetry((value) => value + 1)} /> : null}
  </View>;
}

function MediaResults({ goalId, jobIds }: { goalId: string; jobIds: string[] }) {
  const [artifacts, setArtifacts] = useState<GoalMediaArtifact[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const jobs = jobIds.join(",");
  useEffect(() => {
    let current = true;
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 30000);
    setLoading(true); setError(null); setArtifacts([]); setSelected(null);
    void getGoalMediaArtifacts(goalId, () => current, abort.signal).then((items) => {
      if (!current) return;
      const completed = new Set(jobs.split(","));
      const visible = items.filter((artifact) => completed.has(artifact.job_id));
      setArtifacts(visible); setSelected(visible[0]?.artifact_id ?? null);
    }).catch(() => {
      if (current) setError("Les médias ne sont pas disponibles. Vérifiez la connexion au serveur, puis réessayez.");
    }).finally(() => { clearTimeout(timeout); if (current) setLoading(false); });
    return () => { current = false; clearTimeout(timeout); abort.abort(); };
  }, [goalId, jobs, retry]);
  const active = artifacts.find((artifact) => artifact.artifact_id === selected);
  return <Card>
    <Text accessibilityRole="header" style={{ color: COLORS.text, fontWeight: "800" }}>Images et audio</Text>
    {loading ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.muted }}>Récupération des résultats…</Text> : null}
    {artifacts.map((artifact, index) => <ActionButton key={artifact.artifact_id}
      label={`${artifact.media_type === "image/png" ? "Afficher l’image" : "Ouvrir l’audio"} ${index + 1}`}
      onPress={() => setSelected(artifact.artifact_id)} disabled={artifact.artifact_id === selected} />)}
    {active ? <MediaPreview key={active.artifact_id} artifact={active} /> : null}
    {!loading && !error && !artifacts.length ? <Text style={{ color: COLORS.muted }}>Aucun média validé n’est disponible pour ces étapes.</Text> : null}
    <ErrorBanner message={error} />
    {!loading ? <ActionButton label={error ? "Réessayer les médias" : "Actualiser les médias"} onPress={() => setRetry((value) => value + 1)} /> : null}
  </Card>;
}

export function GoalMediaArtifacts({ goalId, nodes, enabled, visible }: {
  goalId: string; nodes: PlanNode[]; enabled: boolean; visible: boolean;
}) {
  const [foreground, setForeground] = useState(AppState.currentState !== "background" && AppState.currentState !== "inactive");
  const [focused, setFocused] = useState(false);
  const [pairingChanged, setPairingChanged] = useState(false);
  useFocusEffect(useCallback(() => {
    setFocused(true);
    // Stack routes can stay mounted underneath a new route.
    return () => setFocused(false);
  }, []));
  useEffect(() => {
    setPairingChanged(false);
    const unsubscribe = subscribeConnectionChanges(() => setPairingChanged(true));
    const subscription = AppState.addEventListener("change", (state) => setForeground(state === "active"));
    return () => { unsubscribe(); subscription.remove(); };
  }, [goalId]);
  const mediaNodes = nodes.filter(isMediaNode);
  if (!mediaNodes.length) return null;
  const jobs = mediaNodes.filter((node) => node.status === "completed" && node.worker_job_id).map((node) => node.worker_job_id!);
  return <View style={{ gap: 14 }}>
    {mediaNodes.filter((node) => node.status !== "completed").map((node) => <Card key={node.id}>
      <Text style={{ color: COLORS.text, fontWeight: "700" }}>{node.title}</Text>
      <Text accessibilityLiveRegion="polite" style={{ color: COLORS.muted }}>
        {node.status === "failed" ? "La génération du média a échoué." : node.status === "cancelled" ? "La génération a été annulée."
          : node.status === "running" ? "Génération du média en cours…" : "Génération du média en attente."}
      </Text>
      {node.error_summary ? <ErrorBanner message={node.error_summary} /> : null}
    </Card>)}
    {pairingChanged ? <Text style={{ color: COLORS.warning }}>Le jumelage a changé. Rouvrez ce projet pour lire ses médias.</Text> : null}
    {jobs.length > 0 && !enabled ? <Text style={{ color: COLORS.muted }}>Reconnectez-vous au serveur pour lire les médias.</Text> : null}
    {jobs.length > 0 && enabled && visible && focused && foreground && !pairingChanged
      ? <MediaResults key={goalId} goalId={goalId} jobIds={jobs} /> : null}
  </View>;
}
