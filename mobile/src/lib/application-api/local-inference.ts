import * as adapter from "@/lib/local-inference";
import { LOCAL_MODEL_PRESETS } from "@/lib/local-model-presets";
import { invokeApplicationCommand } from "./registry";
import { ApplicationApiError } from "./schema";

export type { LocalInferenceRuntime, LocalInferenceCapabilities, LocalModel, LocalInferenceStatus, BackgroundExecutionStatus,
  LocalGenerationResult, NoToolProposal, LocalToolProposal } from "@/lib/local-inference";
export { LOCAL_INFERENCE_MODULE_NAME, assertUnambiguousJson, parseLocalToolProposal, isActionableToolProposal,
  buildLocalProposalPrompt, isImmutableHuggingFaceRevision, isHuggingFaceModelId, isLocalInferenceAvailable } from "@/lib/local-inference";
const call = <T>(name: string, input: object = {}): Promise<T> => invokeApplicationCommand(name, input);
export const getLocalInferenceCapabilities: typeof adapter.getLocalInferenceCapabilities = () => call("models.capabilities");
export const listLocalModels: typeof adapter.listLocalModels = () => call("models.list");
export const getLocalInferenceStatus: typeof adapter.getLocalInferenceStatus = () => call("models.status");
export const pickAndImportLocalModelDirectory: typeof adapter.pickAndImportLocalModelDirectory = (runtime) => call("models.import", { runtime });
export const pickAndImportLocalModel = (runtime: adapter.LocalInferenceRuntime, shouldAccept?: () => boolean): Promise<adapter.LocalModel> =>
  invokeApplicationCommand("models.import", { runtime }, { shouldAccept });
export const loadLocalModel: typeof adapter.loadLocalModel = (input) => call("models.load", Object.fromEntries(Object.entries(input).filter(([, value]) => value !== undefined)));
export const unloadLocalModel: typeof adapter.unloadLocalModel = () => call("models.unload");
export const generateLocalProposal: typeof adapter.generateLocalProposal = (input) => call("inference.generate", input);
export const cancelLocalGeneration: typeof adapter.cancelLocalGeneration = () => call("inference.cancel");
/** A screen may cancel its own generation, while the loaded model belongs to the app. */
export function createLocalGenerationSession() {
  const nativeOwner = Symbol("local-generation-session");
  let active = true;
  const cancel = (): Promise<void> => invokeApplicationCommand("inference.cancel", {}, { nativeOwner });
  return {
    generate(input: Parameters<typeof adapter.generateLocalProposal>[0], expectedModel: Pick<adapter.LocalInferenceStatus, "runtime" | "modelId" | "revision">) {
      if (!active) return Promise.reject(new ApplicationApiError("cancelled", "Cette session locale est fermée."));
      return invokeApplicationCommand<adapter.LocalGenerationResult>("inference.generate", input, {
        nativeOwner, expectedModel, shouldAccept: () => active,
      });
    },
    cancel,
    close() {
      active = false;
      // Matching the operation ticket is atomic in the registry; a later API operation is untouched.
      return cancel().catch(() => undefined);
    },
  };
}
export const cancelLocalModelDownload: typeof adapter.cancelLocalModelDownload = () => call("models.download.cancel");
export const downloadLocalGgufModel: typeof adapter.downloadLocalGgufModel = (input) => {
  if (JSON.stringify(input) !== JSON.stringify(LOCAL_MODEL_PRESETS["llama.cpp"].download)) {
    // Arbitrary downloads are not a network API capability; the existing imported-model UI remains available.
    return adapter.downloadLocalGgufModel(input);
  }
  return call("models.download", { preset: "dolphin-gguf" });
};
