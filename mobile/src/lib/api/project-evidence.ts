import { activityIdentifier } from "./activity";
import type { ProjectGraph } from "./project-graph";

export type EvidenceRevision = NonNullable<ProjectGraph["latest_revision"]>;
export type EvidenceFile = EvidenceRevision["files"][number];
export type EvidenceCheck = EvidenceRevision["checks"][number];
export type EvidenceStaleReason = "goal_changed" | "revision_changed" | "producer_changed" | "evidence_changed";
export type EvidenceMapping = {
  id: string; version: number; criterion_id: string; criterion_index: number; criterion_text: string; criterion_sha256: string;
  goal_run_id: string; project_id: string; node_id: string; worker_job_id: string; revision_id: string; revision_sha256: string;
  context_sha256: string; conversation_revision: number; file_ids: string[]; check_ids: string[];
  files: EvidenceFile[]; checks: EvidenceCheck[]; review_status: "linked" | "reviewed"; public_explanation: string;
  recorded_at: string; reviewed_at: string | null; stale_reasons: EvidenceStaleReason[];
};
export type EvidenceCriterion = {
  id: string; index: number; text: string; sha256: string;
  status: "unmapped" | "linked" | "reviewed" | "stale"; mapping: EvidenceMapping | null;
};
export type ProjectEvidenceView = {
  schema_version: "1.0"; observed_at: string; goal_run_id: string; project_id: string | null;
  context_sha256: string; conversation_revision: number; current_revision: EvidenceRevision | null;
  criteria: EvidenceCriterion[]; unmatched_mappings: EvidenceMapping[];
};
export type ProjectEvidenceWrite = {
  request_id: string; expected_version: number; context_sha256: string; conversation_revision: number; criterion_sha256: string;
  project_id: string; node_id: string; revision_id: string; revision_sha256: string; file_ids: string[]; check_ids: string[];
  review_status: "linked" | "reviewed"; public_explanation: string;
};
export type RequirementEvidenceView = ProjectEvidenceView;
export type EvidenceMappingRequest = ProjectEvidenceWrite;

function invalid(): never { throw new Error("Les preuves reçues sont incomplètes ou ne correspondent pas à ce projet."); }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function required(value: unknown, fields: string[]): Record<string, unknown> {
  const raw = record(value);
  if (fields.some((field) => !Object.hasOwn(raw, field))) return invalid();
  return raw;
}
function text(value: unknown, max: number, empty = false): string {
  if (typeof value !== "string" || (!empty && !value.trim()) || Array.from(value).length > max || /[\u0000\ud800-\udfff]/u.test(value)) return invalid();
  return value;
}
function number(value: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < min || value > max) return invalid();
  return value;
}
function array(value: unknown, max: number): unknown[] {
  if (!Array.isArray(value) || value.length > max) return invalid();
  return value;
}
function choice<T extends string>(value: unknown, choices: readonly T[]): T {
  if (typeof value !== "string" || !choices.includes(value as T)) return invalid();
  return value as T;
}
function nullable<T>(value: unknown, parse: (value: unknown) => T): T | null { return value === null ? null : parse(value); }
function date(value: unknown): string { const result = text(value, 64); return Number.isFinite(Date.parse(result)) ? result : invalid(); }
function digest(value: unknown): string { const result = text(value, 64); return /^[0-9a-f]{64}$/.test(result) ? result : invalid(); }
const id = (value: unknown) => activityIdentifier(value);
function unique<T>(items: T[], key: (item: T) => string): T[] {
  return new Set(items.map(key)).size === items.length ? items : invalid();
}
function ids(value: unknown, max: number): string[] { return unique(array(value, max).map(id), (item) => item); }
function files(value: unknown): EvidenceFile[] {
  return unique(array(value, 80).map((value) => {
    const file = required(value, ["id", "path", "sha256", "bytes"]);
    return { id: id(file.id), path: text(file.path, 240), sha256: digest(file.sha256), bytes: number(file.bytes, 0, 64000) };
  }), (file) => file.id);
}
function checks(value: unknown): EvidenceCheck[] {
  return unique(array(value, 12).map((value) => {
    const check = required(value, ["id", "index", "command", "status", "exit_code", "duration_ms", "provenance"]);
    return { id: id(check.id), index: number(check.index, 0, 11),
      command: nullable(check.command, (value) => array(value, 8).map((part) => text(part, 256))),
      status: choice(check.status, ["passed", "failed", "skipped"] as const), exit_code: nullable(check.exit_code, (value) => number(value, -255, 255)),
      duration_ms: number(check.duration_ms, 0, 900000), provenance: choice(check.provenance, ["attached_to_revision"] as const) };
  }), (check) => check.id);
}
function revision(value: unknown, projectId: string | null): EvidenceRevision {
  const raw = required(value, ["id", "project_id", "goal_run_id", "node_id", "worker_job_id", "revision", "sha256", "created_at", "files", "checks"]);
  if (raw.project_id !== projectId) return invalid();
  return { id: id(raw.id), project_id: id(raw.project_id), goal_run_id: id(raw.goal_run_id), node_id: id(raw.node_id),
    worker_job_id: id(raw.worker_job_id), revision: number(raw.revision, 1), sha256: digest(raw.sha256), created_at: date(raw.created_at),
    files: files(raw.files), checks: checks(raw.checks) };
}
function mapping(value: unknown, goalId: string): EvidenceMapping {
  const raw = required(value, ["id", "version", "criterion_id", "criterion_index", "criterion_text", "criterion_sha256", "goal_run_id", "project_id",
    "node_id", "worker_job_id", "revision_id", "revision_sha256", "context_sha256", "conversation_revision", "file_ids", "check_ids", "files", "checks",
    "review_status", "public_explanation", "recorded_at", "reviewed_at", "stale_reasons"]);
  const result: EvidenceMapping = {
    id: id(raw.id), version: number(raw.version, 1), criterion_id: id(raw.criterion_id), criterion_index: number(raw.criterion_index, 0, 19),
    criterion_text: text(raw.criterion_text, 500), criterion_sha256: digest(raw.criterion_sha256), goal_run_id: id(raw.goal_run_id), project_id: id(raw.project_id),
    node_id: id(raw.node_id), worker_job_id: id(raw.worker_job_id), revision_id: id(raw.revision_id), revision_sha256: digest(raw.revision_sha256),
    context_sha256: digest(raw.context_sha256), conversation_revision: number(raw.conversation_revision), file_ids: ids(raw.file_ids, 80), check_ids: ids(raw.check_ids, 12),
    files: files(raw.files), checks: checks(raw.checks), review_status: choice(raw.review_status, ["linked", "reviewed"] as const),
    public_explanation: text(raw.public_explanation, 2000, true), recorded_at: date(raw.recorded_at), reviewed_at: nullable(raw.reviewed_at, date),
    stale_reasons: unique(array(raw.stale_reasons, 4).map((value) => choice(value, ["goal_changed", "revision_changed", "producer_changed", "evidence_changed"] as const)), (item) => item),
  };
  if (result.goal_run_id !== goalId || result.criterion_id !== `criterion:${goalId}:${result.criterion_index}`
    || !result.file_ids.length && !result.check_ids.length
    || result.files.length !== result.file_ids.length || result.files.some((file) => !result.file_ids.includes(file.id))
    || result.checks.length !== result.check_ids.length || result.checks.some((check) => !result.check_ids.includes(check.id))
    || (result.review_status === "reviewed" && (!result.reviewed_at || !result.checks.length
      || result.checks.some((check) => check.status !== "passed" || check.exit_code !== 0)))
    || (result.review_status === "linked" && result.reviewed_at !== null)) return invalid();
  return result;
}

/** Compare concrete snapshots as well as server stale reasons; never promote a model/node status to a review. */
export function evidenceStaleReasons(mapping: EvidenceMapping, view: ProjectEvidenceView, criterion?: EvidenceCriterion): EvidenceStaleReason[] {
  const reasons = new Set(mapping.stale_reasons);
  if (mapping.context_sha256 !== view.context_sha256 || mapping.conversation_revision !== view.conversation_revision
    || criterion && (mapping.criterion_id !== criterion.id || mapping.criterion_sha256 !== criterion.sha256 || mapping.criterion_text !== criterion.text)) reasons.add("goal_changed");
  const revision = view.current_revision;
  if (!revision || mapping.revision_id !== revision.id || mapping.revision_sha256 !== revision.sha256 || mapping.project_id !== view.project_id) reasons.add("revision_changed");
  if (!revision || mapping.node_id !== revision.node_id || mapping.worker_job_id !== revision.worker_job_id || mapping.goal_run_id !== revision.goal_run_id) reasons.add("producer_changed");
  if (revision && (mapping.files.some((file) => !revision.files.some((current) => JSON.stringify(current) === JSON.stringify(file)))
    || mapping.checks.some((check) => !revision.checks.some((current) => JSON.stringify(current) === JSON.stringify(check))))) reasons.add("evidence_changed");
  return [...reasons];
}

/** Strict public projection; unknown model or private payload fields never reach rendering. */
export function parseProjectEvidence(value: unknown, goalId: string): ProjectEvidenceView {
  const raw = required(value, ["schema_version", "observed_at", "goal_run_id", "project_id", "context_sha256", "conversation_revision", "current_revision", "criteria", "unmatched_mappings"]);
  if (raw.schema_version !== "1.0" || raw.goal_run_id !== goalId) return invalid();
  const projectId = nullable(raw.project_id, id);
  const view: ProjectEvidenceView = {
    schema_version: "1.0", observed_at: date(raw.observed_at), goal_run_id: id(raw.goal_run_id), project_id: projectId,
    context_sha256: digest(raw.context_sha256), conversation_revision: number(raw.conversation_revision),
    current_revision: nullable(raw.current_revision, (value) => revision(value, projectId)),
    criteria: unique(array(raw.criteria, 20).map((value): EvidenceCriterion => {
      const item = required(value, ["id", "index", "text", "sha256", "status", "mapping"]);
      const result = { id: id(item.id), index: number(item.index, 0, 19), text: text(item.text, 500), sha256: digest(item.sha256),
        status: choice(item.status, ["unmapped", "linked", "reviewed", "stale"] as const), mapping: nullable(item.mapping, (value) => mapping(value, goalId)) };
      if (result.id !== `criterion:${goalId}:${result.index}` || (!result.mapping && result.status !== "unmapped")
        || result.mapping && (result.status === "unmapped" || result.mapping.criterion_index !== result.index)) return invalid();
      return result;
    }), (item) => item.id),
    unmatched_mappings: unique(array(raw.unmatched_mappings, 20).map((value) => mapping(value, goalId)), (item) => item.id),
  };
  for (const criterion of view.criteria) {
    if (!criterion.mapping) continue;
    criterion.mapping.stale_reasons = evidenceStaleReasons(criterion.mapping, view, criterion);
    if (criterion.mapping.stale_reasons.length || criterion.status === "stale") criterion.status = "stale";
    else if (criterion.status !== criterion.mapping.review_status) return invalid();
  }
  for (const item of view.unmatched_mappings) item.stale_reasons = [...new Set([...evidenceStaleReasons(item, view), "goal_changed" as const])];
  return view;
}
