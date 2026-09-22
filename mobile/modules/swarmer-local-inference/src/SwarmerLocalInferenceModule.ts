import { NativeModule, requireNativeModule } from "expo";

import type {
  LocalGenerationResult,
  LocalEmbeddingStatus, LocalEmbeddingLoadInput, LocalEmbeddingInput, LocalEmbeddingResult,
  LocalInferenceCapabilities,
  LocalInferenceRuntime,
  LocalInferenceStatus,
  LocalModel,
  LocalModelDownloadInput,
} from "./SwarmerLocalInference.types";

declare class SwarmerLocalInferenceModule extends NativeModule {
  capabilities(): Promise<LocalInferenceCapabilities>;
  embeddingStatus(): Promise<LocalEmbeddingStatus>;
  loadEmbedder(input: LocalEmbeddingLoadInput): Promise<LocalEmbeddingStatus>;
  embed(input: LocalEmbeddingInput): Promise<LocalEmbeddingResult>;
  unloadEmbedder(): Promise<void>;
  importModel(input: {
    runtime: LocalInferenceRuntime;
    uri: string;
    displayName?: string;
  }): Promise<LocalModel>;
  downloadAndImportModel(input: LocalModelDownloadInput): Promise<LocalModel>;
  cancelModelDownload(): Promise<void>;
  pickAndImportDirectory(runtime: "coreml" | "mlx"): Promise<LocalModel>;
  listModels(): Promise<LocalModel[]>;
  loadModel(input: {
    runtime: LocalInferenceRuntime;
    modelId: string;
    revision?: string;
  }): Promise<LocalInferenceStatus>;
  status(): Promise<LocalInferenceStatus>;
  generate(input: {
    prompt: string;
    maxTokens?: number;
    temperature?: number;
  }): Promise<LocalGenerationResult>;
  cancel(): Promise<void>;
  unload(): Promise<void>;
}

export default requireNativeModule<SwarmerLocalInferenceModule>("SwarmerLocalInference");
