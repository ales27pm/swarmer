/** Explicit owner-selected filters; these never grant access to another project. */
export type MemorySearchOptions = {
  scope?: string;
  kind?: string;
  limit?: number;
  symbolic?: { catalogs: { namespace: string; scheme_id: string }[] };
};

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/;
const symbolicScope = /^(general|project:[A-Za-z0-9][A-Za-z0-9_.:-]{0,199})$/;

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype;
}

function invalid(): never {
  throw new Error("Les filtres de recherche mémoire sont invalides.");
}

export function memorySearchRequest(query: string, options: MemorySearchOptions = {}): MemorySearchOptions & { query: string } {
  if (typeof query !== "string" || !query.trim() || Array.from(query).length > 2000 || query.includes("\0")
    || !record(options) || Object.keys(options).some((key) => !["scope", "kind", "limit", "symbolic"].includes(key))) invalid();
  const result: MemorySearchOptions & { query: string } = { query };
  for (const field of ["scope", "kind"] as const) {
    const value = options[field];
    if (value !== undefined) {
      if (typeof value !== "string" || !value.trim() || Array.from(value).length > 100 || value.includes("\0")) invalid();
      result[field] = value;
    }
  }
  if (options.limit !== undefined) {
    if (!Number.isSafeInteger(options.limit) || options.limit < 1 || options.limit > 20) invalid();
    result.limit = options.limit;
  }
  if (options.symbolic !== undefined) {
    const symbolic = options.symbolic;
    if (!result.scope || !symbolicScope.test(result.scope) || !record(symbolic)
      || Object.keys(symbolic).some((key) => key !== "catalogs") || !Array.isArray(symbolic.catalogs)
      || symbolic.catalogs.length < 1 || symbolic.catalogs.length > 8) invalid();
    const seen = new Set<string>();
    result.symbolic = { catalogs: symbolic.catalogs.map((catalog) => {
      if (!record(catalog) || Object.keys(catalog).some((key) => !["namespace", "scheme_id"].includes(key))
        || typeof catalog.namespace !== "string" || !identifier.test(catalog.namespace)
        || typeof catalog.scheme_id !== "string" || !identifier.test(catalog.scheme_id)) invalid();
      const key = JSON.stringify([catalog.namespace, catalog.scheme_id]);
      if (seen.has(key)) invalid();
      seen.add(key);
      return { namespace: catalog.namespace, scheme_id: catalog.scheme_id };
    }) };
  }
  return result;
}
