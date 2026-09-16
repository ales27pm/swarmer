import * as SecureStore from "expo-secure-store";

import { pairDevice, type PairingResult } from "@/lib/api/client";

const DEVICE_ID_KEY = "mongars.device_id";
let pendingDeviceId: Promise<string> | null = null;

/** One persistent identity, even when UI and a remote request arrive together. */
export async function getApplicationDeviceId(): Promise<string> {
  if (pendingDeviceId) return pendingDeviceId;
  pendingDeviceId = (async () => {
    const existing = await SecureStore.getItemAsync(DEVICE_ID_KEY);
    if (existing) return existing;
    const id = `iphone_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    await SecureStore.setItemAsync(DEVICE_ID_KEY, id);
    return id;
  })();
  try { return await pendingDeviceId; } finally { pendingDeviceId = null; }
}

export type PairApplicationConnectionInput = { code: string; deviceName: string; serverUrl: string };

/** Backend pairing already stages and verifies the new connection before promotion. */
export async function pairApplicationConnection(input: PairApplicationConnectionInput): Promise<PairingResult> {
  return pairDevice(input.code, await getApplicationDeviceId(), input.deviceName, input.serverUrl);
}
