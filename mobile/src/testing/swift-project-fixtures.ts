import { projectFixture } from "./project-fixtures";
import type { ProjectPreview, SwiftValidation } from "@/lib/api/project";

export const swiftProjectFixture: ProjectPreview = { ...projectFixture, state: "needs_user", files: [
  { path: "Package.swift", content: "// swift-tools-version: 6.0\n" },
  { path: "Tests/HelloTests.swift", content: "import XCTest\n" },
] };
export const swiftValidationFixture: SwiftValidation = {
  validation_id: "swift_1", job_id: "job_1", revision_id: swiftProjectFixture.revision_id, sha256: swiftProjectFixture.sha256,
  source_sha256: "b".repeat(64), conversation_revision: 1, agent_id: "agent_mac", operation: "test", target: { kind: "swiftpm" },
  status: "queued", receipt: null,
};
export const swiftReceiptFixture = {
  operation: "test", kind: "swiftpm", status: "passed", exit_code: 0, source_sha256: swiftValidationFixture.source_sha256,
  request_sha256: "c".repeat(64), source_unchanged: true, tests_executed: 1, test_failures: 0, duration_ms: 300,
  test_evidence_format: "swiftpm_xunit", report_error: null, artifact_directory: ".swarmer-swift-runs/" + "d".repeat(32),
  project_revision: { validation_id: swiftValidationFixture.validation_id, project_id: swiftProjectFixture.project_id,
    revision_id: swiftProjectFixture.revision_id, sha256: swiftProjectFixture.sha256 },
};
