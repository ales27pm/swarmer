import { describe, expect, it, jest } from "@jest/globals";
import * as Calendar from "expo-calendar/legacy";
import * as Contacts from "expo-contacts/legacy";
import * as Location from "expo-location";
import * as MailComposer from "expo-mail-composer";
import * as SMS from "expo-sms";

import {
  CapabilityDeniedError,
  CapabilityReplayError,
  IPhoneCapabilityBroker,
  capabilityActionDigest,
  parseCapabilityConsumeReceipt,
  type CapabilityRequest,
  type CapabilityRequestEnvelope,
} from "./index";

jest.mock("expo-location", () => ({
  Accuracy: { Balanced: 3 },
  getCurrentPositionAsync: jest.fn(),
  requestForegroundPermissionsAsync: jest.fn(),
}));
jest.mock("expo-contacts/legacy", () => ({
  Fields: { Emails: "emails", PhoneNumbers: "phoneNumbers" },
  getContactsAsync: jest.fn(),
  requestPermissionsAsync: jest.fn(),
}));
jest.mock("expo-calendar/legacy", () => ({
  EntityTypes: { EVENT: "event" },
  getCalendarsAsync: jest.fn(),
  getEventsAsync: jest.fn(),
  requestCalendarPermissionsAsync: jest.fn(),
}));
jest.mock("expo-contacts", () => {
  throw new Error("the SDK 57 top-level legacy wrapper must not be imported");
});
jest.mock("expo-calendar", () => {
  throw new Error("the SDK 57 top-level legacy wrapper must not be imported");
});
jest.mock("expo-image-picker", () => ({}));
jest.mock("expo-mail-composer", () => ({
  MailComposerStatus: {
    CANCELLED: "cancelled",
    SAVED: "saved",
    SENT: "sent",
    UNDETERMINED: "undetermined",
  },
  composeAsync: jest.fn(),
  isAvailableAsync: jest.fn(),
}));
jest.mock("expo-sms", () => ({
  isAvailableAsync: jest.fn(),
  sendSMSAsync: jest.fn(),
}));

const NOW = Date.parse("2026-09-08T12:00:30.000Z");
let sequence = 0;

function authorization(request: CapabilityRequest) {
  sequence += 1;
  const requestId = `iphreq_${sequence.toString(16).padStart(32, "a")}`;
  const grantId = `grt_${sequence.toString(16).padStart(64, "b")}`;
  const actionDigest = capabilityActionDigest(requestId, request.name, request.arguments as never);
  const envelope = {
    schema_version: "0.9",
    request_id: requestId,
    task_id: `tsk_${"c".repeat(32)}`,
    agent_id: "test-agent",
    target_device_id: "iphone_test",
    capability: request.name,
    status: "approved",
    created_at: "2026-09-08T11:59:00.000Z",
    expires_at: "2026-09-08T12:03:00.000Z",
    arguments: request.arguments,
    action_digest: actionDigest,
    grant: {
      schema_version: "0.9",
      grant_id: grantId,
      request_id: requestId,
      task_id: `tsk_${"c".repeat(32)}`,
      agent_id: "test-agent",
      target_device_id: "iphone_test",
      approval_id: `apr_${"d".repeat(32)}`,
      audit_id: sequence,
      capability: request.name,
      action_digest: actionDigest,
      issued_at: "2026-09-08T12:00:00.000Z",
      expires_at: "2026-09-08T12:01:00.000Z",
      use: "once",
    },
  } as CapabilityRequestEnvelope;
  return parseCapabilityConsumeReceipt({
    status: "consumed",
    request_id: requestId,
    grant_id: grantId,
    action_digest: actionDigest,
    consumed_at: "2026-09-08T12:00:31.000Z",
  }, envelope, NOW);
}

describe("iPhone capability broker", () => {
  it("requires an exact consumed server grant before touching native APIs", async () => {
    const native = jest.fn(async () => ({
      name: "iphone.location.current" as const,
      status: "completed" as const,
      value: { latitude: 45.5, longitude: -73.6, accuracy: 10 },
    }));
    const request = { name: "iphone.location.current", arguments: {} } as const;
    const broker = new IPhoneCapabilityBroker({ "iphone.location.current": native }, () => NOW);
    const wrong = { ...authorization(request), action_digest: `sha256:${"0".repeat(64)}` };

    await expect(broker.execute(request, wrong as never)).rejects.toBeInstanceOf(CapabilityDeniedError);
    expect(native).not.toHaveBeenCalled();
  });

  it("returns a minimal typed result once and rejects replay", async () => {
    const native = jest.fn(async () => ({
      name: "iphone.location.current" as const,
      status: "completed" as const,
      value: { latitude: 45.5, longitude: -73.6, accuracy: 10 },
    }));
    const request = { name: "iphone.location.current", arguments: {} } as const;
    const grant = authorization(request);
    const broker = new IPhoneCapabilityBroker({ "iphone.location.current": native }, () => NOW);

    await expect(broker.execute(request, grant)).resolves.toEqual({
      name: "iphone.location.current",
      status: "completed",
      value: { latitude: 45.5, longitude: -73.6, accuracy: 10 },
    });
    await expect(broker.execute(request, grant)).rejects.toBeInstanceOf(CapabilityReplayError);
    expect(native).toHaveBeenCalledTimes(1);
  });

  it("returns a strict denial when iOS location permission is denied", async () => {
    jest.mocked(Location.requestForegroundPermissionsAsync).mockResolvedValueOnce({ granted: false } as never);
    const request = { name: "iphone.location.current", arguments: {} } as const;
    const broker = new IPhoneCapabilityBroker({}, () => NOW);

    await expect(broker.execute(request, authorization(request))).resolves.toEqual({
      name: "iphone.location.current",
      status: "denied",
      reason: "permission_denied",
      value: null,
    });
    expect(Location.getCurrentPositionAsync).not.toHaveBeenCalled();
  });

  it("uses the SDK 57 Contacts legacy entry point instead of the throwing root wrapper", async () => {
    jest.mocked(Contacts.requestPermissionsAsync).mockResolvedValueOnce({ granted: true } as never);
    jest.mocked(Contacts.getContactsAsync).mockResolvedValueOnce({
      data: [{
        id: "contact-1",
        name: "Ada",
        phoneNumbers: [{ number: "+15145550123" }],
        emails: [{ email: "ada@example.com" }],
      }],
      hasNextPage: false,
      hasPreviousPage: false,
    } as never);
    const request = {
      name: "iphone.contacts.lookup",
      arguments: { query: "Ada" },
    } as const;
    const broker = new IPhoneCapabilityBroker({}, () => NOW);

    await expect(broker.execute(request, authorization(request))).resolves.toMatchObject({
      name: "iphone.contacts.lookup",
      status: "completed",
    });
    expect(Contacts.getContactsAsync).toHaveBeenCalledWith(expect.objectContaining({ name: "Ada" }));
  });

  it("uses the SDK 57 Calendar legacy entry point instead of the throwing root wrapper", async () => {
    jest.mocked(Calendar.requestCalendarPermissionsAsync).mockResolvedValueOnce({ granted: true } as never);
    jest.mocked(Calendar.getCalendarsAsync).mockResolvedValueOnce([{ id: "calendar-1" }] as never);
    jest.mocked(Calendar.getEventsAsync).mockResolvedValueOnce([{
      id: "event-1",
      title: "Review",
      startDate: "2026-09-08T13:00:00.000Z",
      endDate: "2026-09-08T14:00:00.000Z",
    }] as never);
    const request = {
      name: "iphone.calendar.events",
      arguments: {
        start: "2026-09-08T12:00:00.000Z",
        end: "2026-09-08T15:00:00.000Z",
      },
    } as const;
    const broker = new IPhoneCapabilityBroker({}, () => NOW);

    await expect(broker.execute(request, authorization(request))).resolves.toMatchObject({
      name: "iphone.calendar.events",
      status: "completed",
    });
    expect(Calendar.getCalendarsAsync).toHaveBeenCalledWith(Calendar.EntityTypes.EVENT);
  });

  it.each([
    ["saved", MailComposer.MailComposerStatus.SAVED],
    ["undetermined", MailComposer.MailComposerStatus.UNDETERMINED],
    ["cancelled", MailComposer.MailComposerStatus.CANCELLED],
  ])("treats a %s mail dialog as cancelled, never completed", async (_label, status) => {
    jest.mocked(MailComposer.isAvailableAsync).mockResolvedValueOnce(true);
    jest.mocked(MailComposer.composeAsync).mockResolvedValueOnce({ status });
    const request = { name: "iphone.mail.compose", arguments: { subject: "Hello" } } as const;
    const broker = new IPhoneCapabilityBroker({}, () => NOW);

    await expect(broker.execute(request, authorization(request))).resolves.toEqual({
      name: "iphone.mail.compose",
      status: "cancelled",
      value: null,
    });
  });

  it("treats an unknown SMS dialog result as cancelled, never sent", async () => {
    jest.mocked(SMS.isAvailableAsync).mockResolvedValueOnce(true);
    jest.mocked(SMS.sendSMSAsync).mockResolvedValueOnce({ result: "unknown" });
    const request = { name: "iphone.sms.compose", arguments: { message: "Hello" } } as const;
    const broker = new IPhoneCapabilityBroker({}, () => NOW);

    await expect(broker.execute(request, authorization(request))).resolves.toEqual({
      name: "iphone.sms.compose",
      status: "cancelled",
      value: null,
    });
  });
});
