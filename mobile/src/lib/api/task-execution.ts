import type { GoalNodeStatus, GoalStatus } from "@/lib/api/types";

export type TaskGoalExecution = {
  schema_version: "1.0";
  task_id: string;
  goal_run_id: string;
  root_task_id: string;
  status: GoalStatus;
  truncated: boolean;
  nodes: {
    node_id: string;
    node_type: "worker";
    status: GoalNodeStatus;
    title: string;
    output_summary: string | null;
    error_summary: string | null;
    provenance: {
      node_id: string;
      worker_job_id: string | null;
      agent_id: string | null;
      required_skill: string | null;
      result_digest: string | null;
    };
  }[];
};

function invalid(): never {
  throw new Error("Les preuves des travaux d’agents reçues sont invalides.");
}

function record(value: unknown, keys: string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const item = value as Record<string, unknown>;
  if (Object.keys(item).length !== keys.length || keys.some((key) => !(key in item))) return invalid();
  return item;
}

function identifier(value: unknown): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(value)) return invalid();
  return value;
}

function optionalIdentifier(value: unknown): string | null {
  return value === null ? null : identifier(value);
}

function text(value: unknown, limit: number): string {
  if (typeof value !== "string" || value.length > limit * 2 || !value.trim() || [...value].length > limit || /\u0000/.test(value)) return invalid();
  return value;
}

const GOAL_STATUSES = new Set(["planning", "running", "waiting_permission", "completed", "failed", "cancelled", "budget_exhausted"]);
const NODE_STATUSES = new Set(["planned", "ready", "dispatched", "running", "waiting_permission", "waiting_capability", "completed", "failed", "blocked", "cancelled", "skipped"]);

export function parseTaskGoalExecution(value: unknown, taskId: string): TaskGoalExecution | null {
  // Older servers and locally cached tasks do not have this optional projection.
  if (value === undefined || value === null) return null;
  const item = record(value, ["schema_version", "task_id", "goal_run_id", "root_task_id", "status", "nodes", "truncated"]);
  if (item.schema_version !== "1.0" || identifier(item.task_id) !== taskId
    || !GOAL_STATUSES.has(String(item.status)) || typeof item.truncated !== "boolean"
    || !Array.isArray(item.nodes) || item.nodes.length > 20) return invalid();
  const seen = new Set<string>();
  const nodes = item.nodes.map((value): TaskGoalExecution["nodes"][number] => {
    const node = record(value, ["node_id", "node_type", "status", "title", "output_summary", "error_summary", "provenance"]);
    const provenance = record(node.provenance, ["node_id", "worker_job_id", "agent_id", "required_skill", "result_digest"]);
    const id = identifier(node.node_id);
    if (node.node_type !== "worker" || !NODE_STATUSES.has(String(node.status)) || seen.has(id)
      || identifier(provenance.node_id) !== id
      || (provenance.result_digest !== null && (typeof provenance.result_digest !== "string" || !/^[a-f0-9]{64}$/.test(provenance.result_digest)))) return invalid();
    seen.add(id);
    return {
      node_id: id, node_type: "worker", status: node.status as GoalNodeStatus,
      title: text(node.title, 1200),
      output_summary: node.output_summary === null ? null : text(node.output_summary, 1200),
      error_summary: node.error_summary === null ? null : text(node.error_summary, 500),
      provenance: {
        node_id: id, worker_job_id: optionalIdentifier(provenance.worker_job_id),
        agent_id: optionalIdentifier(provenance.agent_id), required_skill: optionalIdentifier(provenance.required_skill),
        result_digest: provenance.result_digest as string | null,
      },
    };
  });
  return {
    schema_version: "1.0", task_id: taskId, goal_run_id: identifier(item.goal_run_id),
    root_task_id: identifier(item.root_task_id), status: item.status as GoalStatus,
    nodes, truncated: item.truncated,
  };
}
