import { sha256 } from "@noble/hashes/sha256";
import { bytesToHex } from "@noble/hashes/utils";
import type { GoalMediaArtifact } from "@/lib/api/goal-media";
export const mediaGoalId = `goal_${"a".repeat(32)}`;
export const mediaJobId = `job_${"b".repeat(32)}`;
export function mediaFixture(audio = false): { artifact: GoalMediaArtifact; bytes: Uint8Array } {
  const bytes = new Uint8Array(audio ? 44 : 24);
  if (audio) { bytes.set([82, 73, 70, 70]); bytes.set([87, 65, 86, 69], 8); }
  else {
    bytes.set([137, 80, 78, 71, 13, 10, 26, 10]); bytes.set([73, 72, 68, 82], 12);
    new DataView(bytes.buffer).setUint32(16, 512); new DataView(bytes.buffer).setUint32(20, 768);
  }
  return { bytes, artifact: {
    artifact_id: `media_${(audio ? "d" : "c").repeat(40)}`, goal_id: mediaGoalId, job_id: mediaJobId,
    sha256: bytesToHex(sha256(bytes)), size_bytes: bytes.length, media_type: audio ? "audio/wav" : "image/png",
    width: audio ? null : 512, height: audio ? null : 768, duration_ms: audio ? 200 : null,
    sample_rate: audio ? 24000 : null, channels: audio ? 1 : null,
  } };
}
