import { describe, expect, it } from "@jest/globals";

import {
  CapabilityProtocolError,
  capabilityActionDigest,
  parseCapabilityResult,
  parseCapabilityRequestDetail,
  parseCapabilityNotification,
  parseCapabilityRequestEnvelope,
  parseCapabilityRequestList,
} from "./grant";

const NOW = Date.parse("2026-09-08T12:00:30.000Z");
const REQUEST_ID = `iphreq_${"a".repeat(32)}`;
const GRANT_ID = `grt_${"c".repeat(64)}`;
const ACTION_DIGEST = "sha256:928d3688233c2de096a0add2152d9747625307590618670406f332b0002a5461";

function requestEnvelope() {
  return {
    schema_version: "0.9",
    request_id: REQUEST_ID,
    task_id: `tsk_${"b".repeat(32)}`,
    agent_id: "mail-worker",
    target_device_id: "iphone_test",
    capability: "iphone.mail.compose",
    status: "approved",
    created_at: "2026-09-08T11:59:00.000Z",
    expires_at: "2026-09-08T12:03:00.000Z",
    arguments: {
      recipients: ["a@example.com"],
      subject: "Hello",
      body: "Body",
    },
    action_digest: ACTION_DIGEST,
    grant: {
      schema_version: "0.9",
      grant_id: GRANT_ID,
      request_id: REQUEST_ID,
      task_id: `tsk_${"b".repeat(32)}`,
      agent_id: "mail-worker",
      target_device_id: "iphone_test",
      approval_id: `apr_${"d".repeat(32)}`,
      audit_id: 42,
      capability: "iphone.mail.compose",
      action_digest: ACTION_DIGEST,
      issued_at: "2026-09-08T12:00:00.000Z",
      expires_at: "2026-09-08T12:01:00.000Z",
      use: "once",
    },
  };
}

describe("iPhone capability grants", () => {
  it("uses a stable SHA-256 digest of the exact canonical capability arguments", () => {
    expect(capabilityActionDigest(REQUEST_ID, "iphone.mail.compose", {
      subject: "Hello",
      body: "Body",
      recipients: ["a@example.com"],
    })).toBe(ACTION_DIGEST);
    expect(capabilityActionDigest(REQUEST_ID, "iphone.mail.compose", {
      recipients: ["a@example.com"],
      body: "Body",
      subject: "Hello",
    })).toBe(ACTION_DIGEST);
  });

  it("strictly parses a short-lived one-use request and its bound grant", () => {
    expect(parseCapabilityRequestEnvelope(requestEnvelope(), NOW)).toEqual(requestEnvelope());
    expect(parseCapabilityRequestList([{
      schema_version: "0.9",
      request_id: REQUEST_ID,
      task_id: `tsk_${"b".repeat(32)}`,
      agent_id: "mail-worker",
      target_device_id: "iphone_test",
      capability: "iphone.mail.compose",
      status: "waiting_approval",
      created_at: "2026-09-08T11:59:00.000Z",
      expires_at: "2026-09-08T12:03:00.000Z",
    }])).toHaveLength(1);
  });

  it("parses authoritative detail without recovering the opaque grant", () => {
    const { grant: _grant, ...detail } = requestEnvelope();
    const waiting = { ...detail, status: "waiting_approval", grant: null };
    expect(parseCapabilityRequestDetail(waiting)).toEqual(waiting);
  });

  it.each([
    ["request extras", (value: ReturnType<typeof requestEnvelope>) => Object.assign(value, { unexpected: true })],
    ["grant request mismatch", (value: ReturnType<typeof requestEnvelope>) => { value.grant.request_id = `iphreq_${"e".repeat(32)}`; }],
    ["grant capability mismatch", (value: ReturnType<typeof requestEnvelope>) => { value.grant.capability = "iphone.sms.compose"; }],
    ["argument tampering", (value: ReturnType<typeof requestEnvelope>) => { value.arguments.subject = "Changed"; }],
    ["non-one-use grant", (value: ReturnType<typeof requestEnvelope>) => { value.grant.use = "many"; }],
    ["expired grant", (value: ReturnType<typeof requestEnvelope>) => { value.grant.expires_at = "2026-09-08T12:00:29.000Z"; }],
    ["long-lived grant", (value: ReturnType<typeof requestEnvelope>) => { value.grant.expires_at = "2026-09-08T12:06:00.001Z"; }],
    ["expired request", (value: ReturnType<typeof requestEnvelope>) => { value.expires_at = "2026-09-08T12:00:29.000Z"; }],
  ])("rejects %s", (_label, mutate) => {
    const value = requestEnvelope();
    mutate(value);
    expect(() => parseCapabilityRequestEnvelope(value, NOW)).toThrow(CapabilityProtocolError);
  });

  it("accepts only an ID-only WebSocket notification so grants cannot enter persistence", () => {
    const notification = {
      request_id: REQUEST_ID,
      capability_name: "iphone.mail.compose",
      expires_at: "2026-09-08T12:03:00.000Z",
      preview: { arguments_redacted: true },
    };
    expect(parseCapabilityNotification(notification, NOW)).toEqual(notification);
    expect(() => parseCapabilityNotification({ ...notification, grant: requestEnvelope().grant }, NOW)).toThrow(
      CapabilityProtocolError,
    );
    expect(() => parseCapabilityNotification({
      ...notification,
      expires_at: "2026-09-08T12:00:30.000Z",
    }, NOW)).toThrow(CapabilityProtocolError);
  });

  it("matches the server digest for Unicode and values spanning multiple SHA-256 blocks", () => {
    expect(capabilityActionDigest(
      `iphreq_${"f".repeat(32)}`,
      "iphone.sms.compose",
      { recipients: [], message: `Allô 👋 — ${"x".repeat(100)}` },
    )).toBe("sha256:772618f364f73d14ce6002e501776b7db5bb4e2c06867e08abcce48106c179da");
    expect(capabilityActionDigest(
      `iphreq_${"f".repeat(32)}`,
      "iphone.sms.compose",
      { recipients: [], message: " exact whitespace " },
    )).not.toBe(capabilityActionDigest(
      `iphreq_${"f".repeat(32)}`,
      "iphone.sms.compose",
      { recipients: [], message: "exact whitespace" },
    ));
  });

  it.each(["", " padded "])(
    "rejects contact strings the strict server result contract rejects: %j",
    (phoneNumber) => {
      expect(() => parseCapabilityResult({
        name: "iphone.contacts.lookup",
        status: "completed",
        value: [{
          id: "contact-1",
          name: "Ada",
          phoneNumbers: [phoneNumber],
          emails: ["ada@example.com"],
        }],
      }, "iphone.contacts.lookup")).toThrow(CapabilityProtocolError);
    },
  );
});
