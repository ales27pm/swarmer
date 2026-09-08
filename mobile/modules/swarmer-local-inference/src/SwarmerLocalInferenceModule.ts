import { NativeModule, requireNativeModule } from "expo";

import type {
  LocalGenerationResult,
  LocalInferenceCapabilities,
  LocalInferenceRuntime,
  LocalInferenceStatus,
  LocalModel,
} from "./SwarmerLocalInference.types";

declare class SwarmerLocalInferenceModule extends NativeModule {
  capabilities(): Promise<LocalInferenceCapabilities>;
  importModel(input: {
    runtime: LocalInferenceRuntime;
    uri: string;
    displayName?: string;
  }): Promise<LocalModel>;
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
