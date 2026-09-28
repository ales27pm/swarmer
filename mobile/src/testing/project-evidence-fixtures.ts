import type { EvidenceMapping, ProjectEvidenceView } from "@/lib/api/project-evidence";

export function evidenceFixture(): ProjectEvidenceView {
  const time = "2026-09-27T22:40:00Z";
  return { schema_version: "1.0", observed_at: time, goal_run_id: "goal_preview", project_id: "project_preview",
    context_sha256: "a".repeat(64), conversation_revision: 2,
    criteria: [{ id: "criterion:goal_preview:0", index: 0, text: "Le planning respecte mes disponibilités", sha256: "b".repeat(64), status: "unmapped", mapping: null }],
    current_revision: { id: "revision_1", project_id: "project_preview", goal_run_id: "goal_preview", node_id: "node_plan", worker_job_id: "job_1",
      revision: 1, sha256: "c".repeat(64), created_at: time,
      files: [{ id: "file_1", path: "planning.md", sha256: "d".repeat(64), bytes: 42 }],
      checks: [{ id: "check_1", index: 0, command: ["node", "--test"], status: "passed", exit_code: 0, duration_ms: 50, provenance: "attached_to_revision" },
        { id: "check_2", index: 1, command: ["npm", "run", "build"], status: "failed", exit_code: 1, duration_ms: 80, provenance: "attached_to_revision" }] },
    unmatched_mappings: [] };
}

export function evidenceMappingFixture(view = evidenceFixture()): EvidenceMapping {
  const revision = view.current_revision!; const criterion = view.criteria[0];
  return { id: "mapping_1", version: 1, criterion_id: criterion.id, criterion_index: criterion.index, criterion_text: criterion.text, criterion_sha256: criterion.sha256,
    goal_run_id: view.goal_run_id, project_id: revision.project_id, node_id: revision.node_id, worker_job_id: revision.worker_job_id,
    revision_id: revision.id, revision_sha256: revision.sha256, context_sha256: view.context_sha256, conversation_revision: view.conversation_revision,
    file_ids: [revision.files[0].id], check_ids: [revision.checks[0].id],
    files: [{ ...revision.files[0] }], checks: [{ ...revision.checks[0] }],
    review_status: "linked", public_explanation: "", recorded_at: view.observed_at, reviewed_at: null, stale_reasons: [] };
}
