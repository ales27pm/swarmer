import { sha256 } from "@/lib/iphone-capabilities/grant";

export type GoalWritingDraft = {
  schema_version: "1.0";
  content_trust: "untrusted";
  text: string;
  summary: string;
  goal_run_id: string;
  node_id: string;
  worker_job_id: string;
  sha256: string;
};

const DRAFT_FIELDS = new Set([
  "schema_version", "content_trust", "text", "summary",
  "goal_run_id", "node_id", "worker_job_id", "sha256",
]);

function invalid(): never {
  throw new Error("Le brouillon reçu ne correspond pas à ce travail ou dépasse les limites.");
}

function identifier(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,200}$/.test(value);
}

function boundedText(value: unknown, maxCharacters: number, maxBytes: number): string {
  if (typeof value !== "string" || value.length > maxCharacters * 2 || !value.trim()) {
    return invalid();
  }
  let characters = 0;
  let bytes = 0;
  for (const character of value) {
    const point = character.codePointAt(0) as number;
    if (point === 0 || (point >= 0xd800 && point <= 0xdfff)) return invalid();
    characters += 1;
    bytes += point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4;
    if (characters > maxCharacters || bytes > maxBytes) return invalid();
  }
  return value;
}

export function parseGoalWritingDraft(
  value: unknown,
  goalId: string,
  nodeId: string,
  workerJobId: string,
): GoalWritingDraft {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const item = value as Record<string, unknown>;
  const keys = Object.keys(item);
  if (
    keys.length !== DRAFT_FIELDS.size || keys.some((key) => !DRAFT_FIELDS.has(key))
    || !identifier(goalId) || !identifier(nodeId) || !identifier(workerJobId)
    || item.goal_run_id !== goalId || item.node_id !== nodeId || item.worker_job_id !== workerJobId
    || item.schema_version !== "1.0" || item.content_trust !== "untrusted"
    || typeof item.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(item.sha256)
  ) return invalid();
  const text = boundedText(item.text, 24_000, 24_000);
  const summary = boundedText(item.summary, 1_200, 4_800);
  if (sha256(text) !== item.sha256) return invalid();
  return {
    schema_version: "1.0",
    content_trust: "untrusted",
    text,
    summary,
    goal_run_id: goalId,
    node_id: nodeId,
    worker_job_id: workerJobId,
    sha256: item.sha256,
  };
}
