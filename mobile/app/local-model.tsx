import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";
import * as DocumentPicker from "expo-document-picker";
import { useRouter } from "expo-router";

import { ScreenShell } from "@/components/screen-shell";
import {
  ActionButton,
  Card,
  COLORS,
  ErrorBanner,
  SectionTitle,
  useAccessibilityAnnouncement,
} from "@/components/swarm-ui";
import { sendChat, submitToolProposal } from "@/lib/api/client";
import {
  buildLocalProposalPrompt,
  cancelLocalGeneration,
  generateLocalProposal,
  getLocalInferenceCapabilities,
  importLocalModel,
  isActionableToolProposal,
  isHuggingFaceModelId,
  isImmutableHuggingFaceRevision,
  isLocalInferenceAvailable,
  listLocalModels,
  loadLocalModel,
  parseLocalToolProposal,
  pickAndImportLocalModelDirectory,
  unloadLocalModel,
  type LocalInferenceCapabilities,
  type LocalInferenceRuntime,
  type LocalModel,
  type LocalToolProposal,
} from "@/lib/local-inference";

const RUNTIMES: readonly {
  value: LocalInferenceRuntime;
  label: string;
  detail: string;
}[] = [
  { value: "coreml", label: "Core ML", detail: "Dossier modèle + tokenizer" },
  { value: "mlx", label: "MLX", detail: "Dossier local ou dépôt épinglé" },
  { value: "llama.cpp", label: "llama.cpp", detail: "Fichier GGUF importé" },
];

type BusyAction = "initial" | "import" | "load" | "generate" | "cancel" | "unload" | "submit";

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

function hasExpectedExtension(runtime: LocalInferenceRuntime, name: string): boolean {
  const normalized = name.toLowerCase();
  if (runtime === "llama.cpp") return normalized.endsWith(".gguf");
  return false;
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
        </Pressable>
      ))}
    </View>
  );
}

function ProposalEvidence({
  proposal,
  rawText,
  tokenCount,
}: {
  proposal: LocalToolProposal | null;
  rawText: string | null;
  tokenCount: number | null;
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
          Proposition locale — non vérifiée et non exécutée
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

export default function LocalModelScreen() {
  const router = useRouter();
  const nativeAvailable = isLocalInferenceAvailable();
  const generationVersion = useRef(0);
  const loadVersion = useRef(0);
  const mounted = useRef(true);
  const [capabilities, setCapabilities] = useState<LocalInferenceCapabilities | null>(null);
  const [models, setModels] = useState<LocalModel[]>([]);
  const [runtime, setRuntime] = useState<LocalInferenceRuntime>("coreml");
  const [modelId, setModelId] = useState("");
  const [revision, setRevision] = useState("");
  const [prompt, setPrompt] = useState("");
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
  useAccessibilityAnnouncement(notice);

  const runtimeModels = useMemo(
    () => models.filter((model) => model.runtime === runtime),
    [models, runtime],
  );
  const selectedImportedModel = runtimeModels.find((model) => model.modelId === modelId.trim());
  const remoteMlx = runtime === "mlx" && !selectedImportedModel;
  const validMlxModelId = isHuggingFaceModelId(modelId);
  const immutableRevision = isImmutableHuggingFaceRevision(revision);
  const selectedRuntimeSupported = supportsRuntime(capabilities, runtime);
  const locked = busy !== null;
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
  }, []);

  useEffect(() => {
    if (!nativeAvailable) return;
    mounted.current = true;
    let active = true;
    void Promise.all([getLocalInferenceCapabilities(), listLocalModels()])
      .then(([nextCapabilities, nextModels]) => {
        if (!active) return;
        setCapabilities(nextCapabilities);
        setModels(nextModels);
        const firstSupported = RUNTIMES.find((item) =>
          supportsRuntime(nextCapabilities, item.value),
        );
        if (firstSupported) setRuntime(firstSupported.value);
        setNotice(
          firstSupported
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
        if (active) setBusy(null);
      });
    return () => {
      active = false;
      mounted.current = false;
      generationVersion.current += 1;
      loadVersion.current += 1;
      void cancelLocalGeneration()
        .catch(() => undefined)
        .then(() => unloadLocalModel().catch(() => undefined));
    };
  }, [nativeAvailable]);

  function selectRuntime(nextRuntime: LocalInferenceRuntime) {
    if (locked || loaded || nextRuntime === runtime) return;
    invalidateProposal();
    setRuntime(nextRuntime);
    setModelId("");
    setRevision("");
    setLoaded(false);
    setError(null);
    setNotice("Choisis un modèle compatible avec ce runtime.");
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
      let uri: string;
      let displayName: string;
      if (runtime === "llama.cpp") {
        const picked = await DocumentPicker.getDocumentAsync({
          copyToCacheDirectory: true,
          multiple: false,
          type: "*/*",
        });
        if (!mounted.current) return;
        if (picked.canceled) {
          setNotice("Importation annulée.");
          return;
        }
        const asset = picked.assets[0];
        if (!asset || !hasExpectedExtension(runtime, asset.name)) {
          throw new Error("Choisis un fichier .gguf.");
        }
        uri = asset.uri;
        displayName = asset.name;
      } else {
        const imported = await pickAndImportLocalModelDirectory(runtime);
        if (!mounted.current) return;
        setModels((current) => [
          imported,
          ...current.filter((model) => model.modelId !== imported.modelId),
        ]);
        setModelId(imported.modelId);
        setLoaded(false);
        invalidateProposal();
        setNotice(`${imported.displayName} a été copié dans le stockage privé de l’app.`);
        return;
      }
      const imported = await importLocalModel({
        runtime,
        uri,
        displayName,
      });
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
        await unloadLocalModel().catch(() => undefined);
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
      setLoaded(true);
      setNotice("Modèle chargé localement. Aucune exécution n’a encore été demandée.");
    } catch (cause) {
      if (!mounted.current || loadVersion.current !== requestVersion) return;
      setError(errorMessage(cause));
      setNotice("Le chargement local n’a pas été confirmé.");
    } finally {
      if (mounted.current && loadVersion.current === requestVersion) setBusy(null);
    }
  }

  async function generateProposal() {
    const intent = prompt.trim();
    if (locked || !loaded || !intent) return;
    const version = generationVersion.current + 1;
    generationVersion.current = version;
    setBusy("generate");
    setError(null);
    setRawText(null);
    setTokenCount(null);
    setProposal(null);
    setNotice("Le modèle génère une proposition locale non vérifiée…");
    try {
      const result = await generateLocalProposal({
        prompt: buildLocalProposalPrompt(intent),
        maxTokens: 512,
        temperature: 0.1,
      });
      if (generationVersion.current !== version) return;
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
      setNotice("La sortie locale est rejetée; aucune tâche ni exécution n’a été créée.");
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
      await cancelLocalGeneration();
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
    setError(null);
    invalidateProposal();
    try {
      await unloadLocalModel();
      if (!mounted.current) return;
      setLoaded(false);
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
      const chat = await sendChat(prompt.trim());
      taskId = chat.task.id;
      await submitToolProposal(chat.task.id, proposal);
      if (!mounted.current) return;
      setNotice(
        "La proposition a été transmise au control plane authentifié. Son état d’exécution reste visible dans la tâche.",
      );
      router.push({ pathname: "/task/[id]", params: { id: chat.task.id } });
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

  return (
    <ScreenShell
      subtitle="Core ML, MLX et GGUF s’exécutent sur l’iPhone; toute action passe ensuite par le control plane authentifié."
      testID="local-model-screen"
      title="Modèle local"
    >
      <ErrorBanner message={error} />
      <Card>
        <Text selectable style={{ color: COLORS.warning, fontWeight: "800" }}>
          Proposition seulement
        </Text>
        <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>
          Le module natif ne reçoit ni jeton, ni adresse du control plane. Une génération locale ne prouve jamais qu’une action a réussi.
        </Text>
        <Text accessibilityLiveRegion="polite" selectable style={{ color: COLORS.text, lineHeight: 20 }}>
          {notice}
        </Text>
      </Card>

      <SectionTitle title="Runtime" />
      <Card>
        <RuntimeSelector
          capabilities={capabilities}
          disabled={locked || loaded || !nativeAvailable}
          onChange={selectRuntime}
          value={runtime}
        />
      </Card>

      <SectionTitle title="Modèle" />
      <Card>
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

      <SectionTitle title="Intention" />
      <Card>
        <TextInput
          accessibilityLabel="Intention pour le modèle local"
          editable={!locked}
          multiline
          onChangeText={(value) => {
            invalidateProposal();
            setPrompt(value);
          }}
          placeholder="Décris une proposition à préparer localement"
          placeholderTextColor={COLORS.subtle}
          style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 12, borderWidth: 1, color: COLORS.text, minHeight: 112, padding: 12, textAlignVertical: "top" }}
          value={prompt}
        />
        {busy === "generate" ? (
          <ActionButton
            label="Annuler la génération"
            onPress={() => void cancelGeneration()}
            variant="danger"
          />
        ) : (
          <ActionButton
            disabled={locked || !loaded || !prompt.trim()}
            label="Générer une proposition locale"
            onPress={() => void generateProposal()}
            variant="accent"
          />
        )}
      </Card>

      <ProposalEvidence proposal={proposal} rawText={rawText} tokenCount={tokenCount} />
      {proposal && isActionableToolProposal(proposal) ? (
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
