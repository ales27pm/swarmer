import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";

import { KeyboardInputGroup, KeyboardTextInput, ScreenShell } from "@/components/screen-shell";
import { LocalModelPresets } from "@/components/local-model-presets";
import {
  ActionButton,
  Card,
  COLORS,
  ErrorBanner,
  SectionTitle,
  useAccessibilityAnnouncement,
} from "@/components/swarm-ui";
import { createLocalGoalPlanSession, type SwarmPlanProposal } from "@/lib/application-api/server";
import { submitReviewedToolProposal } from "@/lib/application-api/tool-proposal";
import { buildLocalSwarmPlanPrompt } from "@/lib/local-swarm-plan";
import { readInitialGoal, parseCompletedLocalGoalPlan, startReviewedLocalGoalPlan, type GoalPlanSession, type GoalPlanSnapshot } from "@/lib/application-api/goal-plan";
import {
  buildLocalProposalPrompt,
  createLocalGenerationSession,
  cancelLocalModelDownload,
  downloadLocalGgufModel,
  getLocalInferenceCapabilities,
  getLocalInferenceStatus,
  isActionableToolProposal,
  isHuggingFaceModelId,
  isImmutableHuggingFaceRevision,
  isLocalInferenceAvailable,
  listLocalModels,
  loadLocalModel,
  parseLocalToolProposal,
  pickAndImportLocalModel,
  unloadLocalModel,
  type LocalInferenceCapabilities,
  type LocalInferenceRuntime,
  type LocalInferenceStatus,
  type BackgroundExecutionStatus,
  type LocalModel,
  type LocalToolProposal,
} from "@/lib/application-api/local-inference";
import { LOCAL_MODEL_PRESETS, preferredLocalRuntime } from "@/lib/local-model-presets";
import {
  DEFAULT_GENERATION_SETTINGS,
  parseGenerationSettings,
  readLocalModelSettings,
  saveLocalModelSettings,
} from "@/lib/application-api/local-settings";

const RUNTIMES: readonly {
  value: LocalInferenceRuntime;
  label: string;
  detail: string;
}[] = [
  { value: "coreml", label: "Core ML", detail: "Dossier modèle + tokenizer" },
  { value: "mlx", label: "MLX", detail: "Dossier local ou dépôt épinglé" },
  { value: "llama.cpp", label: "llama.cpp", detail: "Fichier GGUF importé" },
];

type BusyAction = "initial" | "import" | "download" | "save" | "load" | "generate" | "cancel" | "unload" | "submit";

function BackgroundExecutionCard({ status }: { status: BackgroundExecutionStatus | undefined }) {
  let description = "Disponibilité non confirmée. Garde l’app au premier plan pendant la génération.";
  if (status) {
    if (status.state === "expiring") description = "Arrêt demandé par le système ou l’utilisateur. Le calcul en cours se termine avant la libération de la tâche.";
    else if (status.active) description = `Tâche ${status.executionDevice === "cpu" ? "CPU" : "GPU"} admise par iOS. Cette génération peut continuer en arrière-plan tant que le système l’autorise.`;
    else if (!status.osSupported) description = "Cette version d’iOS ne prend pas en charge la continuation du calcul. Garde l’app au premier plan.";
    else if (!status.supported) description = "Cette version du module ne propose pas de continuation sur cet appareil. Garde l’app au premier plan.";
    else if (status.state === "requesting") description = "Demande en cours auprès d’iOS. Garde l’app au premier plan jusqu’à son admission.";
    else if (status.reason === "not_permitted" || (status.entitlementGranted === false && status.gpuSupported && status.executionDevice !== "cpu")) description = "iOS a refusé l’autorisation d’arrière-plan. La génération reste au premier plan.";
    else if (["system_busy", "admission_timeout", "registration_failed", "request_failed", "foreground_required"].includes(status.reason ?? "")) {
      description = "Aucune tâche d’arrière-plan n’a été admise pour cette opération. Garde l’app au premier plan.";
    } else if (status.state === "cancelled" || status.reason === "request_cancelled") description = "La tâche a été annulée. Aucune continuation d’arrière-plan n’est active.";
    else if (status.state === "failed") description = "La tâche n’a pas confirmé sa réussite. Aucune continuation d’arrière-plan n’est active.";
    else if (status.state === "completed") description = "La tâche de génération est terminée. Cela ne valide pas le plan ou la proposition produite.";
    else if (status.executionDevice === "cpu" || !status.gpuSupported) description = "Le repli CPU est disponible. Une tâche doit encore être admise par iOS lors de la génération ; sa disponibilité ne garantit pas sa continuation.";
    else if (status.entitlementGranted === null) description = "Le système et le GPU sont compatibles ; l’autorisation n’a pas encore été confirmée. Une demande sera faite lors de la génération MLX.";
    else description = "Une admission GPU a déjà été confirmée. Chaque nouvelle génération reste soumise à l’autorisation d’iOS.";
  }
  return <Card testID="local-model-background-status">
    <Text selectable style={{ color: COLORS.text, fontWeight: "800" }}>MLX · Exécution en arrière-plan</Text>
    <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>{description}</Text>
    {status?.executionDevice === "cpu" || (status?.supported && !status.gpuSupported) ? (
      <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>Calcul local sur CPU : il peut être plus lent que sur GPU. Les données restent sur l’iPhone.</Text>
    ) : null}
    {status ? <>
      <Text selectable style={{ color: COLORS.subtle }}>Activité : {status.active ? "tâche admise en cours" : "aucune tâche admise active"}</Text>
      {status.entitlementGranted === true || status.active || status.outputBytes > 0 ? (
        <Text selectable style={{ color: COLORS.subtle }}>Texte produit par la tâche d’arrière-plan : {status.outputBytes} octets UTF-8 · pas un nombre de tokens.</Text>
      ) : null}
    </> : null}
    <Text selectable style={{ color: COLORS.subtle }}>En arrière-plan, l’API permet uniquement de suivre ou d’annuler le calcul déjà admis, pendant sa durée autorisée. Elle ne permet pas de lancer un autre travail et ne constitue pas un serveur permanent.</Text>
  </Card>;
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

function supportsRuntime(
  capabilities: LocalInferenceCapabilities | null,
  runtime: LocalInferenceRuntime,
): boolean {
  if (!capabilities) return false;
  if (runtime === "coreml") return capabilities.coreml;
  if (runtime === "mlx") return capabilities.mlx;
  return capabilities.llamaCpp;
}

function capabilityReason(
  capabilities: LocalInferenceCapabilities | null,
  runtime: LocalInferenceRuntime,
): string | undefined {
  if (!capabilities?.reasons) return undefined;
  return capabilities.reasons[runtime] ?? (
    runtime === "llama.cpp" ? capabilities.reasons.llamaCpp : undefined
  );
}

function formattedSize(bytes: number): string {
  if (bytes < 1_000_000) return `${Math.round(bytes / 1_000)} ko`;
  if (bytes < 1_000_000_000) return `${(bytes / 1_000_000).toFixed(1)} Mo`;
  return `${(bytes / 1_000_000_000).toFixed(2)} Go`;
}

function isPickerCancellation(cause: unknown): boolean {
  if (typeof cause !== "object" || cause === null) return false;
  const value = cause as { code?: unknown; message?: unknown };
  const code = typeof value.code === "string" ? value.code.toLowerCase() : "";
  const message = typeof value.message === "string" ? value.message.toLowerCase() : "";
  return code.includes("cancel") || message.includes("cancelled") || message.includes("canceled");
}

function RuntimeSelector({
  capabilities,
  disabled,
  onChange,
  value,
}: {
  capabilities: LocalInferenceCapabilities | null;
  disabled: boolean;
  onChange: (value: LocalInferenceRuntime) => void;
  value: LocalInferenceRuntime;
}) {
  return (
    <View style={{ gap: 8 }}>
      {RUNTIMES.map((runtime) => {
        const supported = supportsRuntime(capabilities, runtime.value);
        const selected = value === runtime.value;
        const reason = capabilityReason(capabilities, runtime.value);
        return (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Runtime ${runtime.label}`}
            accessibilityState={{ disabled: disabled || !supported, selected }}
            disabled={disabled || !supported}
            key={runtime.value}
            onPress={() => onChange(runtime.value)}
            style={({ pressed }) => ({
              backgroundColor: selected ? `${COLORS.accent}1f` : COLORS.background,
              borderColor: selected ? COLORS.accent : COLORS.border,
              borderCurve: "continuous",
              borderRadius: 12,
              borderWidth: 1,
              gap: 3,
              opacity: disabled || !supported ? 0.5 : pressed ? 0.75 : 1,
              padding: 12,
            })}
          >
            <Text style={{ color: COLORS.text, fontWeight: "800" }}>{runtime.label}</Text>
            <Text selectable style={{ color: COLORS.muted, fontSize: 12 }}>
              {supported ? runtime.detail : reason ?? "Indisponible sur cet appareil"}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

function ImportedModels({
  disabled,
  models,
  onSelect,
  selectedId,
}: {
  disabled: boolean;
  models: LocalModel[];
  onSelect: (model: LocalModel) => void;
  selectedId: string;
}) {
  if (!models.length) {
    return (
      <Text selectable style={{ color: COLORS.subtle, lineHeight: 19 }}>
        Aucun modèle compatible n’est encore importé pour ce runtime.
      </Text>
    );
  }
  return (
    <View style={{ gap: 8 }}>
      {models.map((model) => (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`Choisir ${model.displayName}`}
          accessibilityState={{ disabled, selected: model.modelId === selectedId }}
          disabled={disabled}
          key={model.modelId}
          onPress={() => onSelect(model)}
          style={({ pressed }) => ({
            backgroundColor: COLORS.background,
            borderColor: model.modelId === selectedId ? COLORS.accent : COLORS.border,
            borderCurve: "continuous",
            borderRadius: 12,
            borderWidth: 1,
            gap: 3,
            opacity: disabled ? 0.5 : pressed ? 0.75 : 1,
            padding: 12,
          })}
        >
          <Text selectable style={{ color: COLORS.text, fontWeight: "700" }}>
            {model.displayName}
          </Text>
          <Text selectable style={{ color: COLORS.subtle, fontSize: 11 }}>
            {formattedSize(model.sizeBytes)} · {model.modelId}
          </Text>
          {model.source.startsWith("Documents/Models") ? (
            <Text selectable style={{ color: COLORS.accent, fontSize: 12, lineHeight: 18 }}>
              Conservé dans Fichiers · Sur mon iPhone › monGARS Swarm › Models
            </Text>
          ) : null}
        </Pressable>
      ))}
    </View>
  );
}

function ProposalEvidence({
  proposal,
  rawText,
  tokenCount,
  planMode = false,
}: {
  proposal: LocalToolProposal | null;
  rawText: string | null;
  tokenCount: number | null;
  planMode?: boolean;
}) {
  if (rawText === null) return null;
  return (
    <>
      <SectionTitle title="Sortie locale" />
      <Card>
        <Text
          accessibilityRole="alert"
          selectable
          style={{ color: COLORS.warning, fontSize: 12, fontWeight: "800" }}
        >
          {planMode ? "Sortie du plan initial local" : "Proposition locale — non vérifiée et non exécutée"}
        </Text>
        <Text
          selectable
          style={{ color: COLORS.muted, fontFamily: "Courier", fontSize: 11, lineHeight: 17 }}
        >
          {rawText}
        </Text>
        {tokenCount === null ? null : (
          <Text selectable style={{ color: COLORS.subtle, fontSize: 11, fontVariant: ["tabular-nums"] }}>
            {tokenCount} jeton{tokenCount === 1 ? "" : "s"} généré{tokenCount === 1 ? "" : "s"}
          </Text>
        )}
        {proposal ? (
          <View style={{ borderTopColor: COLORS.border, borderTopWidth: 1, gap: 6, paddingTop: 10 }}>
            <Text selectable style={{ color: COLORS.text, fontWeight: "700" }}>
              {proposal.summary}
            </Text>
            <Text selectable style={{ color: COLORS.subtle, fontSize: 11 }}>
              Outil proposé : {proposal.tool_name}
            </Text>
          </View>
        ) : null}
      </Card>
    </>
  );
}


function LocalPlanEvidence({ plan }: { plan: SwarmPlanProposal }) {
  return (
    <Card>
      <Text selectable style={{ color: COLORS.text, fontWeight: "800" }}>Plan initial à relire</Text>
      <Text selectable style={{ color: COLORS.muted }}>{plan.rationale_summary}</Text>
      {plan.nodes.map((node, index) => (
        <View key={node.temporary_id} style={{ gap: 5, borderTopWidth: 1, borderTopColor: COLORS.border, paddingTop: 10 }}>
          <Text selectable style={{ color: COLORS.text, fontWeight: "700" }}>{index + 1}. {node.title}</Text>
          <Text selectable style={{ color: COLORS.muted }}>{node.objective}</Text>
          <Text selectable style={{ color: COLORS.subtle }}>Compétence : {node.required_skill ?? "Synthèse"}</Text>
          <Text selectable style={{ color: COLORS.subtle }}>Sortie attendue : {node.expected_output}</Text>
          <Text selectable style={{ color: COLORS.subtle }}>Dépendances : {node.dependencies.join(", ") || "Aucune"}</Text>
        </View>
      ))}
      <Text selectable style={{ color: COLORS.muted }}>Critères : {plan.completion_criteria.join(" · ") || "Objectif du but"}</Text>
      <Text selectable style={{ color: COLORS.subtle }}>Parallélisme maximal : {plan.max_parallelism}</Text>
    </Card>
  );
}

export default function LocalModelScreen() {
  const { goalId: parameter } = useLocalSearchParams<{ goalId?: string | string[] }>();
  const value = Array.isArray(parameter) ? parameter[0] : parameter;
  const goalId = value && /^[A-Za-z0-9_-]{1,128}$/.test(value) ? value : null;
  return <LocalModelContent key={value ?? "tool"} goalId={goalId} goalMode={parameter !== undefined} />;
}

function LocalModelContent({ goalId, goalMode }: { goalId: string | null; goalMode: boolean }) {
  const router = useRouter();
  const nativeAvailable = isLocalInferenceAvailable();
  const generationVersion = useRef(0);
  const loadVersion = useRef(0);
  const downloadVersion = useRef(0);
  const runtimeSelections = useRef<Partial<Record<LocalInferenceRuntime, { modelId: string; revision: string }>>>({});
  const mounted = useRef(true);
  const generationSession = useRef<ReturnType<typeof createLocalGenerationSession> | null>(null);
  const loadedModel = useRef<Pick<LocalInferenceStatus, "runtime" | "modelId" | "revision"> | null>(null);
  const statusVersion = useRef(0);
  const [initialized, setInitialized] = useState(false);
  const [nativeBusy, setNativeBusy] = useState(false);
  const [backgroundExecution, setBackgroundExecution] = useState<BackgroundExecutionStatus>();
  const [capabilities, setCapabilities] = useState<LocalInferenceCapabilities | null>(null);
  const [models, setModels] = useState<LocalModel[]>([]);
  const [runtime, setRuntime] = useState<LocalInferenceRuntime>("mlx");
  const [modelId, setModelId] = useState("");
  const [revision, setRevision] = useState("");
  const [prompt, setPrompt] = useState("");
  const [maxTokens, setMaxTokens] = useState(goalMode ? "512" : String(DEFAULT_GENERATION_SETTINGS.maxTokens));
  const [temperature, setTemperature] = useState(String(DEFAULT_GENERATION_SETTINGS.temperature));
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<BusyAction | null>(nativeAvailable ? "initial" : null);
  const [notice, setNotice] = useState(
    nativeAvailable
      ? "Vérification des runtimes locaux…"
      : "Le module natif n’est pas présent dans cette version de l’app.",
  );
  const [error, setError] = useState<string | null>(null);
  const [rawText, setRawText] = useState<string | null>(null);
  const [tokenCount, setTokenCount] = useState<number | null>(null);
  const [proposal, setProposal] = useState<LocalToolProposal | null>(null);
  const goalSession = useRef<GoalPlanSession | null>(null);
  const startAttempted = useRef(false);
  const localStartInFlight = useRef(false);
  const [goalSnapshot, setGoalSnapshot] = useState<GoalPlanSnapshot | null>(null);
  const [goalLoading, setGoalLoading] = useState(goalMode);
  const [localPlan, setLocalPlan] = useState<{ plan: SwarmPlanProposal; snapshot: GoalPlanSnapshot } | null>(null);
  const [startLocked, setStartLocked] = useState(false);
  useAccessibilityAnnouncement(notice);

  const runtimeModels = useMemo(
    () => models.filter((model) => model.runtime === runtime && model.purpose !== "embeddings"),
    [models, runtime],
  );
  const selectedImportedModel = runtimeModels.find((model) => model.modelId === modelId.trim());
  const remoteMlx = runtime === "mlx" && !selectedImportedModel;
  const validMlxModelId = isHuggingFaceModelId(modelId);
  const immutableRevision = isImmutableHuggingFaceRevision(revision);
  const selectedRuntimeSupported = supportsRuntime(capabilities, runtime);
  const locked = busy !== null || nativeBusy;
  const canLoad =
    !loaded &&
    selectedRuntimeSupported &&
    modelId.trim().length > 0 &&
    (runtime === "mlx"
      ? Boolean(selectedImportedModel) || (validMlxModelId && immutableRevision)
      : Boolean(selectedImportedModel));

  const invalidateProposal = useCallback(() => {
    generationVersion.current += 1;
    setRawText(null);
    setTokenCount(null);
    setProposal(null);
    setLocalPlan(null);
  }, []);

  const observeNativeStatus = useCallback((status: LocalInferenceStatus) => {
    setBackgroundExecution(status.backgroundExecution);
    statusVersion.current += 1;
    const hasModel = ["ready", "generating", "cancelling"].includes(status.state) && status.runtime !== null && status.modelId !== null;
    const identity = hasModel ? { runtime: status.runtime, modelId: status.modelId, revision: status.revision } : null;
    if (JSON.stringify(identity) !== JSON.stringify(loadedModel.current)) {
      invalidateProposal();
      setNotice(hasModel
        ? "Le modèle de l’app est chargé. Son état est partagé avec l’API locale."
        : "Aucun modèle prêt n’est confirmé dans l’app. Charge un modèle avant de générer.");
    }
    loadedModel.current = identity;
    setLoaded(hasModel);
    setNativeBusy(["loading", "generating", "cancelling"].includes(status.state));
    if (hasModel) {
      setRuntime(status.runtime!);
      setModelId(status.modelId!);
      setRevision(status.revision ?? "");
    }
  }, [invalidateProposal]);

  // Model ownership is global. Refresh on focus and while visible so API activity
  // cannot leave this screen showing a model that was unloaded or replaced.
  useFocusEffect(useCallback(() => {
    if (!nativeAvailable || !initialized) return;
    let active = true;
    let reading = false;
    const refresh = async () => {
      if (reading) return;
      reading = true;
      const version = statusVersion.current;
      try {
        const status = await getLocalInferenceStatus();
        if (active && mounted.current && version === statusVersion.current) observeNativeStatus(status);
      } catch {
        if (active && mounted.current && version === statusVersion.current) {
          loadedModel.current = null;
          setLoaded(false);
          setNativeBusy(false);
          setBackgroundExecution(undefined);
        }
      } finally { reading = false; }
    };
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 2_000);
    return () => { active = false; clearInterval(timer); };
  }, [initialized, nativeAvailable, observeNativeStatus]));

  useEffect(() => {
    if (!goalMode) return;
    let active = true;
    void (async () => {
      try {
        if (!goalId) throw new Error("L’identifiant du but est invalide.");
        const session = await createLocalGoalPlanSession();
        const snapshot = await readInitialGoal(session, goalId);
        if (!active) return;
        goalSession.current = session;
        setGoalSnapshot(snapshot);
      } catch (cause) {
        if (active) setError(errorMessage(cause));
      } finally {
        if (active) setGoalLoading(false);
      }
    })();
    return () => { active = false; goalSession.current = null; };
  }, [goalId, goalMode]);

  useEffect(() => {
    if (!nativeAvailable) return;
    mounted.current = true;
    const ownedGeneration = createLocalGenerationSession();
    generationSession.current = ownedGeneration;
    let active = true;
    const savedSettings = readLocalModelSettings().catch(() => {
      if (active) setError("Les réglages enregistrés n’ont pas pu être lus. Les préréglages restent disponibles.");
      return null;
    });
    void Promise.all([getLocalInferenceCapabilities(), listLocalModels(), savedSettings, getLocalInferenceStatus()])
      .then(([nextCapabilities, nextModels, saved, status]) => {
        if (!active) return;
        setCapabilities(nextCapabilities);
        setModels(nextModels);
        const firstSupported = preferredLocalRuntime(nextCapabilities);
        const nextRuntime = saved && supportsRuntime(nextCapabilities, saved.runtime)
          ? saved.runtime : firstSupported;
        if (nextRuntime) {
          setRuntime(nextRuntime);
          const savedEmbedding = saved?.modelId === "intfloat/multilingual-e5-small" || nextModels.some(
            (model) => model.modelId === saved?.modelId && model.purpose === "embeddings",
          );
          const restore = saved?.runtime === nextRuntime && !savedEmbedding;
          const preset = LOCAL_MODEL_PRESETS[nextRuntime];
          setModelId(restore ? saved.modelId : nextRuntime === "mlx" ? preset.repoId : "");
          setRevision(restore ? saved.revision : nextRuntime === "mlx" ? preset.revision : "");
        }
        if (saved) {
          setMaxTokens(goalMode ? "512" : String(saved.maxTokens));
          setTemperature(String(saved.temperature));
        }
        observeNativeStatus(status);
        setNotice(
          status.state === "ready"
            ? "Le modèle de l’app est déjà chargé. Il reste disponible jusqu’au déchargement explicite."
            : firstSupported
            ? "Choisis un modèle local. Aucune donnée n’est envoyée au control plane pendant l’inférence."
            : "Aucun runtime local compatible n’est disponible sur cet appareil.",
        );
      })
      .catch((cause) => {
        if (!active) return;
        setError(errorMessage(cause));
        setNotice("Les capacités natives n’ont pas pu être vérifiées.");
      })
      .finally(() => {
        if (active) { setBusy(null); setInitialized(true); }
      });
    return () => {
      active = false;
      mounted.current = false;
      generationVersion.current += 1;
      loadVersion.current += 1;
      downloadVersion.current += 1;
      statusVersion.current += 1;
      void ownedGeneration.close();
    };
  }, [nativeAvailable, goalMode, observeNativeStatus]);

  function selectRuntime(nextRuntime: LocalInferenceRuntime) {
    if (locked || loaded || nextRuntime === runtime) return;
    runtimeSelections.current[runtime] = { modelId, revision };
    invalidateProposal();
    setRuntime(nextRuntime);
    const preset = LOCAL_MODEL_PRESETS[nextRuntime];
    const previous = runtimeSelections.current[nextRuntime];
    setModelId(previous?.modelId ?? (nextRuntime === "mlx" ? preset.repoId : ""));
    setRevision(previous?.revision ?? (nextRuntime === "mlx" ? preset.revision : ""));
    setLoaded(false);
    setError(null);
    setNotice("Choisis un modèle compatible avec ce runtime.");
  }

  function applyMlxPreset() {
    if (locked || loaded) return;
    invalidateProposal();
    setModelId(LOCAL_MODEL_PRESETS.mlx.repoId);
    setRevision(LOCAL_MODEL_PRESETS.mlx.revision);
    setError(null);
    setNotice("Dolphin MLX est sélectionné. Le chargement démarre uniquement avec le bouton Charger le modèle.");
  }

  async function saveSettings() {
    if (locked) return;
    setError(null);
    setBusy("save");
    try {
      const generation = parseGenerationSettings(maxTokens, temperature);
      await saveLocalModelSettings({ runtime, modelId: modelId.trim(), revision: revision.trim(), ...generation });
      if (mounted.current) setNotice("Réglages enregistrés sur cet iPhone. Le modèle ne sera pas chargé automatiquement.");
    } catch (cause) {
      if (mounted.current) setError(errorMessage(cause));
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  async function downloadGgufPreset() {
    const download = LOCAL_MODEL_PRESETS["llama.cpp"].download;
    if (locked || loaded || !selectedRuntimeSupported || runtime !== "llama.cpp" || !download) return;
    const requestVersion = downloadVersion.current + 1;
    downloadVersion.current = requestVersion;
    setBusy("download");
    setError(null);
    setNotice("Téléchargement de Dolphin GGUF (2,02 Go), puis vérification de son intégrité. Garde l’app au premier plan.");
    try {
      const imported = await downloadLocalGgufModel(download);
      if (!mounted.current || downloadVersion.current !== requestVersion) return;
      if (imported.runtime !== "llama.cpp") throw new Error("Le téléchargement n’a pas retourné un modèle GGUF.");
      setModels((current) => [imported, ...current.filter((model) => model.modelId !== imported.modelId)]);
      setModelId(imported.modelId);
      setRevision("");
      invalidateProposal();
      setNotice("Dolphin GGUF est téléchargé et vérifié. Tu peux maintenant charger le modèle.");
    } catch (cause) {
      if (!mounted.current || downloadVersion.current !== requestVersion) return;
      if (isPickerCancellation(cause)) setNotice("Téléchargement annulé.");
      else setError(errorMessage(cause));
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  async function cancelDownload() {
    if (busy !== "download") return;
    downloadVersion.current += 1;
    const cancelledVersion = downloadVersion.current;
    try {
      await cancelLocalModelDownload();
      if (mounted.current && downloadVersion.current === cancelledVersion) setNotice("Annulation du téléchargement demandée.");
    } catch (cause) {
      if (mounted.current && downloadVersion.current === cancelledVersion) setError(errorMessage(cause));
    }
  }

  function selectModel(model: LocalModel) {
    if (locked || loaded) return;
    invalidateProposal();
    setModelId(model.modelId);
    setRevision("");
    setLoaded(false);
    setError(null);
    setNotice(`${model.displayName} est sélectionné, mais pas encore chargé.`);
  }

  async function pickAndImportModel() {
    if (locked || loaded) return;
    setBusy("import");
    setError(null);
    try {
      const imported = await pickAndImportLocalModel(runtime, () => mounted.current);
      if (!mounted.current) return;
      setModels((current) => [
        imported,
        ...current.filter((model) => model.modelId !== imported.modelId),
      ]);
      setModelId(imported.modelId);
      setLoaded(false);
      invalidateProposal();
      setNotice(`${imported.displayName} a été copié dans le stockage privé de l’app.`);
    } catch (cause) {
      if (!mounted.current) return;
      if (isPickerCancellation(cause)) {
        setNotice("Importation annulée.");
        return;
      }
      setError(errorMessage(cause));
      setNotice("Le modèle n’a pas été importé.");
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  async function loadSelectedModel() {
    if (locked || loaded || !canLoad) return;
    const requestVersion = loadVersion.current + 1;
    loadVersion.current = requestVersion;
    const requestedModelId = modelId.trim();
    const requestedRevision = remoteMlx ? revision.trim().toLowerCase() : null;
    setBusy("load");
    statusVersion.current += 1;
    setError(null);
    setLoaded(false);
    invalidateProposal();
    try {
      const result = await loadLocalModel({
        runtime,
        modelId: requestedModelId,
        ...(requestedRevision ? { revision: requestedRevision } : {}),
      });
      if (!mounted.current || loadVersion.current !== requestVersion) {
        return;
      }
      if (
        result.state !== "ready" ||
        result.runtime !== runtime ||
        result.modelId !== requestedModelId ||
        result.revision !== requestedRevision
      ) {
        throw new Error("Le runtime natif n’a pas confirmé le modèle exact demandé.");
      }
      observeNativeStatus(result);
      setNotice("Modèle chargé localement. Aucune exécution n’a encore été demandée.");
      // Remote MLX loading can materialize a durable model in Documents/Models.
      // Listing failure must not contradict the native ready result above.
      const refreshedModels = await listLocalModels().catch(() => null);
      if (refreshedModels && mounted.current && loadVersion.current === requestVersion) {
        setModels(refreshedModels);
      }
    } catch (cause) {
      if (!mounted.current || loadVersion.current !== requestVersion) return;
      setError(errorMessage(cause));
      setNotice("Le chargement local n’a pas été confirmé.");
    } finally {
      if (mounted.current && loadVersion.current === requestVersion) setBusy(null);
    }
  }

  async function generateProposal() {
    const intent = goalMode ? goalSnapshot?.detail.goal.objective : prompt.trim();
    if (locked || !loaded || !intent || (goalMode && (goalLoading || startAttempted.current))) return;
    const version = generationVersion.current + 1;
    generationVersion.current = version;
    setBusy("generate");
    setError(null);
    setRawText(null);
    setTokenCount(null);
    setProposal(null);
    setLocalPlan(null);
    setNotice(goalMode ? "Vérification du but avant la génération du plan initial sur l’iPhone…" : "Le modèle génère une proposition locale non vérifiée…");
    try {
      let snapshot: GoalPlanSnapshot | null = null;
      if (goalMode) {
        const session = goalSession.current;
        if (!session || !goalId || !goalSnapshot) throw new Error("Le contexte authentifié du but n’est pas disponible.");
        snapshot = await readInitialGoal(session, goalId);
        if (!mounted.current || generationVersion.current !== version) return;
        setGoalSnapshot(snapshot);
        if (snapshot.fingerprint !== goalSnapshot.fingerprint) {
          throw new Error("Le but, la mémoire ou les capacités ont changé. Relis le contexte actualisé, puis génère un nouveau plan.");
        }
        const currentCapabilities = await getLocalInferenceCapabilities();
        if (!supportsRuntime(currentCapabilities, runtime)) throw new Error("Le runtime local choisi n’est plus disponible.");
        await session.assertCurrent();
        if (!mounted.current || generationVersion.current !== version) return;
        setNotice("Le modèle génère le plan initial sur l’iPhone. Aucun démarrage serveur n’est envoyé…");
      }
      const session = generationSession.current;
      const expectedModel = loadedModel.current;
      if (!session || !expectedModel) throw new Error("Le modèle chargé n’a pas été confirmé. Actualisez son état.");
      const result = await session.generate({
        prompt: snapshot ? buildLocalSwarmPlanPrompt(snapshot.context) : buildLocalProposalPrompt(intent),
        ...parseGenerationSettings(goalMode ? "512" : maxTokens, temperature),
      }, expectedModel);
      if (!mounted.current || generationVersion.current !== version) return;
      setRawText(result.text);
      setTokenCount(result.tokenCount);
      if (result.finishReason !== "stop") {
        setNotice(
          result.finishReason === "cancelled"
            ? "Génération annulée; aucune proposition n’a été soumise."
            : "La limite de génération a été atteinte; la sortie incomplète ne peut pas être soumise.",
        );
        return;
      }
      if (snapshot) {
        const plan = parseCompletedLocalGoalPlan(result, snapshot);
        setLocalPlan({ plan, snapshot });
        setNotice("Plan initial généré sur l’iPhone. Relis les nœuds avant de démarrer explicitement; les évaluations et la suite restent sur Ubuntu.");
        return;
      }
      const parsed = parseLocalToolProposal(result.text);
      setProposal(parsed);
      setNotice(
        parsed.tool_name === "none"
          ? "Le modèle propose une réponse sans outil. Rien ne peut être soumis à l’exécuteur."
          : "Proposition structurée localement. Vérifie-la avant toute soumission explicite.",
      );
    } catch (cause) {
      if (generationVersion.current !== version) return;
      setError(errorMessage(cause));
      setNotice(goalMode ? "Plan local rejeté; aucun démarrage n’a été envoyé." : "La sortie locale est rejetée; aucune tâche ni exécution n’a été créée.");
    } finally {
      if (generationVersion.current === version) setBusy(null);
    }
  }

  async function cancelGeneration() {
    if (busy !== "generate") return;
    generationVersion.current += 1;
    setBusy("cancel");
    setError(null);
    try {
      await generationSession.current?.cancel();
      if (!mounted.current) return;
      setNotice("Annulation demandée au runtime local. Aucune proposition n’a été soumise.");
    } catch (cause) {
      if (!mounted.current) return;
      setError(errorMessage(cause));
      setNotice("Le runtime n’a pas confirmé l’annulation.");
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  async function unloadModel() {
    if (locked || !loaded) return;
    setBusy("unload");
    statusVersion.current += 1;
    setError(null);
    invalidateProposal();
    try {
      await unloadLocalModel();
      if (!mounted.current) return;
      observeNativeStatus({ state: "idle", runtime: null, modelId: null, revision: null });
      setNotice("Modèle déchargé de la mémoire de l’appareil.");
    } catch (cause) {
      if (!mounted.current) return;
      setError(errorMessage(cause));
      setNotice("Le déchargement du modèle n’a pas été confirmé.");
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  async function submitProposal() {
    if (locked || !proposal || !isActionableToolProposal(proposal)) return;
    setBusy("submit");
    setError(null);
    let taskId: string | null = null;
    try {
      const { task } = await submitReviewedToolProposal(prompt.trim(), proposal, (created) => { taskId = created.id; });
      if (!mounted.current) return;
      setNotice(
        "La proposition a été transmise au control plane authentifié. Son état d’exécution reste visible dans la tâche.",
      );
      router.push({ pathname: "/task/[id]", params: { id: task.id } });
    } catch (cause) {
      if (!mounted.current) return;
      setError(errorMessage(cause));
      invalidateProposal();
      if (taskId) {
        setNotice(
          "La tâche authentifiée existe; l’état de la proposition doit être vérifié dans cette tâche avant toute nouvelle tentative.",
        );
        router.push({ pathname: "/task/[id]", params: { id: taskId } });
        return;
      }
      setNotice(
        "La création de la tâche authentifiée n’a pas pu être confirmée. Vérifie la liste des tâches avant toute nouvelle tentative.",
      );
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  async function startWithLocalPlan() {
    const session = goalSession.current;
    if (locked || !goalId || !localPlan || !session || startAttempted.current || localStartInFlight.current) return;
    localStartInFlight.current = true;
    setBusy("submit");
    setError(null);
    try {
      const detail = await startReviewedLocalGoalPlan(session, goalId, localPlan.snapshot, rawText ?? "", {
        shouldAccept: () => mounted.current,
        onAttempt: () => { startAttempted.current = true; setStartLocked(true); },
      });
      if (!detail || !mounted.current) return;
      setNotice("Le serveur a reçu le plan initial iPhone. Consulte le but pour suivre les agents et les évaluations sur Ubuntu.");
      router.push({ pathname: "/goal/[id]", params: { id: goalId } });
    } catch (cause) {
      if (!mounted.current) return;
      setError(errorMessage(cause));
      invalidateProposal();
      setNotice(startAttempted.current
        ? "Le résultat du démarrage doit être vérifié dans le but. Aucun renvoi automatique ni nouveau démarrage depuis cet écran."
        : "Le plan n’a pas été envoyé. Vérifie le but et son contexte avant de générer à nouveau.");
    } finally {
      localStartInFlight.current = false;
      if (mounted.current) setBusy(null);
    }
  }

  return (
    <ScreenShell
      subtitle={goalMode ? "Plan initial sur l’iPhone; agents, évaluations et suite du projet sur Ubuntu." : "Core ML, MLX et GGUF s’exécutent sur l’iPhone; toute action passe ensuite par le control plane authentifié."}
      testID="local-model-screen"
      title={goalMode ? "Plan initial local" : "Modèle local"}
    >
      <ErrorBanner message={error} />
      <Card>
        <Text selectable style={{ color: COLORS.warning, fontWeight: "800" }}>
          {goalMode ? "Plan initial à valider" : "Proposition seulement"}
        </Text>
        <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>
          Le module natif ne reçoit ni jeton, ni adresse du control plane. Une génération locale ne prouve jamais qu’une action a réussi.
        </Text>
        <Text accessibilityLiveRegion="polite" selectable style={{ color: COLORS.text, lineHeight: 20 }}>
          {notice}
        </Text>
      </Card>

      {runtime === "mlx" && nativeAvailable ? <BackgroundExecutionCard status={backgroundExecution} /> : null}

      {goalMode ? (
        <Card>
          <Text selectable style={{ color: COLORS.text, fontWeight: "800" }}>Objectif du but</Text>
          <Text selectable style={{ color: COLORS.muted }}>{goalSnapshot?.detail.goal.objective ?? (goalLoading ? "Lecture authentifiée du but…" : "But indisponible")}</Text>
          {goalSnapshot?.detail.goal.completion_criteria.map((criterion, index) => (
            <Text key={index} selectable style={{ color: COLORS.subtle }}>• {criterion}</Text>
          ))}
          <Text selectable style={{ color: COLORS.muted }}>L’objectif est conservé tel qu’enregistré. Le plan local ne démarre rien avant ta validation.</Text>
          <Text selectable style={{ color: COLORS.subtle }}>Planificateur : iPhone · {runtime} · {loaded ? "modèle chargé" : "modèle non chargé"}</Text>
          {modelId ? <Text selectable style={{ color: COLORS.subtle }}>{modelId}{revision ? ` @ ${revision}` : ""}</Text> : null}
          {goalSnapshot?.context.memory ? (
            <View testID="goal-memory-status" style={{ gap: 5 }}>
              <Text selectable style={{ color: COLORS.text, fontWeight: "700" }}>Mémoire du projet · Ubuntu · SQLite</Text>
              <Text selectable style={{ color: COLORS.muted }}>
                Recherche {goalSnapshot.context.memory.mode === "semantic" ? "sémantique" : "lexicale"} · {goalSnapshot.context.memory.items.length} extrait(s)
              </Text>
              <Text selectable style={{ color: COLORS.subtle }}>
                Embeddings configurés sur Ubuntu : {goalSnapshot.context.memory.embedding.configured
                  ? (goalSnapshot.context.memory.embedding.model ?? "modèle configuré non précisé") : "non configurés"}
                {goalSnapshot.context.memory.embedding.model_revision ? ` @ ${goalSnapshot.context.memory.embedding.model_revision}` : ""}
              </Text>
              {!goalSnapshot.context.memory.items.length ? (
                <Text selectable style={{ color: COLORS.muted }}>{goalSnapshot.context.memory.reason === "no_linked_project"
                  ? "Aucun projet lié : aucun historique disponible pour ce but."
                  : "Aucun extrait pertinent retrouvé dans la mémoire de ce projet."}</Text>
              ) : <Text selectable style={{ color: COLORS.subtle }}>Les extraits historiques complètent le but courant sans remplacer ses exigences.</Text>}
              {goalSnapshot.context.memory.recent_conversation.some((message) => message.role === "user") ? (
                <View style={{ gap: 5 }}>
                  <Text selectable style={{ color: COLORS.text, fontWeight: "700" }}>Dernière réponse utilisateur</Text>
                  <Text selectable style={{ color: COLORS.muted }}>{goalSnapshot.context.memory.recent_conversation.findLast((message) => message.role === "user")?.content}</Text>
                </View>
              ) : null}
            </View>
          ) : null}
          {goalId ? <ActionButton disabled={locked} label="Consulter le but" onPress={() => router.push({ pathname: "/goal/[id]", params: { id: goalId } })} /> : null}
        </Card>
      ) : null}

      <SectionTitle title="Runtime" />
      <Card>
        <RuntimeSelector
          capabilities={capabilities}
          disabled={locked || loaded || !nativeAvailable}
          onChange={selectRuntime}
          value={runtime}
        />
      </Card>

      <LocalModelPresets
        runtime={runtime}
        disabled={locked || loaded || !selectedRuntimeSupported}
        onApply={applyMlxPreset}
        onError={setError}
      />

      <SectionTitle title="Modèle" />
      <Card>
        {runtime === "llama.cpp" ? (
          <ActionButton
            disabled={busy === "download" ? false : locked || loaded || !selectedRuntimeSupported}
            label={busy === "download" ? "Annuler le téléchargement" : "Télécharger Dolphin GGUF · 2,02 Go"}
            onPress={() => void (busy === "download" ? cancelDownload() : downloadGgufPreset())}
          />
        ) : null}
        <ImportedModels
          disabled={locked || loaded}
          models={runtimeModels}
          onSelect={selectModel}
          selectedId={modelId}
        />
        {runtime === "mlx" ? (
          <>
            <ActionButton
              busy={busy === "import"}
              disabled={locked || loaded || !selectedRuntimeSupported}
              label="Importer un dossier MLX local"
              onPress={() => void pickAndImportModel()}
            />
            <Text selectable style={{ color: COLORS.muted, fontSize: 12, fontWeight: "700" }}>
              Dépôt Hugging Face
            </Text>
            <TextInput
              accessibilityLabel="Dépôt Hugging Face"
              autoCapitalize="none"
              autoCorrect={false}
              editable={!locked && !loaded}
              onChangeText={(value) => {
                invalidateProposal();
                setModelId(value);
                setLoaded(false);
              }}
              placeholder="organisation/modele"
              placeholderTextColor={COLORS.subtle}
              style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 12, borderWidth: 1, color: COLORS.text, minHeight: 46, paddingHorizontal: 12 }}
              value={modelId}
            />
            {remoteMlx && modelId.length > 0 && !validMlxModelId ? (
              <Text accessibilityRole="alert" selectable style={{ color: COLORS.warning, lineHeight: 19 }}>
                Utilise un identifiant exact sous la forme organisation/modèle, sans URL.
              </Text>
            ) : null}
            <Text selectable style={{ color: COLORS.muted, fontSize: 12, fontWeight: "700" }}>
              Révision immuable (commit Git complet)
            </Text>
            <TextInput
              accessibilityLabel="Révision Hugging Face immuable"
              autoCapitalize="none"
              autoCorrect={false}
              editable={!locked && !loaded}
              maxLength={40}
              onChangeText={(value) => {
                invalidateProposal();
                setRevision(value);
                setLoaded(false);
              }}
              placeholder="40 caractères hexadécimaux"
              placeholderTextColor={COLORS.subtle}
              style={{ backgroundColor: COLORS.background, borderColor: immutableRevision || !revision ? COLORS.border : COLORS.warning, borderRadius: 12, borderWidth: 1, color: COLORS.text, fontFamily: "Courier", minHeight: 46, paddingHorizontal: 12 }}
              value={revision}
            />
            {remoteMlx && revision.length > 0 && !immutableRevision ? (
              <Text accessibilityRole="alert" selectable style={{ color: COLORS.warning, lineHeight: 19 }}>
                Une branche ou une étiquette mobile est refusée. Saisis le SHA Git complet de 40 caractères.
              </Text>
            ) : null}
          </>
        ) : (
          <ActionButton
            busy={busy === "import"}
            disabled={locked || loaded || !selectedRuntimeSupported}
            label={runtime === "coreml" ? "Importer un dossier Core ML" : "Importer un fichier GGUF"}
            onPress={() => void pickAndImportModel()}
          />
        )}
        <ActionButton
          busy={busy === "load"}
          disabled={locked || !canLoad}
          label="Charger le modèle"
          onPress={() => void loadSelectedModel()}
          variant="accent"
        />
        {loaded ? (
          <ActionButton
            busy={busy === "unload"}
            disabled={locked}
            label="Décharger le modèle"
            onPress={() => void unloadModel()}
          />
        ) : null}
      </Card>

      <SectionTitle title="Réglages de génération" />
      <Card>
        <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>
          {goalMode
            ? "Le plan utilise 512 jetons de sortie, la limite native actuelle. Une sortie tronquée est rejetée. La température reste réglable."
            : "256 jetons et une température de 0,1 par défaut pour des itérations courtes. Une température de 0 utilise un choix déterministe. Les limites de contexte dépendent du runtime."}
        </Text>
        <KeyboardInputGroup dismissKeyboard testID="local-model-generation-settings">
          <Text style={{ color: COLORS.text, fontWeight: "700" }}>Jetons de sortie (1–512)</Text>
          <KeyboardTextInput
            accessibilityLabel="Limite de jetons de sortie"
            editable={!locked && !goalMode}
            keyboardType="number-pad"
            value={maxTokens}
            onChangeText={(value) => { invalidateProposal(); setMaxTokens(value); }}
            style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 12, borderWidth: 1, color: COLORS.text, minHeight: 46, paddingHorizontal: 12 }}
          />
          <Text style={{ color: COLORS.text, fontWeight: "700" }}>Température (0–2)</Text>
          <KeyboardTextInput
            accessibilityLabel="Température de génération"
            editable={!locked}
            keyboardType="decimal-pad"
            value={temperature}
            onChangeText={(value) => { invalidateProposal(); setTemperature(value); }}
            style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 12, borderWidth: 1, color: COLORS.text, minHeight: 46, paddingHorizontal: 12 }}
          />
          <ActionButton label="Enregistrer les réglages" disabled={locked || !nativeAvailable} busy={busy === "save"} onPress={() => void saveSettings()} />
        </KeyboardInputGroup>
      </Card>

      <SectionTitle title={goalMode ? "Génération du plan initial" : "Intention"} />
      <Card>
        <KeyboardInputGroup testID="local-model-composer">
          {goalMode ? null : <KeyboardTextInput
            accessibilityLabel="Intention pour le modèle local"
            editable={!locked}
            multiline
            onChangeText={(value) => {
              invalidateProposal();
              setPrompt(value);
            }}
            placeholder="Décris une proposition à préparer localement"
            placeholderTextColor={COLORS.subtle}
            style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 12, borderWidth: 1, color: COLORS.text, minHeight: 112, maxHeight: 200, padding: 12, textAlignVertical: "top" }}
            value={prompt}
          />}
          {busy === "generate" ? (
            <ActionButton
              label="Annuler la génération"
              onPress={() => void cancelGeneration()}
              variant="danger"
            />
          ) : (
            <ActionButton
              disabled={locked || !loaded || (goalMode ? goalLoading || !goalSnapshot || startLocked : !prompt.trim())}
              label={goalMode ? "Générer le plan initial sur l’iPhone" : "Générer une proposition locale"}
              onPress={() => void generateProposal()}
              variant="accent"
            />
          )}
        </KeyboardInputGroup>
      </Card>

      <ProposalEvidence proposal={proposal} rawText={rawText} tokenCount={tokenCount} planMode={goalMode} />
      {goalMode && localPlan ? (
        <>
          <LocalPlanEvidence plan={localPlan.plan} />
          <Card>
            <Text selectable style={{ color: COLORS.warning, lineHeight: 20 }}>Ce bouton transmet le plan initial au serveur authentifié et démarre les agents. Les évaluations et la suite du projet restent sur Ubuntu; les autorisations d’écriture restent applicables.</Text>
            <ActionButton
              busy={busy === "submit"}
              disabled={locked || startLocked}
              label="Démarrer avec ce plan local"
              onPress={() => void startWithLocalPlan()}
              variant="accent"
            />
          </Card>
        </>
      ) : null}
      {!goalMode && proposal && isActionableToolProposal(proposal) ? (
        <Card>
          <Text selectable style={{ color: COLORS.warning, lineHeight: 20 }}>
            La soumission crée d’abord une tâche authentifiée, puis transmet cette proposition validée. Les règles serveur et accords uniques restent applicables.
          </Text>
          <ActionButton
            busy={busy === "submit"}
            disabled={locked}
            label="Soumettre au control plane"
            onPress={() => void submitProposal()}
            variant="accent"
          />
        </Card>
      ) : null}
    </ScreenShell>
  );
}
