import { sha256 } from "@noble/hashes/sha256";
import { bytesToHex } from "@noble/hashes/utils";

export const MAX_GOAL_MEDIA_BYTES = 8 * 1024 * 1024;
export type GoalMediaArtifact = {
  artifact_id: string; job_id: string; goal_id: string;
  media_type: "image/png" | "audio/wav"; size_bytes: number; sha256: string;
  width: number | null; height: number | null; duration_ms: number | null;
  sample_rate: number | null; channels: number | null;
};
const keys = ["artifact_id", "job_id", "goal_id", "media_type", "size_bytes", "sha256", "width", "height", "duration_ms", "sample_rate", "channels"];
function fail(): never { throw new Error("Le média reçu ne correspond pas au résultat attendu."); }
export function goalMediaIdentifier(value: string): string {
  if (!/^goal_[a-f0-9]{32}$/.test(value)) fail();
  return value;
}
export function parseGoalMediaArtifact(value: unknown, goalId: string): GoalMediaArtifact {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail();
  const record = value as Record<string, unknown>;
  if (Object.keys(record).length !== keys.length || keys.some((key) => !(key in record))) fail();
  if (record.goal_id !== goalMediaIdentifier(goalId) || typeof record.artifact_id !== "string" || !/^media_[a-f0-9]{40}$/.test(record.artifact_id)
    || typeof record.job_id !== "string" || !/^job_[a-f0-9]{32}$/.test(record.job_id)
    || typeof record.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(record.sha256)
    || !Number.isSafeInteger(record.size_bytes) || Number(record.size_bytes) < 1 || Number(record.size_bytes) > MAX_GOAL_MEDIA_BYTES) fail();
  if (record.media_type === "image/png") {
    if (![512, 768].includes(Number(record.width)) || typeof record.width !== "number"
      || ![512, 768].includes(Number(record.height)) || typeof record.height !== "number"
      || record.duration_ms !== null || record.sample_rate !== null || record.channels !== null) fail();
  } else if (record.media_type === "audio/wav") {
    if (record.width !== null || record.height !== null || record.sample_rate !== 24000 || record.channels !== 1
      || !Number.isSafeInteger(record.duration_ms) || Number(record.duration_ms) < 1 || Number(record.duration_ms) > 30000) fail();
  } else fail();
  return Object.fromEntries(keys.map((key) => [key, record[key]])) as GoalMediaArtifact;
}
export function parseGoalMedia(value: unknown, goalId: string): GoalMediaArtifact[] {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).length !== 1 || !("artifacts" in value)
    || !Array.isArray(value.artifacts) || value.artifacts.length > 100) fail();
  const artifacts = value.artifacts.map((item) => parseGoalMediaArtifact(item, goalId));
  if (new Set(artifacts.map((item) => item.artifact_id)).size !== artifacts.length) fail();
  return artifacts;
}
export function verifyGoalMediaBytes(bytes: Uint8Array, artifact: GoalMediaArtifact): void {
  if (bytes.length !== artifact.size_bytes || bytesToHex(sha256(bytes)) !== artifact.sha256) fail();
  if (artifact.media_type === "image/png") {
    const magic = [137, 80, 78, 71, 13, 10, 26, 10];
    if (bytes.length < 24 || magic.some((byte, i) => bytes[i] !== byte)
      || String.fromCharCode(...bytes.subarray(12, 16)) !== "IHDR") fail();
    const data = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    if (data.getUint32(16) !== artifact.width || data.getUint32(20) !== artifact.height) fail();
  } else if (bytes.length < 44 || String.fromCharCode(...bytes.subarray(0, 4)) !== "RIFF"
    || String.fromCharCode(...bytes.subarray(8, 12)) !== "WAVE") fail();
}
