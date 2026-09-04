import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";
import { applyBootstrap, upsertEvent } from "@/lib/state/replica";

const SERVER_URL_KEY = "mongars.server_url";
const TOKEN_KEY = "mongars.device_token";

export type Task = { id: string; status: string; input: string };
export type Approval = { id: string; task_id: string; action: string; summary: string; risk: string; status: string; created_at: string; decided_at?: string | null };

export async function getServerUrl() {
  return (await SecureStore.getItemAsync(SERVER_URL_KEY)) ?? "http://127.0.0.1:8710";
}

export async function setServerUrl(value: string) {
  await SecureStore.setItemAsync(SERVER_URL_KEY, value.replace(/\/$/, ""));
}

async function getToken() {
  return SecureStore.getItemAsync(TOKEN_KEY);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const [baseUrl, token] = await Promise.all([getServerUrl(), getToken()]);
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init?.headers },
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`HTTP ${response.status}${detail ? ` — ${detail}` : ""}`);
  }
  return response.json() as Promise<T>;
}

export async function pairDevice(code: string, deviceId: string, name = "iPhone") {
  const result = await request<{ token: string }>("/pairing/complete", { method: "POST", body: JSON.stringify({ code, device_id: deviceId, name }) });
  await SecureStore.setItemAsync(TOKEN_KEY, result.token);
  return result;
}

export function createTask(input: string) {
  return request<Task>("/tasks", { method: "POST", body: JSON.stringify({ input, mode: "normal", source: "iphone" }) });
}

export async function bootstrapSync() {
  const data = await request<{ tasks: any[]; approvals: any[]; cursor: string }>("/sync/bootstrap");
  await applyBootstrap(data);
  return data;
}

export function listApprovals() {
  return request<Approval[]>("/approvals");
}

export async function decideApproval(id: string, decision: "approve" | "deny") {
  const result = await request<Approval>(`/approvals/${id}/decision`, { method: "POST", body: JSON.stringify({ decision }) });
  await upsertEvent("approval.decided", result);
  return result;
}

export async function connectEvents(onEvent?: (event: any) => void) {
  const [baseUrl, token] = await Promise.all([getServerUrl(), getToken()]);
  if (!token) throw new Error("Device not paired");
  const wsUrl = baseUrl.replace(/^http/, "ws") + `/ws?token=${encodeURIComponent(token)}`;
  const ws = new WebSocket(wsUrl);
  ws.onmessage = async ({ data }) => {
    const event = JSON.parse(String(data));
    if (event.payload?.id) await upsertEvent(event.type, event.payload);
    onEvent?.(event);
  };
  return ws;
}
