import * as SecureStore from "expo-secure-store";
import { pairDevice } from "@/lib/api/client";
import { getApplicationDeviceId, pairApplicationConnection } from "./connection";

jest.mock("expo-secure-store", () => ({ getItemAsync: jest.fn(), setItemAsync: jest.fn() }));
jest.mock("@/lib/api/client", () => ({ pairDevice: jest.fn() }));

beforeEach(() => { jest.clearAllMocks(); });

test("UI and API concurrent pairing share the saved device identity", async () => {
  jest.mocked(SecureStore.getItemAsync).mockResolvedValue(null);
  jest.mocked(SecureStore.setItemAsync).mockResolvedValue();
  const [first, second] = await Promise.all([getApplicationDeviceId(), getApplicationDeviceId()]);
  expect(first).toBe(second);
  expect(SecureStore.setItemAsync).toHaveBeenCalledTimes(1);
});

test("preserves an existing identity and delegates the verified pairing transaction", async () => {
  jest.mocked(SecureStore.getItemAsync).mockResolvedValue("iphone_existing");
  const result = { serverUrl: "https://server.example", bootstrap: { counts: {} } };
  jest.mocked(pairDevice).mockResolvedValue(result as Awaited<ReturnType<typeof pairDevice>>);
  await expect(pairApplicationConnection({ code: "123456", deviceName: "Test", serverUrl: "https://server.example" })).resolves.toBe(result);
  expect(pairDevice).toHaveBeenCalledWith("123456", "iphone_existing", "Test", "https://server.example");
  expect(SecureStore.setItemAsync).not.toHaveBeenCalled();
});
