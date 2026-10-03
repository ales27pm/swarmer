import type { MemoryItem } from "./types";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Language = { tag: string; origin: "declared" | "source_metadata" | "detected" };
type Identity = { type: "identity"; namespace: string; identity: string };
type Literal = { type: "literal"; datatype: string; lexical_value: string; language: Language | null; unit: string | null };
export type SymbolicEvidence = {
  schema_version: "symbolic-evidence-v1";
  catalog: { namespace: string; scheme_id: string };
  proposal: {
    proposal_id: string;
    claim: { scope: string; namespace: string; scheme_id: string; kind: string; subject: Identity; predicate: Identity;
      object: Identity | Literal; polarity: "affirmed" | "negated"; modality: string; applicability: { [key: string]: Json };
      version: string | null; effective_conditions: { relation: string; argument: Identity | Literal }[] };
    claim_sha256: string;
    sources: { binding: { memory_id: string; revision: number; view_id: string; field: "content" | "summary";
      field_sha256: string; document_sha256: string }; scope: string; origin: "user_statement" | "source_document";
      validation_status: "unvalidated" }[];
    concept_ids: string[]; lifecycle: "active" | "superseded"; validation_status: "unvalidated"; grants_authority: false; created_at: string;
  };
  matches: { channel: "concept_label" | "exact_identity"; value: string; concept_id: string | null; language: string | null; field: string | null }[];
  concepts: { concept_id: string; scope: string; namespace: string; scheme_id: string;
    labels: { text: string; language: Language; role: "pref" | "alt" | "hidden" }[]; curation_status: "proposed"; grants_authority: false }[];
  relations: { id: string; proposal_id: string; target_proposal_id: string; relationship: "supersedes" | "contradicts" | "related_to";
    created_at: string; grants_authority: false }[];
  read_token: string; validation_status: "unvalidated"; grants_authority: false;
};

const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/;
const HASH = /^[a-f0-9]{64}$/;
const CONCEPT = /^urn:swarmer:concept:[a-f0-9]{32}$/;
function invalid(): never { throw new Error("Les propositions associées à cette mémoire sont invalides."); }
function check(ok: unknown): asserts ok { if (!ok) invalid(); }
function object(value: unknown, keys?: string[]): Record<string, unknown> {
  check(value !== null && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype);
  const result = value as Record<string, unknown>;
  if (keys) check(Object.keys(result).length === keys.length && keys.every((key) => Object.hasOwn(result, key)));
  return result;
}
function text(value: unknown, max = 200): asserts value is string { check(typeof value === "string" && value.length > 0 && Array.from(value).length <= max); }
function identifier(value: unknown, pattern = ID): asserts value is string { text(value); check(pattern.test(value)); }
function array(value: unknown, min: number, max: number): unknown[] { check(Array.isArray(value) && value.length >= min && value.length <= max); return value; }
function language(value: unknown): void {
  const item = object(value, ["tag", "origin"]); text(item.tag, 100);
  check(["declared", "source_metadata", "detected"].includes(String(item.origin)));
}
function term(value: unknown, identityOnly = false): void {
  const item = object(value);
  if (item.type === "identity") {
    object(item, ["type", "namespace", "identity"]); identifier(item.namespace); text(item.identity, 1000); return;
  }
  check(!identityOnly && item.type === "literal");
  object(item, ["type", "datatype", "lexical_value", "language", "unit"]);
  check(["text", "code", "path", "url", "symbol", "integer", "decimal", "boolean", "date", "datetime", "version", "quantity"].includes(String(item.datatype)));
  text(item.lexical_value, 4000);
  if (item.datatype === "text") language(item.language); else check(item.language === null);
  if (item.datatype === "quantity") text(item.unit, 100); else check(item.unit === null);
}

/** Bounded JSON only. Preserve literal spelling, nesting and arrays without redaction or truncation. */
function bounded(value: unknown): void {
  let nodes = 0;
  const visit = (item: unknown, depth: number): void => {
    check(++nodes <= 20000 && depth <= 16);
    if (item === null || typeof item === "boolean") return;
    // JSON.parse already rounds oversized integers. Never display their altered value as evidence.
    // Large exact quantities use typed lexical_value strings in the symbolic contract.
    if (typeof item === "number") { check(Number.isFinite(item) && (!Number.isInteger(item) || Number.isSafeInteger(item))); return; }
    if (typeof item === "string") {
      check(item.length <= 65536);
      for (const char of item) check(char.length === 2 || char.charCodeAt(0) < 0xd800 || char.charCodeAt(0) > 0xdfff);
      return;
    }
    if (Array.isArray(item)) { check(item.length <= 1000); item.forEach((child) => visit(child, depth + 1)); return; }
    for (const [key, child] of Object.entries(object(item))) {
      check(!["__proto__", "prototype", "constructor"].includes(key)); visit(child, depth + 1);
    }
  };
  visit(value, 0);
  let bytes = 0;
  for (const char of JSON.stringify(value)) {
    const point = char.codePointAt(0)!;
    bytes += point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4;
  }
  check(bytes <= 128 * 1024);
}

/** Validate transport shape and scope, not the truth of a claim or the server's database state. */
export function memorySymbolicEvidence(item: Pick<MemoryItem, "id" | "scope" | "sensitivity" | "symbolic_evidence" | "symbolic_status">): SymbolicEvidence[] {
  if (item.symbolic_evidence === undefined && item.symbolic_status === undefined) return [];
  check(item.symbolic_status === "available" && item.sensitivity === "normal");
  const evidence = array(item.symbolic_evidence, 0, 50);
  const seen = new Set<string>();
  for (const value of evidence) {
    bounded(value);
    const entry = object(value, ["schema_version", "catalog", "proposal", "matches", "concepts", "relations", "read_token", "validation_status", "grants_authority"]);
    check(entry.schema_version === "symbolic-evidence-v1" && entry.validation_status === "unvalidated" && entry.grants_authority === false);
    identifier(entry.read_token, HASH);
    const catalog = object(entry.catalog, ["namespace", "scheme_id"]);
    identifier(catalog.namespace); identifier(catalog.scheme_id);
    const proposal = object(entry.proposal, ["proposal_id", "claim", "claim_sha256", "sources", "concept_ids", "lifecycle", "validation_status", "grants_authority", "created_at"]);
    identifier(proposal.proposal_id); identifier(proposal.claim_sha256, HASH); text(proposal.created_at, 64);
    check(!seen.has(proposal.proposal_id)); seen.add(proposal.proposal_id);
    check(proposal.validation_status === "unvalidated" && proposal.grants_authority === false && ["active", "superseded"].includes(String(proposal.lifecycle)));
    const claim = object(proposal.claim, ["scope", "namespace", "scheme_id", "kind", "subject", "predicate", "object", "polarity", "modality", "applicability", "version", "effective_conditions"]);
    check(claim.scope === item.scope && claim.namespace === catalog.namespace && claim.scheme_id === catalog.scheme_id);
    identifier(claim.kind); term(claim.subject, true); term(claim.predicate, true); term(claim.object);
    check(["affirmed", "negated"].includes(String(claim.polarity)));
    check(["asserted", "possible", "permitted", "required", "forbidden", "preferred", "unknown"].includes(String(claim.modality)));
    object(claim.applicability); if (claim.version !== null) text(claim.version);
    for (const condition of array(claim.effective_conditions, 0, 32)) {
      const v = object(condition, ["relation", "argument"]);
      check(["before", "after", "only_after", "until", "while", "unless", "if", "only_if", "at"].includes(String(v.relation))); term(v.argument);
    }
    const sources = array(proposal.sources, 1, 32);
    let boundToItem = false;
    const sourceKeys = new Set<string>();
    for (const source of sources) {
      const v = object(source, ["binding", "scope", "origin", "validation_status"]);
      check(v.scope === item.scope && v.validation_status === "unvalidated" && ["user_statement", "source_document"].includes(String(v.origin)));
      const b = object(v.binding, ["memory_id", "revision", "view_id", "field", "field_sha256", "document_sha256"]);
      identifier(b.memory_id); identifier(b.view_id); identifier(b.field_sha256, HASH); identifier(b.document_sha256, HASH);
      check(Number.isSafeInteger(b.revision) && Number(b.revision) >= 1 && ["content", "summary"].includes(String(b.field)));
      const key = JSON.stringify([b.memory_id, b.field]); check(!sourceKeys.has(key)); sourceKeys.add(key);
      boundToItem ||= b.memory_id === item.id;
    }
    check(boundToItem);
    const ids = array(proposal.concept_ids, 0, 35); ids.forEach((id) => identifier(id, CONCEPT));
    check(new Set(ids).size === ids.length);
    const conceptIds = new Set<string>();
    for (const concept of array(entry.concepts, 0, 35)) {
      const c = object(concept, ["concept_id", "scope", "namespace", "scheme_id", "labels", "curation_status", "grants_authority"]);
      identifier(c.concept_id, CONCEPT); check(ids.includes(c.concept_id) && !conceptIds.has(c.concept_id)); conceptIds.add(c.concept_id);
      check(c.scope === item.scope && c.namespace === catalog.namespace && c.scheme_id === catalog.scheme_id && c.curation_status === "proposed" && c.grants_authority === false);
      for (const label of array(c.labels, 1, 64)) {
        const l = object(label, ["text", "language", "role"]); text(l.text, 400); language(l.language);
        check(["pref", "alt", "hidden"].includes(String(l.role)));
      }
    }
    check(conceptIds.size === ids.length);
    for (const match of array(entry.matches, 1, 64)) {
      const m = object(match, ["channel", "value", "concept_id", "language", "field"]); text(m.value, 4000);
      check(["concept_label", "exact_identity"].includes(String(m.channel)));
      if (m.concept_id !== null) { identifier(m.concept_id, CONCEPT); check(conceptIds.has(m.concept_id)); }
      if (m.language !== null) text(m.language, 100);
      if (m.field !== null) text(m.field, 100);
    }
    for (const relation of array(entry.relations, 0, 100)) {
      const r = object(relation, ["id", "proposal_id", "target_proposal_id", "relationship", "created_at", "grants_authority"]);
      identifier(r.id); identifier(r.proposal_id); identifier(r.target_proposal_id); text(r.created_at, 64);
      check(r.grants_authority === false && ["supersedes", "contradicts", "related_to"].includes(String(r.relationship)));
      check(r.proposal_id === proposal.proposal_id || r.target_proposal_id === proposal.proposal_id);
    }
  }
  return evidence as SymbolicEvidence[];
}
