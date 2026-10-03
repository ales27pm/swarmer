import { describe, expect, it } from "@jest/globals";
import ordinary from "@/testing/symbolic-http-ordinary.json";
import astral from "@/testing/symbolic-http-astral.json";
import largeInteger from "@/testing/symbolic-http-large-integer.json";
import { memorySymbolicEvidence } from "./memory-symbolic";
import { memorySearchRequest, type MemorySearchOptions } from "./memory-search";
import { memorySearchResult } from "@/lib/application-api/memory-search";
import { serializableResult } from "@/lib/application-api/schema";
import type { MemoryItem } from "./types";

// Captured from real paired HTTP against a disposable SQL registry, without providers.
const item = (): MemoryItem => JSON.parse(JSON.stringify(ordinary[0])) as MemoryItem;

describe("symbolic memory transport", () => {
  it("accepts the public HTTP response unchanged, including source bindings and conditions", () => {
    const record = item();
    const before = JSON.stringify(record);
    expect(memorySymbolicEvidence(record)).toBe(record.symbolic_evidence);
    expect(serializableResult(memorySearchResult([record]))).toEqual([record]);
    expect(JSON.stringify(record)).toBe(before);
    expect(record.symbolic_evidence![0].proposal.claim.effective_conditions[0].relation).toBe("only_after");
  });

  it("matches Python Unicode character bounds without changing exact symbol identities", () => {
    const record = astral[0] as unknown as MemoryItem;
    expect(memorySymbolicEvidence(record)).toEqual(record.symbolic_evidence);
    expect(memorySearchRequest("🚀".repeat(1001)).query).toBe("🚀".repeat(1001));
    expect(() => memorySearchRequest("🚀".repeat(2001))).toThrow();
  });

  it("refuses integers rounded by JavaScript instead of showing changed evidence", () => {
    expect(() => memorySymbolicEvidence(largeInteger[0] as unknown as MemoryItem)).toThrow();
    const record = item();
    record.symbolic_evidence![0].proposal.claim.object = {
      type: "literal", datatype: "integer", lexical_value: "9007199254740993", language: null, unit: null,
    };
    expect(memorySymbolicEvidence(record)[0].proposal.claim.object).toHaveProperty("lexical_value", "9007199254740993");
  });

  it("preserves negation, modality, case-sensitive code, typed units, long conditions and JSON types", () => {
    const record = item();
    const claim = record.symbolic_evidence![0].proposal.claim;
    claim.polarity = "negated"; claim.modality = "forbidden";
    claim.applicability = { enabled: false, limit: 1, ratio: 1.25, sequence: ["A", "a", null] };
    claim.object = { type: "literal", datatype: "quantity", lexical_value: "0.030", unit: "s", language: null };
    claim.effective_conditions.push({ relation: "unless", argument: {
      type: "literal", datatype: "code", lexical_value: "Worker.Run() ".repeat(200), language: null, unit: null,
    } });
    expect(memorySymbolicEvidence(record)[0].proposal.claim).toEqual(claim);
    expect(serializableResult(memorySearchResult([record]))).toEqual([record]);
  });

  it.each(["password", "TOKEN", "credentials", "grant_id"])("refuses a card requiring %s redaction instead of silently deleting conditions", (key) => {
    const record = item();
    record.symbolic_evidence![0].proposal.claim.applicability = { required: { [key]: false } };
    expect(() => memorySearchResult([record])).toThrow(/intégralement/);
  });

  it.each(["authority", "scope", "source", "concept", "duplicate", "status", "incomplete", "sensitive"])("rejects %s corruption", (kind) => {
    const record = item(); const card = record.symbolic_evidence![0];
    if (kind === "authority") Object.assign(card, { grants_authority: true });
    if (kind === "scope") card.proposal.claim.scope = "project:other";
    if (kind === "source") card.proposal.sources[0].binding.memory_id = "another_memory";
    if (kind === "concept") card.concepts = [];
    if (kind === "duplicate") record.symbolic_evidence!.push(card);
    if (kind === "status") Object.assign(card, { validation_status: "verified" });
    if (kind === "incomplete") delete (card.proposal.claim as Partial<typeof card.proposal.claim>).effective_conditions;
    if (kind === "sensitive") record.sensitivity = "sensitive";
    expect(() => memorySymbolicEvidence(record)).toThrow();
  });

  it("does not invent evidence for legacy memory", () => {
    const record = item(); delete record.symbolic_evidence; delete record.symbolic_status;
    expect(memorySymbolicEvidence(record)).toEqual([]);
    expect(memorySearchRequest("cache")).toEqual({ query: "cache" });
  });

  it("requires an explicit scope and distinct catalog selections", () => {
    const symbolic = { catalogs: [{ namespace: "software", scheme_id: "engineering" }] };
    const options = { scope: "project:crm", kind: "fact", limit: 2, symbolic };
    expect(memorySearchRequest("cache", options)).toEqual({ query: "cache", ...options });
    expect(memorySearchRequest("cache", options).symbolic).not.toBe(symbolic);
    for (const bad of [{ symbolic }, { ...options, scope: "global" }, { ...options, limit: 0 },
      { ...options, symbolic: { catalogs: [] } }, { ...options, symbolic: { catalogs: [...symbolic.catalogs, ...symbolic.catalogs] } },
      { ...options, symbolic: { ...symbolic, grants_authority: true } }]) {
      expect(() => memorySearchRequest("cache", bad as MemorySearchOptions)).toThrow();
    }
  });
});
