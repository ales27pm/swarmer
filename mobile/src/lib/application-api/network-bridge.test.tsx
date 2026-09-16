import { act, render } from "@testing-library/react-native";
import { AppState } from "react-native";
import { requireOptionalNativeModule } from "expo";

import { ApplicationNetworkBridge } from "./network-bridge";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn() }));
jest.mock("@/lib/application-api", () => ({ applicationApi: {
  catalog: jest.fn(() => ({ schemaVersion: "1.0", commands: [] })),
  execute: jest.fn(async () => ({ data: "ok" })),
} }));

let receive: (request: { requestId: string; method: string; path: string; body: string }) => void;
let appState: (state: string) => void;
let removed: jest.Mock;
let native: {
  automationAvailable: boolean;
  addListener: jest.Mock;
  startAutomationServer: jest.Mock;
  stopAutomationServer: jest.Mock;
  completeAutomationRequest: jest.Mock;
};

beforeEach(() => {
  removed = jest.fn();
  native = {
    automationAvailable: true,
    addListener: jest.fn((_name, listener) => { receive = listener; return { remove: removed }; }),
    startAutomationServer: jest.fn(async () => ({ enabled: true, port: 8766 })),
    stopAutomationServer: jest.fn(async () => {}),
    completeAutomationRequest: jest.fn(async () => {}),
  };
  jest.mocked(requireOptionalNativeModule).mockReturnValue(native);
  Object.defineProperty(AppState, "currentState", { configurable: true, writable: true, value: "active" });
  jest.spyOn(AppState, "addEventListener").mockImplementation((_type, listener) => {
    appState = listener as (state: string) => void;
    return { remove: jest.fn() };
  });
});
afterEach(() => { jest.restoreAllMocks(); });

test("embedded production JS can connect to a native Debug transport", async () => {
  const runtimeGlobals = globalThis as typeof globalThis & { __DEV__: boolean };
  const previous = runtimeGlobals.__DEV__;
  runtimeGlobals.__DEV__ = false;
  try {
    const view = await render(<ApplicationNetworkBridge />);
    expect(native.startAutomationServer).toHaveBeenCalledTimes(1);
    await view.unmount();
  } finally { runtimeGlobals.__DEV__ = previous; }
});

test("a native Release build never attaches a transport even with development JS", async () => {
  native.automationAvailable = false;
  const view = await render(<ApplicationNetworkBridge />);
  expect(native.addListener).not.toHaveBeenCalled();
  expect(native.startAutomationServer).not.toHaveBeenCalled();
  await view.unmount();
});

test("attaches before starting and returns real protocol data through the native bridge", async () => {
  const view = await render(<ApplicationNetworkBridge />);
  await act(async () => {
    receive({ requestId: "native-request", method: "GET", path: "/v1/health", body: "" });
  });
  expect(native.addListener.mock.invocationCallOrder[0]).toBeLessThan(native.startAutomationServer.mock.invocationCallOrder[0]);
  expect(native.completeAutomationRequest).toHaveBeenCalledWith("native-request", 200, expect.any(String));
  expect(JSON.parse(native.completeAutomationRequest.mock.calls[0][2])).toMatchObject({ state: "ready", persistence: "javascript_session" });
  await view.unmount();
  expect(removed).toHaveBeenCalled();
  expect(native.stopAutomationServer).toHaveBeenCalled();
});

test("background pauses command acceptance and foreground resumes the same instance", async () => {
  const view = await render(<ApplicationNetworkBridge />);
  const readHealth = () => receive({ requestId: "health", method: "GET", path: "/v1/health", body: "" });
  await act(async () => { readHealth(); });
  const initial = JSON.parse(native.completeAutomationRequest.mock.calls.at(-1)![2]);
  await act(async () => {
    Object.assign(AppState, { currentState: "background" });
    appState("background");
    readHealth();
  });
  expect(native.stopAutomationServer).toHaveBeenCalled();
  expect(native.completeAutomationRequest.mock.calls.at(-1)![1]).toBe(503);
  await act(async () => {
    Object.assign(AppState, { currentState: "active" });
    appState("active");
    readHealth();
  });
  expect(JSON.parse(native.completeAutomationRequest.mock.calls.at(-1)![2]).instanceId).toBe(initial.instanceId);
  await view.unmount();
});

test("a rejected native reply never retries dispatch", async () => {
  native.completeAutomationRequest.mockRejectedValue(new Error("connection lost"));
  const view = await render(<ApplicationNetworkBridge />);
  await act(async () => { receive({ requestId: "gone", method: "GET", path: "/v1/catalog", body: "" }); });
  expect(native.completeAutomationRequest).toHaveBeenCalledTimes(1);
  await view.unmount();
});
