import * as SecureStore from "expo-secure-store";

const SERVER_URL_KEY = "mongars.server_url";
const TOKEN_KEY = "mongars.device_token";

export type Task = {
  id: string;
  status: string;
  input: string;
};

export async function getServerUrl() {
  return (await SecureStore.getItemAsync(SERVER_URL_KEY)) ?? "http://127.0.0.1:8710";
}

export async function setServerUrl(value: string) {
  await SecureStore.setItemAsync(SERVER_URL_KEY, value.replace(/\/$/, ""));
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const [baseUrl, token] = await Promise.all([
    getServerUrl(),
    SecureStore.getItemAsync(TOKEN_KEY),
  ]);

  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`HTTP ${response.status}${detail ? ` — ${detail}` : ""}`);
  }

  return response.json() as Promise<T>;
}

export function createTask(input: string) {
  return request<Task>("/tasks", {
    method: "POST",
    body: JSON.stringify({ input, mode: "normal", source: "iphone" }),
  });
}
