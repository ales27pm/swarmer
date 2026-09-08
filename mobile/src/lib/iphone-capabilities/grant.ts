import type {
  CapabilityActionDigest,
  CapabilityArgumentsByName,
  CapabilityAuthorizationDecision,
  CapabilityAuthorizationResponse,
  CapabilityConsumeReceipt,
  CapabilityGrant,
  CapabilityNotification,
  CapabilityRequestDetail,
  CapabilityRequestEnvelope,
  CapabilityRequestPreview,
  CapabilityRequestStatus,
  CapabilityResult,
  CapabilityResultReceipt,
  ConsumedCapabilityGrant,
  IPhoneCapabilityName,
} from "./types";
import { IPHONE_CAPABILITY_SCHEMA_VERSION } from "./types";

const MAX_GRANT_LIFETIME_MS = 5 * 60 * 1_000;
const CLOCK_SKEW_MS = 30_000;
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/;
const REQUEST_ID_PATTERN = /^iphreq_[A-Za-z0-9_-]{1,160}$/;
const SAFE_ID_PATTERN = /^[A-Za-z0-9._:-]{1,200}$/;
const OPAQUE_GRANT_PATTERN = /^[A-Za-z0-9._~-]{20,512}$/;
const CAPABILITIES = new Set<IPhoneCapabilityName>([
  "iphone.location.current",
  "iphone.contacts.lookup",
  "iphone.calendar.events",
  "iphone.photos.pick",
  "iphone.mail.compose",
  "iphone.sms.compose",
]);
const REQUEST_STATUSES = new Set<CapabilityRequestStatus>([
  "waiting_approval",
  "approved",
  "denied",
  "consumed",
  "completed",
  "failed",
  "cancelled",
  "expired",
]);

export class CapabilityProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CapabilityProtocolError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) throw new CapabilityProtocolError(`${label} must be an object.`);
  return value;
}

function requireExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
  label: string,
): void {
  const actual = Object.keys(value);
  if (actual.length !== keys.length || actual.some((key) => !keys.includes(key))) {
    throw new CapabilityProtocolError(`${label} has an invalid shape.`);
  }
}

function requireString(
  value: unknown,
  label: string,
  maximum = 4_000,
  allowEmpty = false,
): string {
  if (
    typeof value !== "string" ||
    value.length > maximum ||
    (!allowEmpty && value.length === 0) ||
    value.trim() !== value
  ) {
    throw new CapabilityProtocolError(`${label} is invalid.`);
  }
  return value;
}

function requireText(
  value: unknown,
  label: string,
  maximum: number,
  allowEmpty = false,
): string {
  if (
    typeof value !== "string" ||
    value.length > maximum ||
    (!allowEmpty && value.length === 0)
  ) {
    throw new CapabilityProtocolError(`${label} is invalid.`);
  }
  return value;
}

function requireSafeId(value: unknown, label: string): string {
  const result = requireString(value, label, 200);
  if (!SAFE_ID_PATTERN.test(result)) throw new CapabilityProtocolError(`${label} is invalid.`);
  return result;
}

function requireRequestId(value: unknown): string {
  const result = requireString(value, "request_id", 200);
  if (!REQUEST_ID_PATTERN.test(result)) {
    throw new CapabilityProtocolError("request_id is invalid.");
  }
  return result;
}

export function assertCapabilityRequestId(value: string): string {
  return requireRequestId(value);
}

function requireGrantId(value: unknown): string {
  const result = requireString(value, "grant_id", 512);
  if (!OPAQUE_GRANT_PATTERN.test(result)) {
    throw new CapabilityProtocolError("grant_id is invalid.");
  }
  return result;
}

function requireDigest(value: unknown): CapabilityActionDigest {
  if (typeof value !== "string" || !DIGEST_PATTERN.test(value)) {
    throw new CapabilityProtocolError("action_digest is invalid.");
  }
  return value as CapabilityActionDigest;
}

function requireTimestamp(value: unknown, label: string): { milliseconds: number; value: string } {
  const result = requireString(value, label, 64);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(result)) {
    throw new CapabilityProtocolError(`${label} is not RFC 3339.`);
  }
  const milliseconds = Date.parse(result);
  if (!Number.isFinite(milliseconds)) {
    throw new CapabilityProtocolError(`${label} is invalid.`);
  }
  return { milliseconds, value: result };
}

function requireCapability(value: unknown): IPhoneCapabilityName {
  if (typeof value !== "string" || !CAPABILITIES.has(value as IPhoneCapabilityName)) {
    throw new CapabilityProtocolError("capability is unsupported.");
  }
  return value as IPhoneCapabilityName;
}

function requireStatus(value: unknown): CapabilityRequestStatus {
  if (typeof value !== "string" || !REQUEST_STATUSES.has(value as CapabilityRequestStatus)) {
    throw new CapabilityProtocolError("request status is invalid.");
  }
  return value as CapabilityRequestStatus;
}

function requireStringArray(
  value: unknown,
  label: string,
  maximum: number,
  strict = false,
): string[] {
  if (!Array.isArray(value) || value.length > maximum) {
    throw new CapabilityProtocolError(`${label} is invalid.`);
  }
  return value.map((entry, index) => (
    strict
      ? requireString(entry, `${label}[${index}]`, 1_000)
      : requireText(entry, `${label}[${index}]`, 1_000, true)
  ));
}

function requireAllowedKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
  label: string,
): void {
  if (Object.keys(value).some((key) => !keys.includes(key))) {
    throw new CapabilityProtocolError(`${label} has an invalid shape.`);
  }
}

function parseEmptyArguments(
  arguments_: Record<string, unknown>,
): Record<string, never> {
  requireExactKeys(arguments_, [], "arguments");
  return {};
}

function parseContactsArguments(
  arguments_: Record<string, unknown>,
): CapabilityArgumentsByName["iphone.contacts.lookup"] {
  requireExactKeys(arguments_, ["query"], "arguments");
  return { query: requireText(arguments_.query, "arguments.query", 200) };
}

function parseCalendarArguments(
  arguments_: Record<string, unknown>,
): CapabilityArgumentsByName["iphone.calendar.events"] {
  requireExactKeys(arguments_, ["start", "end"], "arguments");
  const start = requireTimestamp(arguments_.start, "arguments.start");
  const end = requireTimestamp(arguments_.end, "arguments.end");
  if (end.milliseconds <= start.milliseconds) {
    throw new CapabilityProtocolError("The calendar interval is invalid.");
  }
  return { start: start.value, end: end.value };
}

function optionalRecipients(
  arguments_: Record<string, unknown>,
): Pick<CapabilityArgumentsByName["iphone.mail.compose"], "recipients"> | object {
  if (!("recipients" in arguments_)) return {};
  return {
    recipients: requireStringArray(arguments_.recipients, "arguments.recipients", 20),
  };
}

function optionalTextArgument<Key extends "subject" | "body" | "message">(
  arguments_: Record<string, unknown>,
  key: Key,
  maximum: number,
): Partial<Record<Key, string>> {
  if (!(key in arguments_)) return {};
  return {
    [key]: requireText(arguments_[key], `arguments.${key}`, maximum, true),
  } as Partial<Record<Key, string>>;
}

function parseMailArguments(
  arguments_: Record<string, unknown>,
): CapabilityArgumentsByName["iphone.mail.compose"] {
  requireAllowedKeys(arguments_, ["recipients", "subject", "body"], "arguments");
  return {
    ...optionalRecipients(arguments_),
    ...optionalTextArgument(arguments_, "subject", 500),
    ...optionalTextArgument(arguments_, "body", 10_000),
  };
}

function parseSmsArguments(
  arguments_: Record<string, unknown>,
): CapabilityArgumentsByName["iphone.sms.compose"] {
  requireAllowedKeys(arguments_, ["recipients", "message"], "arguments");
  return {
    ...optionalRecipients(arguments_),
    ...optionalTextArgument(arguments_, "message", 2_000),
  };
}

type CapabilityArgumentParsers = {
  [Name in IPhoneCapabilityName]: (
    arguments_: Record<string, unknown>,
  ) => CapabilityArgumentsByName[Name];
};

const CAPABILITY_ARGUMENT_PARSERS: CapabilityArgumentParsers = {
  "iphone.location.current": parseEmptyArguments,
  "iphone.contacts.lookup": parseContactsArguments,
  "iphone.calendar.events": parseCalendarArguments,
  "iphone.photos.pick": parseEmptyArguments,
  "iphone.mail.compose": parseMailArguments,
  "iphone.sms.compose": parseSmsArguments,
};

function parseArguments<Name extends IPhoneCapabilityName>(
  capability: Name,
  value: unknown,
): CapabilityArgumentsByName[Name] {
  const arguments_ = requireRecord(value, "arguments");
  if (!CAPABILITIES.has(capability)) {
    throw new CapabilityProtocolError("Canonical JSON contains an unsupported value.");
  }
  return CAPABILITY_ARGUMENT_PARSERS[capability](arguments_);
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new CapabilityProtocolError("Canonical JSON cannot contain a non-finite number.");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    const entries = Object.keys(value).sort().map((key) => {
      const entry = value[key];
      if (entry === undefined || typeof entry === "function" || typeof entry === "symbol") {
        throw new CapabilityProtocolError("Canonical JSON contains an unsupported value.");
      }
      return `${JSON.stringify(key)}:${canonicalJson(entry)}`;
    });
    return `{${entries.join(",")}}`;
  }
  throw new CapabilityProtocolError("Canonical JSON contains an unsupported value.");
}

function utf8Bytes(value: string): number[] {
  const bytes: number[] = [];
  for (const character of value) {
    const codePoint = character.codePointAt(0);
    if (codePoint === undefined) continue;
    if (codePoint <= 0x7f) bytes.push(codePoint);
    else if (codePoint <= 0x7ff) {
      bytes.push(0xc0 | (codePoint >>> 6), 0x80 | (codePoint & 0x3f));
    } else if (codePoint <= 0xffff) {
      bytes.push(
        0xe0 | (codePoint >>> 12),
        0x80 | ((codePoint >>> 6) & 0x3f),
        0x80 | (codePoint & 0x3f),
      );
    } else {
      bytes.push(
        0xf0 | (codePoint >>> 18),
        0x80 | ((codePoint >>> 12) & 0x3f),
        0x80 | ((codePoint >>> 6) & 0x3f),
        0x80 | (codePoint & 0x3f),
      );
    }
  }
  return bytes;
}

const SHA256_CONSTANTS = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

function rotateRight(value: number, count: number): number {
  return (value >>> count) | (value << (32 - count));
}

function sha256(value: string): string {
  const input = utf8Bytes(value);
  const bitLength = input.length * 8;
  const bytes = [...input, 0x80];
  while (bytes.length % 64 !== 56) bytes.push(0);
  const high = Math.floor(bitLength / 0x1_0000_0000);
  const low = bitLength >>> 0;
  for (let shift = 24; shift >= 0; shift -= 8) bytes.push((high >>> shift) & 0xff);
  for (let shift = 24; shift >= 0; shift -= 8) bytes.push((low >>> shift) & 0xff);

  const hash = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ];
  const words = new Array<number>(64).fill(0);
  for (let offset = 0; offset < bytes.length; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      const start = offset + index * 4;
      words[index] = (
        (bytes[start] << 24) |
        (bytes[start + 1] << 16) |
        (bytes[start + 2] << 8) |
        bytes[start + 3]
      ) >>> 0;
    }
    for (let index = 16; index < 64; index += 1) {
      const lower = words[index - 15];
      const upper = words[index - 2];
      const sigma0 = rotateRight(lower, 7) ^ rotateRight(lower, 18) ^ (lower >>> 3);
      const sigma1 = rotateRight(upper, 17) ^ rotateRight(upper, 19) ^ (upper >>> 10);
      words[index] = (words[index - 16] + sigma0 + words[index - 7] + sigma1) >>> 0;
    }

    let [a, b, c, d, e, f, g, h] = hash;
    for (let index = 0; index < 64; index += 1) {
      const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choice = (e & f) ^ (~e & g);
      const temporary1 = (h + sum1 + choice + SHA256_CONSTANTS[index] + words[index]) >>> 0;
      const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temporary2 = (sum0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temporary1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temporary1 + temporary2) >>> 0;
    }
    hash[0] = (hash[0] + a) >>> 0;
    hash[1] = (hash[1] + b) >>> 0;
    hash[2] = (hash[2] + c) >>> 0;
    hash[3] = (hash[3] + d) >>> 0;
    hash[4] = (hash[4] + e) >>> 0;
    hash[5] = (hash[5] + f) >>> 0;
    hash[6] = (hash[6] + g) >>> 0;
    hash[7] = (hash[7] + h) >>> 0;
  }
  return hash.map((word) => word.toString(16).padStart(8, "0")).join("");
}

export function capabilityGrantFingerprint(grantId: string): CapabilityActionDigest {
  return `sha256:${sha256(requireGrantId(grantId))}`;
}

export function capabilityActionDigest<Name extends IPhoneCapabilityName>(
  requestId: string,
  capability: Name,
  arguments_: CapabilityArgumentsByName[Name],
): CapabilityActionDigest {
  requireRequestId(requestId);
  const parsedArguments = parseArguments(capability, arguments_);
  const canonical = canonicalJson({
    arguments: parsedArguments,
    tool_call_id: requestId,
    tool_name: capability,
  });
  return `sha256:${sha256(canonical)}`;
}

function digestsMatch(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let different = 0;
  for (let index = 0; index < left.length; index += 1) {
    different |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return different === 0;
}

function parseIdentity(value: Record<string, unknown>) {
  if (value.schema_version !== IPHONE_CAPABILITY_SCHEMA_VERSION) {
    throw new CapabilityProtocolError("Unsupported iPhone capability schema version.");
  }
  const capability = requireCapability(value.capability);
  const createdAt = requireTimestamp(value.created_at, "created_at");
  const expiresAt = requireTimestamp(value.expires_at, "expires_at");
  if (expiresAt.milliseconds <= createdAt.milliseconds) {
    throw new CapabilityProtocolError("Capability request expiry is invalid.");
  }
  return {
    schema_version: IPHONE_CAPABILITY_SCHEMA_VERSION,
    request_id: requireRequestId(value.request_id),
    task_id: requireSafeId(value.task_id, "task_id"),
    agent_id: requireSafeId(value.agent_id, "agent_id"),
    target_device_id: requireSafeId(value.target_device_id, "target_device_id"),
    capability,
    status: requireStatus(value.status),
    created_at: createdAt.value,
    expires_at: expiresAt.value,
  };
}

function parseCapabilityRequestPreview(value: unknown): CapabilityRequestPreview {
  const record = requireRecord(value, "capability request preview");
  requireExactKeys(record, [
    "schema_version", "request_id", "task_id", "agent_id", "target_device_id",
    "capability", "status", "created_at", "expires_at",
  ], "capability request preview");
  return parseIdentity(record) as CapabilityRequestPreview;
}

export function parseCapabilityRequestList(value: unknown): CapabilityRequestPreview[] {
  if (!Array.isArray(value) || value.length > 200) {
    throw new CapabilityProtocolError("Capability request list must be an array.");
  }
  return value.map(parseCapabilityRequestPreview);
}

type ParsedTimestamp = ReturnType<typeof requireTimestamp>;

function assertGrantProtocol(record: Record<string, unknown>): void {
  if (record.schema_version !== IPHONE_CAPABILITY_SCHEMA_VERSION || record.use !== "once") {
    throw new CapabilityProtocolError("Capability grant is not a v0.9 one-use grant.");
  }
}

function parseGrantWindow(
  record: Record<string, unknown>,
  now: number,
): { expiresAt: ParsedTimestamp; issuedAt: ParsedTimestamp } {
  const issuedAt = requireTimestamp(record.issued_at, "grant.issued_at");
  const expiresAt = requireTimestamp(record.expires_at, "grant.expires_at");
  const invalidWindow = [
    expiresAt.milliseconds <= issuedAt.milliseconds,
    expiresAt.milliseconds - issuedAt.milliseconds > MAX_GRANT_LIFETIME_MS,
    issuedAt.milliseconds > now + CLOCK_SKEW_MS,
    expiresAt.milliseconds <= now,
  ].some(Boolean);
  if (invalidWindow) {
    throw new CapabilityProtocolError("Capability grant is expired or not short-lived.");
  }
  return { expiresAt, issuedAt };
}

function requireAuditId(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 1) {
    throw new CapabilityProtocolError("grant.audit_id is invalid.");
  }
  return Number(value);
}

function parseGrantFields(
  record: Record<string, unknown>,
  auditId: number,
  issuedAt: ParsedTimestamp,
  expiresAt: ParsedTimestamp,
): CapabilityGrant {
  return {
    schema_version: IPHONE_CAPABILITY_SCHEMA_VERSION,
    grant_id: requireGrantId(record.grant_id),
    request_id: requireRequestId(record.request_id),
    task_id: requireSafeId(record.task_id, "grant.task_id"),
    agent_id: requireSafeId(record.agent_id, "grant.agent_id"),
    target_device_id: requireSafeId(record.target_device_id, "grant.target_device_id"),
    approval_id: requireSafeId(record.approval_id, "grant.approval_id"),
    audit_id: auditId,
    capability: requireCapability(record.capability),
    action_digest: requireDigest(record.action_digest),
    issued_at: issuedAt.value,
    expires_at: expiresAt.value,
    use: "once",
  };
}

function assertGrantBinding(
  grant: CapabilityGrant,
  request: Omit<CapabilityRequestDetail, "grant">,
  expiresAt: ParsedTimestamp,
): void {
  const matches = [
    grant.request_id === request.request_id,
    grant.task_id === request.task_id,
    grant.agent_id === request.agent_id,
    grant.target_device_id === request.target_device_id,
    grant.capability === request.capability,
    digestsMatch(grant.action_digest, request.action_digest),
    expiresAt.milliseconds <= Date.parse(request.expires_at),
  ].every(Boolean);
  if (!matches) {
    throw new CapabilityProtocolError("Capability grant binding does not match its request.");
  }
}

function parseBoundGrant(
  value: unknown,
  request: Omit<CapabilityRequestDetail, "grant">,
  now: number,
): CapabilityGrant {
  const record = requireRecord(value, "capability grant");
  requireExactKeys(record, [
    "schema_version", "grant_id", "request_id", "task_id", "agent_id",
    "target_device_id", "approval_id", "audit_id", "capability", "action_digest",
    "issued_at", "expires_at", "use",
  ], "capability grant");
  assertGrantProtocol(record);
  const { expiresAt, issuedAt } = parseGrantWindow(record, now);
  const auditId = requireAuditId(record.audit_id);
  const grant = parseGrantFields(record, auditId, issuedAt, expiresAt);
  assertGrantBinding(grant, request, expiresAt);
  return grant;
}

export function parseCapabilityRequestDetail(
  value: unknown,
  now = Date.now(),
): CapabilityRequestDetail {
  const record = requireRecord(value, "capability request detail");
  requireExactKeys(record, [
    "schema_version", "request_id", "task_id", "agent_id", "target_device_id",
    "capability", "status", "created_at", "expires_at", "arguments", "action_digest", "grant",
  ], "capability request detail");
  const identity = parseIdentity(record);
  const arguments_ = parseArguments(identity.capability, record.arguments);
  const actionDigest = requireDigest(record.action_digest);
  const expectedDigest = capabilityActionDigest(identity.request_id, identity.capability, arguments_);
  if (!digestsMatch(actionDigest, expectedDigest)) {
    throw new CapabilityProtocolError("Capability request arguments do not match action_digest.");
  }
  const withoutGrant = {
    ...identity,
    arguments: arguments_,
    action_digest: actionDigest,
  } as Omit<CapabilityRequestDetail, "grant">;
  const grant = record.grant === null ? null : parseBoundGrant(record.grant, withoutGrant, now);
  if (grant !== null && identity.status !== "approved") {
    throw new CapabilityProtocolError("Only an approved request may contain a grant.");
  }
  if (identity.status === "denied" && grant !== null) {
    throw new CapabilityProtocolError("A denied request cannot contain a grant.");
  }
  return { ...withoutGrant, grant } as CapabilityRequestDetail;
}

export function parseCapabilityRequestLookup(
  value: unknown,
  now = Date.now(),
): CapabilityRequestDetail {
  const request = parseCapabilityRequestDetail(value, now);
  if (request.grant !== null) {
    throw new CapabilityProtocolError("A capability lookup must not disclose the one-use grant.");
  }
  return request;
}

export function parseCapabilityRequestEnvelope(
  value: unknown,
  now = Date.now(),
): CapabilityRequestEnvelope {
  const request = parseCapabilityRequestDetail(value, now);
  if (request.status !== "approved" || request.grant === null) {
    throw new CapabilityProtocolError("An approved one-use grant envelope is required.");
  }
  assertCapabilityRequestFresh(request, now);
  return request as CapabilityRequestEnvelope;
}

export function parseCapabilityAuthorizationResponse(
  value: unknown,
  decision: CapabilityAuthorizationDecision,
  expected: CapabilityRequestDetail,
  now = Date.now(),
): CapabilityAuthorizationResponse {
  const response = parseCapabilityRequestDetail(value, now);
  if (
    response.request_id !== expected.request_id ||
    response.task_id !== expected.task_id ||
    response.agent_id !== expected.agent_id ||
    response.target_device_id !== expected.target_device_id ||
    response.capability !== expected.capability ||
    response.created_at !== expected.created_at ||
    response.expires_at !== expected.expires_at ||
    !digestsMatch(response.action_digest, expected.action_digest)
  ) {
    throw new CapabilityProtocolError("Authorization response changed the capability request.");
  }
  if (decision === "approve") return parseCapabilityRequestEnvelope(response, now);
  if (response.status !== "denied" || response.grant !== null) {
    throw new CapabilityProtocolError("Denied authorization did not return a terminal request.");
  }
  return response as CapabilityAuthorizationResponse;
}

export function parseCapabilityNotification(
  value: unknown,
  now = Date.now(),
): CapabilityNotification {
  const record = requireRecord(value, "capability notification");
  requireExactKeys(
    record,
    ["request_id", "capability_name", "expires_at", "preview"],
    "capability notification",
  );
  const preview = requireRecord(record.preview, "capability notification preview");
  requireExactKeys(preview, ["arguments_redacted"], "capability notification preview");
  if (preview.arguments_redacted !== true) {
    throw new CapabilityProtocolError("Capability notification arguments must be redacted.");
  }
  const expiresAt = requireTimestamp(record.expires_at, "notification.expires_at");
  if (expiresAt.milliseconds <= now) {
    throw new CapabilityProtocolError("Capability notification is expired.");
  }
  return {
    request_id: requireRequestId(record.request_id),
    capability_name: requireCapability(record.capability_name),
    expires_at: expiresAt.value,
    preview: { arguments_redacted: true },
  };
}

export function assertCapabilityRequestFresh(
  request: CapabilityRequestDetail | CapabilityRequestEnvelope,
  now = Date.now(),
): void {
  if (Date.parse(request.expires_at) <= now) {
    throw new CapabilityProtocolError("Capability request is expired.");
  }
}

export function parseCapabilityConsumeReceipt(
  value: unknown,
  request: CapabilityRequestEnvelope,
  now = Date.now(),
): ConsumedCapabilityGrant {
  const record = requireRecord(value, "capability consume receipt");
  requireExactKeys(record, [
    "status", "request_id", "grant_id", "action_digest", "consumed_at",
  ], "capability consume receipt");
  if (record.status !== "consumed") {
    throw new CapabilityProtocolError("Server did not consume the grant.");
  }
  const receipt: CapabilityConsumeReceipt = {
    status: "consumed",
    request_id: requireRequestId(record.request_id),
    grant_id: requireGrantId(record.grant_id),
    action_digest: requireDigest(record.action_digest),
    consumed_at: requireTimestamp(record.consumed_at, "consumed_at").value,
  };
  const consumedAt = Date.parse(receipt.consumed_at);
  if (
    receipt.request_id !== request.request_id ||
    receipt.grant_id !== request.grant.grant_id ||
    !digestsMatch(receipt.action_digest, request.action_digest) ||
    consumedAt < Date.parse(request.grant.issued_at) - CLOCK_SKEW_MS ||
    consumedAt > Date.parse(request.grant.expires_at) ||
    consumedAt > now + CLOCK_SKEW_MS
  ) {
    throw new CapabilityProtocolError("Consume receipt does not match the one-use grant.");
  }
  return {
    ...receipt,
    capability: request.capability,
    grant_expires_at: request.grant.expires_at,
  } as ConsumedCapabilityGrant;
}

export function parseCapabilityResultReceipt(
  value: unknown,
  request: CapabilityRequestEnvelope,
): CapabilityResultReceipt {
  const record = requireRecord(value, "capability result receipt");
  requireExactKeys(record, ["status", "request_id", "grant_id"], "capability result receipt");
  if (record.status !== "accepted" && record.status !== "duplicate") {
    throw new CapabilityProtocolError("Capability result was not accepted.");
  }
  const receipt: CapabilityResultReceipt = {
    status: record.status,
    request_id: requireRequestId(record.request_id),
    grant_id: requireGrantId(record.grant_id),
  };
  if (receipt.request_id !== request.request_id || receipt.grant_id !== request.grant.grant_id) {
    throw new CapabilityProtocolError("Result receipt does not match the capability grant.");
  }
  return receipt;
}

function finiteNumber(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new CapabilityProtocolError(`${label} is invalid.`);
  }
  return value;
}

function parseDeniedResult(record: Record<string, unknown>): CapabilityResult {
  requireExactKeys(record, ["name", "status", "reason", "value"], "capability result");
  const invalidReason = record.reason !== "permission_denied" && record.reason !== "unavailable";
  if (invalidReason || record.value !== null) {
    throw new CapabilityProtocolError("Capability denial result is invalid.");
  }
  return record as CapabilityResult;
}

function parseFailedResult(record: Record<string, unknown>): CapabilityResult {
  requireExactKeys(record, ["name", "status", "reason", "value"], "capability result");
  if (record.reason !== "native_error" || record.value !== null) {
    throw new CapabilityProtocolError("Capability failure result is invalid.");
  }
  return record as CapabilityResult;
}

const CANCELLABLE_CAPABILITIES = new Set<IPhoneCapabilityName>([
  "iphone.photos.pick",
  "iphone.mail.compose",
  "iphone.sms.compose",
]);

function parseCancelledResult(
  record: Record<string, unknown>,
  expectedCapability: IPhoneCapabilityName,
): CapabilityResult {
  requireExactKeys(record, ["name", "status", "value"], "capability result");
  if (!CANCELLABLE_CAPABILITIES.has(expectedCapability) || record.value !== null) {
    throw new CapabilityProtocolError("Capability cancellation result is invalid.");
  }
  return record as CapabilityResult;
}

function validateLocationResult(value: unknown): void {
  const location = requireRecord(value, "location result");
  requireExactKeys(location, ["latitude", "longitude", "accuracy"], "location result");
  const latitude = finiteNumber(location.latitude, "latitude");
  const longitude = finiteNumber(location.longitude, "longitude");
  const outsideBounds = latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180;
  if (outsideBounds) {
    throw new CapabilityProtocolError("Location result is outside valid bounds.");
  }
  if (location.accuracy !== null) finiteNumber(location.accuracy, "accuracy");
}

function validateContactResult(value: unknown): void {
  const contact = requireRecord(value, "contact result");
  requireExactKeys(contact, ["id", "name", "phoneNumbers", "emails"], "contact result");
  requireString(contact.id, "contact.id", 500);
  requireText(contact.name, "contact.name", 1_000, true);
  requireStringArray(contact.phoneNumbers, "contact.phoneNumbers", 20, true);
  requireStringArray(contact.emails, "contact.emails", 20, true);
}

function validateContactsResult(value: unknown): void {
  if (!Array.isArray(value) || value.length > 25) {
    throw new CapabilityProtocolError("Contacts result is invalid.");
  }
  value.forEach(validateContactResult);
}

function validateCalendarEventResult(value: unknown): void {
  const event = requireRecord(value, "calendar result");
  requireExactKeys(event, ["id", "title", "start", "end"], "calendar result");
  requireString(event.id, "event.id", 500);
  requireText(event.title, "event.title", 2_000, true);
  requireTimestamp(event.start, "event.start");
  requireTimestamp(event.end, "event.end");
}

function validateCalendarResult(value: unknown): void {
  if (!Array.isArray(value) || value.length > 100) {
    throw new CapabilityProtocolError("Calendar result is invalid.");
  }
  value.forEach(validateCalendarEventResult);
}

function validatePhotoResult(value: unknown): void {
  const asset = requireRecord(value, "photo result");
  requireExactKeys(asset, ["uri", "width", "height"], "photo result");
  requireString(asset.uri, "photo.uri", 8_000);
  const width = finiteNumber(asset.width, "photo.width");
  if (width <= 0) {
    throw new CapabilityProtocolError("Photo dimensions are invalid.");
  }
  if (finiteNumber(asset.height, "photo.height") <= 0) {
    throw new CapabilityProtocolError("Photo dimensions are invalid.");
  }
}

function validateComposerResult(value: unknown): void {
  const composed = requireRecord(value, "composer result");
  requireExactKeys(composed, ["composed"], "composer result");
  if (composed.composed !== true) {
    throw new CapabilityProtocolError("Composer result is invalid.");
  }
}

const COMPLETED_RESULT_VALIDATORS: Record<IPhoneCapabilityName, (value: unknown) => void> = {
  "iphone.location.current": validateLocationResult,
  "iphone.contacts.lookup": validateContactsResult,
  "iphone.calendar.events": validateCalendarResult,
  "iphone.photos.pick": validatePhotoResult,
  "iphone.mail.compose": validateComposerResult,
  "iphone.sms.compose": validateComposerResult,
};

function parseCompletedResult(
  record: Record<string, unknown>,
  expectedCapability: IPhoneCapabilityName,
): CapabilityResult {
  requireExactKeys(record, ["name", "status", "value"], "capability result");
  COMPLETED_RESULT_VALIDATORS[expectedCapability](record.value);
  return record as CapabilityResult;
}

type CapabilityResultParser = (
  record: Record<string, unknown>,
  expectedCapability: IPhoneCapabilityName,
) => CapabilityResult;

const CAPABILITY_RESULT_PARSERS = new Map<CapabilityResult["status"], CapabilityResultParser>([
  ["denied", parseDeniedResult],
  ["failed", parseFailedResult],
  ["cancelled", parseCancelledResult],
  ["completed", parseCompletedResult],
]);

function requireResultParser(status: string): CapabilityResultParser {
  const parser = CAPABILITY_RESULT_PARSERS.get(status as CapabilityResult["status"]);
  if (!parser) {
    throw new CapabilityProtocolError("Capability result status is invalid.");
  }
  return parser;
}

export function parseCapabilityResult(
  value: unknown,
  expectedCapability: IPhoneCapabilityName,
): CapabilityResult {
  const outer = requireRecord(value, "capability result");
  if (outer.name !== expectedCapability || typeof outer.status !== "string") {
    throw new CapabilityProtocolError("Capability result does not match the request.");
  }
  return requireResultParser(outer.status)(outer, expectedCapability);
}
