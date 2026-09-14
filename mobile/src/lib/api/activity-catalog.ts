export type CatalogAvailability =
  | "goal_ready" | "parameters_required" | "worker_unavailable"
  | "iphone_request" | "planned" | "policy_denied" | "unknown";

export type CatalogDomain = { id: string; title: string; description: string };
export type CatalogRole = CatalogDomain & {
  domain_id: string;
  skill_ids: string[];
  examples: string[];
};
export type CatalogSkill = CatalogDomain & {
  inputs: string[];
  output: string;
  execution: { kind: "worker" | "iphone" | "planned"; target: string | null };
  requirements: string[];
  availability: { state: CatalogAvailability; agent_ids: string[]; reason: string };
};
export type ActivityCatalog = {
  schema_version: "1.0";
  generated_at: string;
  domains: CatalogDomain[];
  roles: CatalogRole[];
  skills: CatalogSkill[];
};

const STATES = new Set<CatalogAvailability>([
  "goal_ready", "parameters_required", "worker_unavailable", "iphone_request",
  "planned", "policy_denied", "unknown",
]);

function invalid(): never { throw new Error("Le catalogue reçu est incomplet ou incompatible."); }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, maximum = 2000): string {
  if (typeof value !== "string" || !value.trim() || value.length > maximum) invalid();
  return value;
}
function list(value: unknown, maximum: number): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) invalid();
  return value;
}
function texts(value: unknown, maximum = 20, maximumText = 1000): string[] {
  return list(value, maximum).map((item) => text(item, maximumText));
}
function identifier(value: unknown): string {
  const result = text(value, 160);
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$/.test(result)) invalid();
  return result;
}
function ids(value: unknown, maximum = 250): string[] {
  const result = list(value, maximum).map(identifier);
  if (new Set(result).size !== result.length) invalid();
  return result;
}
function base(value: Record<string, unknown>): CatalogDomain {
  return { id: identifier(value.id), title: text(value.title, 200), description: text(value.description) };
}

/** Metadata only: catalog entries never authorize a job or a native capability. */
export function parseActivityCatalog(value: unknown): ActivityCatalog {
  const raw = record(value);
  if (raw.schema_version !== "1.0") invalid();
  const generatedAt = text(raw.generated_at, 80);
  if (!/^\d{4}-\d\d-\d\dT/.test(generatedAt) || !Number.isFinite(Date.parse(generatedAt))) invalid();
  const domains = list(raw.domains, 64).map((item) => base(record(item)));
  const skills = list(raw.skills, 512).map((item): CatalogSkill => {
    const source = record(item);
    const execution = record(source.execution);
    const availability = record(source.availability);
    const kind = execution.kind;
    if (kind !== "worker" && kind !== "iphone" && kind !== "planned") invalid();
    const target = execution.target === null ? null : identifier(execution.target);
    if ((kind === "planned") !== (target === null)) invalid();
    const state = availability.state as CatalogAvailability;
    if (!STATES.has(state)) invalid();
    const agents = ids(availability.agent_ids);
    if (kind === "planned" && (state !== "planned" || agents.length)) invalid();
    if (kind !== "planned" && state === "planned") invalid();
    if ((state === "goal_ready" || state === "parameters_required") && (kind !== "worker" || !agents.length)) invalid();
    if (state === "iphone_request" && kind !== "iphone") invalid();
    if (kind === "iphone" && (!target?.startsWith("iphone.") || agents.length)) invalid();
    return {
      ...base(source), inputs: texts(source.inputs), output: text(source.output),
      requirements: texts(source.requirements), execution: { kind, target },
      availability: { state, agent_ids: agents, reason: text(availability.reason) },
    };
  });
  const roles = list(raw.roles, 256).map((item): CatalogRole => {
    const source = record(item);
    return { ...base(source), domain_id: identifier(source.domain_id), skill_ids: ids(source.skill_ids, 64), examples: texts(source.examples, 12, 2000) };
  });
  for (const entries of [domains, skills, roles]) {
    if (!entries.length || new Set(entries.map((entry) => entry.id)).size !== entries.length) invalid();
  }
  const domainIds = new Set(domains.map((entry) => entry.id));
  const skillIds = new Set(skills.map((entry) => entry.id));
  if (roles.some((role) => !domainIds.has(role.domain_id) || !role.skill_ids.length || role.skill_ids.some((id) => !skillIds.has(id)))) invalid();
  return { schema_version: "1.0", generated_at: generatedAt, domains, skills, roles };
}

export function catalogSearchText(value: string): string {
  return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}
