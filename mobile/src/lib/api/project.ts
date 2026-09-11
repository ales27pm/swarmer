import type { GoalDetail } from "@/lib/api/types";
import type { CodeProposalApplication } from "@/lib/api/code-proposal";

export type ProjectCheck = {
  command: string[];
  status: "passed" | "failed" | "skipped";
  exit_code: number | null;
  output: string;
  duration_ms: number;
};
export type ProjectFile = { path: string; content: string };
export type ProjectPreview = {
  project_id: string;
  revision_id: string;
  revision: number;
  sha256: string;
  state: "building" | "needs_user" | "ready" | "waiting_permission" | "applied" | "failed";
  message: string;
  plan: string[];
  files: ProjectFile[];
  checks: ProjectCheck[];
  run_instructions: string;
  runtime: "python" | "node" | "python_node";
  task_id: string | null;
};
export type ProjectReview = { project: ProjectPreview; prepareApproval: () => Promise<CodeProposalApplication> };
export type GoalMessage = {
  id: string;
  goal_run_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
};
export type GoalConversation = {
  messages: GoalMessage[];
  active_goal_id: string;
  pending_question_id: string | null;
};
export type GoalReplyAttempt = { clientMessageId: string; send: () => Promise<GoalDetail> };
export type GoalConversationSession = {
  conversation: GoalConversation;
  prepareReply: (message: string) => GoalReplyAttempt;
};

function invalid(): never { throw new Error("Les données du projet reçues sont invalides ou dépassent les limites."); }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
export function projectIdentifier(value: unknown): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{1,200}$/.test(value)) return invalid();
  return value;
}
function text(value: unknown, limit: number): string {
  if (typeof value !== "string" || Array.from(value).length > limit || value.includes("\0")) return invalid();
  utf8Bytes(value);
  return value;
}
function utf8Bytes(value: string): number {
  let bytes = 0;
  for (const character of value) {
    const point = character.codePointAt(0) as number;
    if (point >= 0xd800 && point <= 0xdfff) return invalid();
    bytes += point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4;
  }
  return bytes;
}
function list(value: unknown, limit: number): unknown[] {
  if (!Array.isArray(value) || value.length > limit) return invalid();
  return value;
}
function choice<T extends string>(value: unknown, choices: readonly T[]): T {
  if (typeof value !== "string" || !choices.includes(value as T)) return invalid();
  return value as T;
}
function integer(value: unknown, minimum: number): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) return invalid();
  return value;
}
function filePath(value: unknown): string {
  const path = text(value, 240);
  const parts = path.split("/");
  if (!path || !/^[A-Za-z0-9_.@/-]+$/.test(path) || path.startsWith("/")) return invalid();
  if (parts.some((part) => !part || part === "." || part === ".." || part.endsWith(".") || part.endsWith(" "))) return invalid();
  const forbidden = /^(?:\.git|\.ssh|\.aws|\.codex|\.venv|node_modules|\.npmrc|\.pypirc|auth\.json|id_rsa|id_ed25519|credentials\.json)$/i;
  const environmentTemplates = new Set([".env.example", ".env.sample", ".env.template"]);
  if (parts.some((part) => forbidden.test(part)
    || (/^\.env/i.test(part) && !environmentTemplates.has(part.toLowerCase()))
    || /\.(?:pem|p12|pfx|key)$/i.test(part))) return invalid();
  return path;
}
function parseCheck(value: unknown): ProjectCheck {
  const item = record(value);
  const status = choice(item.status, ["passed", "failed", "skipped"]);
  const exit = item.exit_code === null ? null : integer(item.exit_code, -255);
  if (exit !== null && exit > 255) return invalid();
  if ((status === "passed" && exit !== 0) || (status === "failed" && exit === 0)) return invalid();
  const command = list(item.command, 24).map((part) => text(part, 500));
  if (!command.length || command.some((part) => !part)) return invalid();
  const duration = integer(item.duration_ms, 0);
  if (duration > 900_000) return invalid();
  return { command, status, exit_code: exit, output: text(item.output, 8_000), duration_ms: duration };
}

export function parseProjectPreview(value: unknown): ProjectPreview {
  const item = record(value);
  const paths = new Set<string>();
  let total = 0;
  const files = list(item.files, 80).map((value) => {
    const file = record(value);
    const path = filePath(file.path);
    const folded = path.normalize("NFC").toLowerCase();
    if (paths.has(folded)) return invalid();
    paths.add(folded);
    const content = text(file.content, 64_000);
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(content)) return invalid();
    const bytes = utf8Bytes(content);
    total += bytes;
    if (bytes > 64_000 || total > 1_000_000) return invalid();
    return { path, content };
  });
  for (const path of paths) {
    const parts = path.split("/");
    if (parts.some((_, index) => index > 0 && paths.has(parts.slice(0, index).join("/")))) return invalid();
  }
  if (typeof item.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(item.sha256)) return invalid();
  const state = choice(item.state, ["building", "needs_user", "ready", "waiting_permission", "applied", "failed"]);
  const taskId = item.task_id === null ? null : projectIdentifier(item.task_id);
  if ((state === "waiting_permission" || state === "applied") && taskId === null) return invalid();
  return {
    project_id: projectIdentifier(item.project_id), revision_id: projectIdentifier(item.revision_id),
    revision: integer(item.revision, 1), sha256: item.sha256, state,
    message: text(item.message, 4_000), plan: list(item.plan, 20).map((entry) => text(entry, 4_000)),
    files, checks: list(item.checks, 12).map(parseCheck), run_instructions: text(item.run_instructions, 4_000),
    runtime: choice(item.runtime, ["python", "node", "python_node"]), task_id: taskId,
  };
}

export function parseGoalConversation(value: unknown): GoalConversation {
  const item = record(value);
  const ids = new Set<string>();
  const messages = list(item.messages, 100).map((value): GoalMessage => {
    const message = record(value);
    const id = projectIdentifier(message.id);
    if (ids.has(id)) return invalid();
    ids.add(id);
    const created = text(message.created_at, 80);
    if (!Number.isFinite(Date.parse(created))) return invalid();
    return { id, goal_run_id: projectIdentifier(message.goal_run_id), role: choice(message.role, ["user", "assistant"]), content: text(message.content, 4_000), created_at: created };
  });
  const pending = item.pending_question_id === null ? null : projectIdentifier(item.pending_question_id);
  if (pending && !messages.some((message) => message.id === pending && message.role === "assistant")) return invalid();
  return { messages, active_goal_id: projectIdentifier(item.active_goal_id), pending_question_id: pending };
}

export function validateGoalReply(value: string): string {
  const message = text(value.trim(), 4_000);
  if (!message) return invalid();
  return message;
}

export function newGoalMessageId(): string {
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") globalThis.crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  return `reply_${Date.now().toString(36)}_${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}
