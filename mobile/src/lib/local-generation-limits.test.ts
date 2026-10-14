import { localModelGenerationTokenLimit, localModelSupportsGeneration } from "./local-generation-limits";

it("uses native Core ML split identity, not an editable name, to restrict generation", () => {
  expect(localModelSupportsGeneration({ runtime: "coreml", generationProfile: "dolphin-split9-experimental" })).toBe(false);
  expect(localModelSupportsGeneration({ runtime: "coreml", generationProfile: "dolphin-monolith-experimental" })).toBe(true);
  expect(localModelSupportsGeneration({ runtime: "coreml" })).toBe(true);
  expect(localModelSupportsGeneration()).toBe(true);
  const renamedGeneric = { runtime: "coreml" as const, displayName: "Dolphin3-ANE" };
  expect(localModelSupportsGeneration(renamedGeneric)).toBe(true);
});

it.each(["mlx", "llama.cpp"] as const)("does not restrict %s using a Core ML-only profile", (runtime) => {
  const model = { runtime, generationProfile: "dolphin-split9-experimental" as const };
  expect(localModelSupportsGeneration(model)).toBe(true);
  expect(localModelGenerationTokenLimit(model)).toBe(1024);
});

it("preserves the monolith's 32-token qualification cap and generic model budgets", () => {
  expect(localModelGenerationTokenLimit({ runtime: "coreml", generationProfile: "dolphin-monolith-experimental" })).toBe(32);
  expect(localModelGenerationTokenLimit({ runtime: "coreml" })).toBe(1024);
});
