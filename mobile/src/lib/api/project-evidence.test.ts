import { describe, expect, it } from "@jest/globals";
import { parseProjectEvidence } from "./project-evidence";
import { evidenceFixture, evidenceMappingFixture } from "@/testing/project-evidence-fixtures";

describe("requirement evidence public contract", () => {
  it("keeps a fixture mapping valid when its revision contains several selectable files", () => {
    const view = evidenceFixture();
    view.current_revision!.files.push({ id: "file_2", path: "notes.md", sha256: "e".repeat(64), bytes: 30 });
    const mapping = evidenceMappingFixture(view);
    view.criteria[0].mapping = mapping; view.criteria[0].status = "linked";
    expect(mapping.files.map((file) => file.id)).toEqual(mapping.file_ids);
    expect(parseProjectEvidence(view, view.goal_run_id).criteria[0].status).toBe("linked");
    view.current_revision!.files[0].path = "renamed.md";
    expect(mapping.files[0].path).toBe("planning.md");
  });
  it("accepts unmapped requirements and strips arbitrary fields", () => {
    const raw = { ...evidenceFixture(), private_payload: "not public" };
    expect(parseProjectEvidence(raw, "goal_preview")).toEqual(evidenceFixture());
    expect(parseProjectEvidence(raw, "goal_preview").criteria[0].status).toBe("unmapped");
  });
  it("distinguishes a linked successful check from an explicit review", () => {
    const view = evidenceFixture();
    view.criteria[0].mapping = evidenceMappingFixture(view);
    view.criteria[0].status = "linked";
    expect(parseProjectEvidence(view, view.goal_run_id).criteria[0].status).toBe("linked");
    view.criteria[0].mapping.review_status = "reviewed";
    view.criteria[0].mapping.reviewed_at = view.observed_at;
    view.criteria[0].status = "reviewed";
    expect(parseProjectEvidence(view, view.goal_run_id).criteria[0].status).toBe("reviewed");
  });
  it.each(["failed", "skipped"] as const)("allows linking mixed receipts but rejects a claimed review containing a %s check", (status) => {
    const view = evidenceFixture(); view.current_revision!.checks[1].status = status;
    const mapping = evidenceMappingFixture(view);
    mapping.check_ids.push(view.current_revision!.checks[1].id); mapping.checks.push({ ...view.current_revision!.checks[1] });
    view.criteria[0].mapping = mapping; view.criteria[0].status = "linked";
    expect(parseProjectEvidence(view, view.goal_run_id).criteria[0].status).toBe("linked");
    mapping.review_status = "reviewed"; mapping.reviewed_at = view.observed_at; view.criteria[0].status = "reviewed";
    expect(() => parseProjectEvidence(view, view.goal_run_id)).toThrow();
  });
  it.each(["context", "criterion", "revision", "producer", "file", "check"])("downgrades a claimed review when the %s snapshot differs", (kind) => {
    const view = evidenceFixture(); const mapping = evidenceMappingFixture(view);
    mapping.review_status = "reviewed"; mapping.reviewed_at = view.observed_at;
    view.criteria[0].mapping = mapping; view.criteria[0].status = "reviewed";
    if (kind === "context") view.context_sha256 = "f".repeat(64);
    if (kind === "criterion") view.criteria[0].text = "Exigence modifiée";
    if (kind === "revision") view.current_revision!.sha256 = "f".repeat(64);
    if (kind === "producer") view.current_revision!.worker_job_id = "another_job";
    if (kind === "file") view.current_revision!.files[0].sha256 = "f".repeat(64);
    if (kind === "check") view.current_revision!.checks[0].exit_code = 1;
    const parsed = parseProjectEvidence(view, view.goal_run_id);
    expect(parsed.criteria[0].status).toBe("stale");
    expect(parsed.criteria[0].mapping!.stale_reasons.length).toBeGreaterThan(0);
  });
  it("retains old requirement mappings as stale history", () => {
    const view = evidenceFixture(); view.unmatched_mappings = [evidenceMappingFixture(view)]; view.criteria = [];
    expect(parseProjectEvidence(view, view.goal_run_id).unmatched_mappings[0].stale_reasons).toContain("goal_changed");
  });
  it.each(["foreign goal", "empty selection", "duplicate ids", "unbound snapshot", "review without pass", "missing field", "duplicate criteria", "status mismatch"])("rejects %s", (kind) => {
    const view = evidenceFixture(); const mapping = evidenceMappingFixture(view);
    view.criteria[0].mapping = mapping; view.criteria[0].status = "linked";
    if (kind === "foreign goal") mapping.goal_run_id = "another_goal";
    if (kind === "empty selection") { mapping.file_ids = []; mapping.check_ids = []; mapping.files = []; mapping.checks = []; }
    if (kind === "duplicate ids") mapping.file_ids.push(mapping.file_ids[0]);
    if (kind === "unbound snapshot") mapping.files[0].id = "another_file";
    if (kind === "review without pass") { mapping.review_status = "reviewed"; mapping.reviewed_at = view.observed_at; mapping.checks[0].exit_code = 1; }
    if (kind === "missing field") delete (mapping as Partial<typeof mapping>).reviewed_at;
    if (kind === "duplicate criteria") view.criteria.push(view.criteria[0]);
    if (kind === "status mismatch") view.criteria[0].status = "reviewed";
    expect(() => parseProjectEvidence(view, view.goal_run_id)).toThrow();
  });
  it("permits a prior-goal current revision without pretending its mapping is current", () => {
    const view = evidenceFixture(); view.criteria[0].mapping = evidenceMappingFixture(view); view.criteria[0].status = "linked";
    view.current_revision!.goal_run_id = "older_goal";
    expect(parseProjectEvidence(view, view.goal_run_id).criteria[0].status).toBe("stale");
  });
});
