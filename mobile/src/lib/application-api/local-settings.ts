import * as adapter from "@/lib/local-model-settings";
import { invokeApplicationCommand } from "./registry";

export type { LocalModelSettings } from "@/lib/local-model-settings";
export { DEFAULT_GENERATION_SETTINGS, parseGenerationSettings } from "@/lib/local-model-settings";
export const readLocalModelSettings: typeof adapter.readLocalModelSettings = () => invokeApplicationCommand("settings.local.read", {});
export const saveLocalModelSettings: typeof adapter.saveLocalModelSettings = (input) => invokeApplicationCommand("settings.local.update", input);
