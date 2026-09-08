import { describe, expect, it, jest } from "@jest/globals";

import { CapabilityDeniedError, IPhoneCapabilityBroker } from "./index";

jest.mock("expo-location", () => ({}));
jest.mock("expo-contacts", () => ({}));
jest.mock("expo-calendar", () => ({}));
jest.mock("expo-image-picker", () => ({}));
jest.mock("expo-mail-composer", () => ({}));
jest.mock("expo-sms", () => ({}));

describe("iPhone capability broker", () => {
  it("requires fresh server gateway authorization before touching iOS permissions", async () => {
    const native = jest.fn<(arguments_: never) => Promise<never>>();
    const broker = new IPhoneCapabilityBroker(async () => ({ authorized: false, reason: "approval required" }), { "iphone.location.current": native });
    await expect(broker.execute({ name: "iphone.location.current", arguments: {} })).rejects.toBeInstanceOf(CapabilityDeniedError);
    expect(native).not.toHaveBeenCalled();
  });

  it("returns a minimal typed location result", async () => {
    const native = jest.fn<(arguments_: never) => Promise<{ name: "iphone.location.current"; status: "completed"; value: { latitude: number; longitude: number; accuracy: number } }>>()
      .mockResolvedValue({ name: "iphone.location.current", status: "completed", value: { latitude: 45.5, longitude: -73.6, accuracy: 10 } });
    const broker = new IPhoneCapabilityBroker(async () => ({ authorized: true, authorizationId: "apr_1" }), { "iphone.location.current": native });
    await expect(broker.execute({ name: "iphone.location.current", arguments: {} })).resolves.toEqual({
      name: "iphone.location.current", status: "completed",
      value: { latitude: 45.5, longitude: -73.6, accuracy: 10 },
    });
  });
});
