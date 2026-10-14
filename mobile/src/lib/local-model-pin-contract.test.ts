import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { LOCAL_MODEL_PRESETS } from "./local-model-presets";
import { DOLPHIN_COREML_QUALIFICATION_MAX_TOKENS } from "./local-generation-limits";

const nativeSupport = readFileSync(resolve(
  __dirname, "../../modules/swarmer-local-inference/ios/CoreMLDolphinSupport.swift",
), "utf8");

it("downloads the same immutable Dolphin artifact revision that native loading validates", () => {
  const nativeRevision = nativeSupport.match(/static\s+let\s+officialRepositoryRevision\s*=\s*"([a-f0-9]{40})"/);
  const nativeRepository = nativeSupport.match(/static\s+let\s+officialRepository\s*=\s*"([^"]+)"/);
  expect(nativeRevision).not.toBeNull();
  expect(nativeRepository).not.toBeNull();
  expect(LOCAL_MODEL_PRESETS.coreml.revision).toBe(nativeRevision![1]);
  expect(LOCAL_MODEL_PRESETS.coreml.repoId).toBe(nativeRepository![1]);
});

it("keeps native and command-level Dolphin qualification limits aligned", () => {
  const nativeLimit = nativeSupport.match(/static\s+let\s+qualificationMaxTokens\s*=\s*(\d+)/);
  expect(nativeLimit).not.toBeNull();
  expect(DOLPHIN_COREML_QUALIFICATION_MAX_TOKENS).toBe(Number(nativeLimit![1]));
});
