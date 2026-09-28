import type { GoalDetail, PlanNode } from "@/lib/api/types";
import type { ProjectGraph } from "@/lib/api/project-graph";
import type { GoalDetailController } from "@/screens/goal-detail-content";

const time = "2026-09-27T22:40:00Z";
export function graphNode(id: string, patch: Partial<PlanNode> = {}): PlanNode {
  return { id, goal_run_id: "goal_preview", parent_node_id: null, node_type: "worker", title: "Préparer la semaine",
    objective: "Organiser les priorités déjà fournies.", required_skill: "writing.draft", status: "running", priority: 0,
    depends_on: [], assigned_agent_id: "writer", worker_job_id: null, task_id: `task_${id}`, expected_output: "Une liste priorisée",
    result_summary: null, error_summary: null, created_at: time, updated_at: time, completed_at: null, ...patch };
}
export function projectGraphFixture(): ProjectGraph {
  return { schema_version: "1.0", observed_at: time,
    goal: { id: "goal_preview", root_task_id: "task_preview", objective: "Organiser ma semaine", status: "running", current_phase: "executing", updated_at: time },
    conversation_revision: 2, project_id: "project_preview", criteria: [{ id: "criterion_1", index: 0, text: "Un planning fondé sur mes disponibilités", coverage: "not_mapped" }],
    nodes: [graphNode("node_availability", { title: "Lire les disponibilités", status: "completed", required_skill: "calendar.read", assigned_agent_id: "calendar", result_summary: "Créneaux fournis relevés." }),
      graphNode("node_priorities", { title: "Classer les priorités", status: "completed", result_summary: "Liste priorisée enregistrée." }),
      graphNode("node_plan", { title: "Préparer le planning", depends_on: ["node_availability", "node_priorities"] }),
      graphNode("node_review", { title: "Vérifier les conflits", status: "planned", assigned_agent_id: null, depends_on: ["node_plan"] })],
    dependencies: [{ from_node_id: "node_availability", to_node_id: "node_plan", dependency_type: "hard" },
      { from_node_id: "node_priorities", to_node_id: "node_plan", dependency_type: "optional" },
      { from_node_id: "node_plan", to_node_id: "node_review", dependency_type: "hard" }], latest_revision: null,
    evaluations: [{ id: "evaluation_preview", goal_run_id: "goal_preview", sequence: 1, status: "continue", reason_summary: "Les créneaux et les priorités sont disponibles. Le planning reste à rédiger puis à vérifier.", missing_requirements: ["Planning relu"], invalid_results: [], created_at: time, authority: "model_report" }],
    evaluations_has_more: false,
    planning_decisions: [{ id: "planning:1", audit_event_id: 1, goal_run_id: "goal_preview", event_type: "goal.plan.accepted", planner_source: "ubuntu_local", rationale_summary: "Recueillir les disponibilités et les priorités séparément, puis les réunir pour proposer un planning et vérifier les conflits.", node_ids: ["node_availability", "node_priorities", "node_plan", "node_review"], conversation_revision: 1, model_call_id: "model_call_preview", model_id: "planner-preview", created_at: time, authority: "planner_proposal" }],
    planning_decisions_has_more: false,
    coverage: { mode: "persisted_records", scope: "current_goal_and_latest_project_revision", criterion_mapping: "not_recorded", planner_rationale: "recorded", check_freshness: "not_established" } };
}
/** Benign local fixture only. No requests or mutation operations are provided. */
export function projectPreviewController(): GoalDetailController {
  const graph = projectGraphFixture();
  const goal: GoalDetail["goal"] = { ...graph.goal, autonomy_profile: "assisted", planner_source: "ubuntu_local", max_steps: 20,
    max_parallelism: 2, max_replans: 3, max_runtime_seconds: 600, max_model_calls: 30, step_count: 4, replan_count: 0,
    model_call_count: 3, completion_criteria: graph.criteria.map((item) => item.text), evaluator_status: "continue",
    evaluator_summary: graph.evaluations[0].reason_summary, created_at: time };
  const detail: GoalDetail = { goal, nodes: graph.nodes, result: null };
  return { detail, goal, nodes: graph.nodes, result: null, source: "authoritative", initialLoading: false, refreshing: false,
    busy: null, feedbackLocked: false, notice: null, error: null, completedCount: 2, blockedCount: 0, runningAgents: ["writer"], online: true,
    refresh: async () => {}, confirmCancel: () => {}, start: async () => {}, replan: async () => {}, submitFeedback: async () => {} };
}
