/** Small JSON-only schema vocabulary shared by the public catalogue and validator. */
export type JsonSchema = {
  type: "object" | "array" | "string" | "integer" | "number" | "boolean";
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: false;
  items?: JsonSchema;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  minimum?: number;
  maximum?: number;
  enum?: (string | number | boolean)[];
  pattern?: string;
};

export class ApplicationApiError extends Error {
  constructor(public readonly code: string, message: string, public readonly status?: number) {
    super(message);
    this.name = "ApplicationApiError";
  }
}

export const text = (maxLength = 4000, minLength = 1): JsonSchema => ({ type: "string", minLength, maxLength });
export const choice = (...values: string[]): JsonSchema => ({ type: "string", enum: values });
export const integer = (minimum: number, maximum: number): JsonSchema => ({ type: "integer", minimum, maximum });
export const number = (minimum: number, maximum: number): JsonSchema => ({ type: "number", minimum, maximum });
export const boolean: JsonSchema = { type: "boolean" };
export const list = (items: JsonSchema, maxItems: number): JsonSchema => ({ type: "array", items, maxItems });
export const object = (properties: Record<string, JsonSchema> = {}, required = Object.keys(properties)): JsonSchema => (
  { type: "object", properties, required, additionalProperties: false }
);
export const identifier: JsonSchema = { ...text(200), pattern: "^[A-Za-z0-9_-]+$" };

function invalid(): never {
  throw new ApplicationApiError("invalid_arguments", "Les arguments ne correspondent pas au contrat de cette commande.");
}

export function validateInput(schema: JsonSchema, value: unknown, depth = 0): void {
  if (depth > 12) invalid();
  if (schema.enum && !schema.enum.includes(value as string)) invalid();
  switch (schema.type) {
    case "object": {
      if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) invalid();
      const record = value as Record<string, unknown>;
      if (Object.keys(record).some((key) => !Object.hasOwn(schema.properties ?? {}, key))) invalid();
      if (schema.required?.some((key) => !Object.hasOwn(record, key))) invalid();
      for (const [key, item] of Object.entries(record)) validateInput(schema.properties![key], item, depth + 1);
      break;
    }
    case "array":
      if (!Array.isArray(value) || value.length > (schema.maxItems ?? 1000) || value.length < (schema.minItems ?? 0)) invalid();
      value.forEach((item) => validateInput(schema.items!, item, depth + 1));
      break;
    case "string":
      if (typeof value !== "string" || value.includes("\0") || value.length < (schema.minLength ?? 0)
          || value.length > (schema.maxLength ?? 32_000) || (schema.pattern && !new RegExp(schema.pattern).test(value))) invalid();
      break;
    case "number":
    case "integer":
      if (typeof value !== "number" || !Number.isFinite(value) || (schema.type === "integer" && !Number.isSafeInteger(value))
          || value < (schema.minimum ?? -Infinity) || value > (schema.maximum ?? Infinity)) invalid();
      break;
    case "boolean": if (typeof value !== "boolean") invalid();
  }
}

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
const PRIVATE_KEYS = /^(?:token|access_token|refresh_token|auth_token|auth_token_hash|authorization|cookie|password|secret|private_key|credentials|grant_id)$/i;

/** Never serialize closures, native objects, credentials, unbounded logs or cyclic data. */
export function serializableResult(value: unknown): JsonValue {
  let count = 0;
  const visit = (item: unknown, depth: number): JsonValue => {
    if (++count > 20_000 || depth > 16) throw new ApplicationApiError("output_too_large", "Le résultat dépasse les limites de cette API.");
    if (item === null || item === undefined) return null;
    if (typeof item === "boolean") return item;
    if (typeof item === "number" && Number.isFinite(item)) return item;
    if (typeof item === "string" && item.length <= 65_536) return item;
    if (Array.isArray(item) && item.length <= 1000) return item.map((entry) => visit(entry, depth + 1));
    if (typeof item === "object" && Object.getPrototypeOf(item) === Object.prototype) {
      const result: Record<string, JsonValue> = {};
      for (const [key, entry] of Object.entries(item)) {
        if (PRIVATE_KEYS.test(key)) continue;
        if (["__proto__", "constructor", "prototype"].includes(key)) throw new ApplicationApiError("invalid_response", "Le résultat reçu est invalide.");
        result[key] = visit(entry, depth + 1);
      }
      return result;
    }
    throw new ApplicationApiError("invalid_response", "Le résultat reçu est invalide ou trop volumineux.");
  };
  const result = visit(value, 0);
  if (JSON.stringify(result).length > 262_144) throw new ApplicationApiError("output_too_large", "Le résultat dépasse les limites de cette API.");
  return result;
}
