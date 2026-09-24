import type { ActivityItem, ActivityPage } from "@/lib/api/activity";

export function activityItem(id = "worker_job:job_1", patch: Partial<ActivityItem> = {}): ActivityItem {
  return {
    id, kind: "worker_job", status: "completed", title: "Travail d’agent",
    recorded_at: "2026-09-24T12:00:00Z", started_at: "2026-09-24T12:00:01Z", completed_at: "2026-09-24T12:00:02Z",
    duration_ms: 1000, goal_run_id: "goal_1", task_id: "task_1", node_id: "node_1", agent_id: "agent_1",
    model_id: null, role: null, tool_name: "research.query",
    detail: { revision_id: null, check_index: null, exit_code: null, file_count: null, command: null },
    ...patch,
  };
}
export function activityPage(items: ActivityItem[] = [activityItem()], patch: Partial<ActivityPage> = {}): ActivityPage {
  return {
    schema_version: "1.0", scope: { type: "goal", id: "goal_1", goal_run_id: "goal_1", root_task_id: "task_root" },
    items, next_cursor: null, has_more: false,
    coverage: { mode: "persisted_records", live_operations: false, notice: "Preuves enregistrées ; les opérations internes non enregistrées sont absentes." },
    ...patch,
  };
}
