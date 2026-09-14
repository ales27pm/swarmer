import { describe, expect, it } from "@jest/globals";
import { parseGoalMemoryContext } from "@/lib/api/goal-memory";
import type { GoalMemoryContext } from "@/lib/api/types";

const memory: GoalMemoryContext = {
  schema_version: "1.0", goal_id: "goal_1", project_id: null, conversation_revision: 0,
  base_revision_id: null, provider_fingerprint: "a".repeat(64), context_fingerprint: "b".repeat(64),
  mode: "lexical", reason: "no_linked_project", items: [],
  embedding: { configured: false, model: null, model_revision: null, storage: "ubuntu_sqlite" },
  local_planning_eligible: true, planning_embedding_call_count: 0, recent_conversation: [],
};

describe("scoped goal memory receipt", () => {
  it("accepts a confirmed empty context without inventing a project or embedding provider", () => {
    expect(parseGoalMemoryContext(memory, "goal_1")).toEqual(memory);
  });

  it("projects bounded public fields without forwarding arbitrary metadata", () => {
    const value = { ...memory, extra: "private-token", items: [{ id: "mem_1", source_id: "revision_1", summary: "Résumé", score: -1, metadata: { secret: "not-input" } }] };
    const parsed = parseGoalMemoryContext(value, "goal_1");
    expect(parsed.items).toEqual([{ id: "mem_1", source_id: "revision_1", summary: "Résumé", score: -1 }]);
    expect(JSON.stringify(parsed)).not.toMatch(/private-token|not-input/);
  });

  it.each([
    { goal_id: "other_goal" }, { schema_version: "2.0" }, { context_fingerprint: "bad" },
    { provider_fingerprint: "A".repeat(64) }, { mode: "unknown" }, { conversation_revision: -1 },
    { planning_embedding_call_count: 0.5 }, { local_planning_eligible: undefined },
    { recent_conversation: undefined },
    { recent_conversation: [{ role: "system", content: "not-user" }] },
    { recent_conversation: [{ role: "user", content: "x".repeat(4001) }] },
    { recent_conversation: Array.from({ length: 41 }, () => ({ role: "user", content: "test" })) },
    { embedding: { ...memory.embedding, storage: "iphone" } },
    { items: Array.from({ length: 5 }, (_, index) => ({ id: String(index), source_id: "rev", summary: "Résumé", score: 0 })) },
    { items: [{ id: "mem", source_id: "rev", summary: "x".repeat(1201), score: 0 }] },
    { items: [{ id: "mem", source_id: "rev", summary: "Résumé", score: Infinity }] },
    { items: [{ id: "mem", source_id: "rev", summary: "\ud800", score: 0 }] },
  ])("rejects an incompatible or unbounded receipt %j", (patch) => {
    expect(() => parseGoalMemoryContext({ ...memory, ...patch }, "goal_1")).toThrow("contexte mémoire");
  });
});
