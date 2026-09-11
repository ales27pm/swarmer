import type { GoalDetail } from "@/lib/api/types";
import type { ProjectPreview } from "@/lib/api/project";

export const projectFixture: ProjectPreview = {
  project_id: "project_1", revision_id: "revision_2", revision: 2, sha256: "a".repeat(64), state: "ready",
  message: "Application et tests prêts à relire.", plan: ["Créer les contacts", "Vérifier la persistance"],
  files: [{ path: "app.py", content: "print('project')\n" }, { path: "README.md", content: "Lancer python app.py" }],
  checks: [{ command: ["python", "-m", "unittest"], status: "passed", exit_code: 0, output: "3 tests passed", duration_ms: 130 }],
  run_instructions: "python app.py", runtime: "python", task_id: null,
};
export const projectGoalFixture: GoalDetail = {
  goal: {
    id: "goal_1", root_task_id: "task_1", objective: "Créer une application de contacts", status: "waiting_permission",
    autonomy_profile: "assisted", planner_source: "ubuntu_local", max_steps: 8, max_parallelism: 1,
    max_replans: 3, max_runtime_seconds: 1800, max_model_calls: 30, step_count: 1, replan_count: 0, model_call_count: 2,
    completion_criteria: ["Gérer les contacts"], current_phase: "needs_user", evaluator_status: "needs_user",
    created_at: "2030-01-01T00:00:00Z", updated_at: "2030-01-01T00:01:00Z",
  },
  nodes: [], result: null,
};
