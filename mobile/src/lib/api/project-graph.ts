import { activityIdentifier } from "./activity";
import type { GoalNodeStatus, GoalRecord, PlanNode } from "./types";

export type ProjectGraph = {
  schema_version: "1.0";
  observed_at: string;
  goal: Pick<GoalRecord, "id" | "root_task_id" | "objective" | "status" | "current_phase" | "updated_at">;
  conversation_revision: number;
  project_id: string | null;
  criteria: { id: string; index: number; text: string; coverage: "not_mapped" }[];
  nodes: PlanNode[];
  dependencies: { from_node_id: string; to_node_id: string; dependency_type: "hard" | "optional" }[];
  latest_revision: null | {
    id: string; project_id: string; goal_run_id: string; node_id: string; worker_job_id: string;
    revision: number; sha256: string; created_at: string;
    files: { id: string; path: string; sha256: string; bytes: number }[];
    checks: { id: string; index: number; command: string[] | null; status: "passed" | "failed" | "skipped";
      exit_code: number | null; duration_ms: number; provenance: "attached_to_revision" }[];
  };
  evaluations: { id: string; goal_run_id: string; sequence: number; status: "continue" | "replan" | "done" | "failed" | "needs_user";
    reason_summary: string; missing_requirements: string[]; invalid_results: string[]; created_at: string; authority: "model_report" }[];
  evaluations_has_more: boolean;
  planning_decisions: { id: string; audit_event_id: number; goal_run_id: string;
    event_type: "goal.plan.accepted" | "goal.replan.accepted"; planner_source: "iphone_local" | "ubuntu_local" | "manual" | "test";
    rationale_summary: string; node_ids: string[]; conversation_revision: number; model_call_id: string | null;
    model_id: string | null; created_at: string; authority: "planner_proposal" }[];
  planning_decisions_has_more: boolean;
  coverage: { mode: "persisted_records"; scope: "current_goal_and_latest_project_revision";
    criterion_mapping: "not_recorded"; planner_rationale: "recorded" | "not_recorded"; check_freshness: "not_established" };
};

function invalid(): never { throw new Error("Le parcours reçu est incomplet ou ne correspond pas à ce projet."); }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, max = 4000): string {
  if (typeof value !== "string" || !value.trim() || Array.from(value).length > max || /[\u0000\ud800-\udfff]/u.test(value)) return invalid();
  return value;
}
function count(value: unknown, max = Number.MAX_SAFE_INTEGER, min = 0): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < min || value > max) return invalid();
  return value;
}
function array(value: unknown, max: number): unknown[] {
  if (!Array.isArray(value) || value.length > max) return invalid();
  return value;
}
function choice<T extends string>(value: unknown, options: readonly T[]): T {
  if (typeof value !== "string" || !options.includes(value as T)) return invalid();
  return value as T;
}
function nullable<T>(value: unknown, parse: (value: unknown) => T): T | null {
  return value == null ? null : parse(value);
}
function date(value: unknown): string {
  const result = text(value, 64);
  if (!Number.isFinite(Date.parse(result))) return invalid();
  return result;
}
function digest(value: unknown): string {
  const result = text(value, 64);
  if (!/^[0-9a-f]{64}$/.test(result)) return invalid();
  return result;
}
function unique<T extends { id: string }>(items: T[]): T[] {
  if (new Set(items.map((item) => item.id)).size !== items.length) return invalid();
  return items;
}
const id = (value: unknown) => activityIdentifier(value);
const STATUSES: GoalNodeStatus[] = ["planned", "ready", "dispatched", "running", "waiting_permission", "waiting_capability", "completed", "failed", "blocked", "cancelled", "skipped"];

/** Explicit public projection. Never forward arbitrary audit/model payloads into the UI. */
export function parseProjectGraph(value: unknown, goalId: string): ProjectGraph {
  const raw = record(value);
  for (const field of ["schema_version", "observed_at", "goal", "conversation_revision", "project_id", "criteria", "nodes", "dependencies", "latest_revision", "evaluations", "evaluations_has_more", "planning_decisions", "planning_decisions_has_more", "coverage"]) {
    if (!Object.hasOwn(raw, field)) return invalid();
  }
  const goal = record(raw.goal); const coverage = record(raw.coverage);
  if (raw.schema_version !== "1.0" || goal.id !== goalId || typeof raw.evaluations_has_more !== "boolean"
    || coverage.mode !== "persisted_records" || coverage.scope !== "current_goal_and_latest_project_revision"
    || coverage.criterion_mapping !== "not_recorded" || !["recorded", "not_recorded"].includes(String(coverage.planner_rationale))
    || typeof raw.planning_decisions_has_more !== "boolean"
    || coverage.check_freshness !== "not_established") return invalid();
  const nodes = unique(array(raw.nodes, 20).map((value): PlanNode => {
    const node = record(value);
    if (node.goal_run_id !== goalId) return invalid();
    return {
      id: id(node.id), goal_run_id: id(node.goal_run_id), parent_node_id: nullable(node.parent_node_id, id),
      node_type: choice(node.node_type, ["worker", "synthesis"]), title: text(node.title, 500), objective: text(node.objective),
      required_skill: nullable(node.required_skill, id), status: choice(node.status, STATUSES), priority: count(node.priority, 100),
      depends_on: array(node.depends_on, 20).map(id), assigned_agent_id: nullable(node.assigned_agent_id, id),
      worker_job_id: nullable(node.worker_job_id, id), task_id: nullable(node.task_id, id),
      expected_output: nullable(node.expected_output, text), result_summary: nullable(node.result_summary, text),
      error_summary: nullable(node.error_summary, text), created_at: date(node.created_at), updated_at: date(node.updated_at),
      completed_at: nullable(node.completed_at, date),
    };
  }));
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = array(raw.dependencies, 190).map((value) => {
    const edge = record(value); const from = id(edge.from_node_id); const to = id(edge.to_node_id);
    if (from === to || !nodeIds.has(from) || !nodeIds.has(to)) return invalid();
    return { from_node_id: from, to_node_id: to, dependency_type: choice(edge.dependency_type, ["hard", "optional"] as const) };
  });
  if (new Set(edges.map((edge) => JSON.stringify([edge.from_node_id, edge.to_node_id]))).size !== edges.length) return invalid();
  for (const node of nodes) {
    const incoming = edges.filter((edge) => edge.to_node_id === node.id).map((edge) => edge.from_node_id);
    if (new Set(node.depends_on).size !== node.depends_on.length || incoming.length !== node.depends_on.length
      || incoming.some((dependency) => !node.depends_on.includes(dependency))) return invalid();
  }
  const remaining = new Set(nodeIds);
  while (remaining.size) {
    const ready = nodes.filter((node) => remaining.has(node.id) && node.depends_on.every((dependency) => !remaining.has(dependency)));
    if (!ready.length) return invalid();
    ready.forEach((node) => remaining.delete(node.id));
  }
  if ((raw.planning_decisions as unknown[])?.length === 0 && coverage.planner_rationale === "recorded") return invalid();
  const projectId = nullable(raw.project_id, id);
  const revision = nullable(raw.latest_revision, (value): NonNullable<ProjectGraph["latest_revision"]> => {
    const item = record(value);
    if (item.project_id !== projectId) return invalid();
    return {
      id: id(item.id), project_id: id(item.project_id), goal_run_id: id(item.goal_run_id), node_id: id(item.node_id),
      worker_job_id: id(item.worker_job_id), revision: count(item.revision, Number.MAX_SAFE_INTEGER, 1), sha256: digest(item.sha256), created_at: date(item.created_at),
      files: unique(array(item.files, 80).map((value) => {
        const file = record(value);
        return { id: id(file.id), path: text(file.path, 240), sha256: digest(file.sha256), bytes: count(file.bytes, 64000) };
      })),
      checks: unique(array(item.checks, 12).map((value) => {
        const check = record(value);
        if (check.provenance !== "attached_to_revision") return invalid();
        return { id: id(check.id), index: count(check.index, 11), command: nullable(check.command, (value) => array(value, 8).map((part) => text(part, 256))),
          status: choice(check.status, ["passed", "failed", "skipped"] as const), exit_code: nullable(check.exit_code, (value) => count(value, 255, -255)),
          duration_ms: count(check.duration_ms, 900000), provenance: "attached_to_revision" as const };
      })),
    };
  });
  return {
    schema_version: "1.0", observed_at: date(raw.observed_at), conversation_revision: count(raw.conversation_revision), project_id: projectId,
    goal: { id: id(goal.id), root_task_id: id(goal.root_task_id), objective: text(goal.objective),
      status: choice(goal.status, ["planning", "running", "waiting_permission", "completed", "failed", "cancelled", "budget_exhausted"] as const), current_phase: text(goal.current_phase, 200), updated_at: date(goal.updated_at) },
    criteria: unique(array(raw.criteria, 20).map((value) => {
      const item = record(value); if (item.coverage !== "not_mapped") return invalid();
      return { id: id(item.id), index: count(item.index, 19), text: text(item.text, 500), coverage: "not_mapped" as const };
    })), nodes, dependencies: edges, latest_revision: revision,
    evaluations: unique(array(raw.evaluations, 5).map((value) => {
      const item = record(value); if (item.goal_run_id !== goalId || item.authority !== "model_report") return invalid();
      return { id: id(item.id), goal_run_id: id(item.goal_run_id), sequence: count(item.sequence, Number.MAX_SAFE_INTEGER, 1),
        status: choice(item.status, ["continue", "replan", "done", "failed", "needs_user"] as const), reason_summary: text(item.reason_summary),
        missing_requirements: array(item.missing_requirements, 20).map((value) => text(value, 500)), invalid_results: array(item.invalid_results, 20).map((value) => text(value, 500)),
        created_at: date(item.created_at), authority: "model_report" as const };
    })), evaluations_has_more: raw.evaluations_has_more,
    planning_decisions: unique(array(raw.planning_decisions, 5).map((value) => {
      const item = record(value);
      if (item.goal_run_id !== goalId || item.authority !== "planner_proposal") return invalid();
      const nodeIds = array(item.node_ids, 20).map(id);
      if (!nodeIds.length || new Set(nodeIds).size !== nodeIds.length) return invalid();
      return { id: id(item.id), audit_event_id: count(item.audit_event_id, Number.MAX_SAFE_INTEGER, 1), goal_run_id: id(item.goal_run_id),
        event_type: choice(item.event_type, ["goal.plan.accepted", "goal.replan.accepted"] as const),
        planner_source: choice(item.planner_source, ["iphone_local", "ubuntu_local", "manual", "test"] as const),
        rationale_summary: text(item.rationale_summary), node_ids: nodeIds, conversation_revision: count(item.conversation_revision),
        model_call_id: nullable(item.model_call_id, id), model_id: nullable(item.model_id, (value) => text(value, 500)),
        created_at: date(item.created_at), authority: "planner_proposal" as const };
    })), planning_decisions_has_more: raw.planning_decisions_has_more,
    coverage: { mode: "persisted_records", scope: "current_goal_and_latest_project_revision", criterion_mapping: "not_recorded", planner_rationale: choice(coverage.planner_rationale, ["recorded", "not_recorded"] as const), check_freshness: "not_established" },
  };
}
