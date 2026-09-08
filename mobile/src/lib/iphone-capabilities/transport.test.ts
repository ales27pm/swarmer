import { describe, expect, it, jest } from "@jest/globals";

import type {
  CapabilityConsumeReceipt,
  CapabilityRequestEnvelope,
  CapabilityResult,
  CapabilityResultReceipt,
} from "./types";
import { IPhoneCapabilityTransport } from "./transport";

const NOW = Date.parse("2026-09-08T12:00:30.000Z");
const REQUEST_ID = `iphreq_${"a".repeat(32)}`;
const GRANT_ID = `grt_${"c".repeat(64)}`;
const ACTION_DIGEST = "sha256:928d3688233c2de096a0add2152d9747625307590618670406f332b0002a5461";

function requestEnvelope(): CapabilityRequestEnvelope {
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
    arguments: { recipients: ["a@example.com"], subject: "Hello", body: "Body" },
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

function consumeReceipt(): CapabilityConsumeReceipt {
  return {
    status: "consumed",
    request_id: REQUEST_ID,
    grant_id: GRANT_ID,
    action_digest: ACTION_DIGEST,
    consumed_at: "2026-09-08T12:00:31.000Z",
  };
}

function result(): CapabilityResult {
  return {
    name: "iphone.mail.compose",
    status: "completed",
    value: { composed: true },
  };
}

function setup() {
  const order: string[] = [];
  const getRequest = jest.fn<() => Promise<unknown>>(async () => {
    order.push("get");
    return { ...requestEnvelope(), status: "waiting_approval" as const, grant: null };
  });
  const authorizeRequest = jest.fn<() => Promise<unknown>>(async () => {
    order.push("authorize");
    return requestEnvelope();
  });
  const consumeRequest = jest.fn<() => Promise<unknown>>(async () => {
    order.push("consume");
    return consumeReceipt();
  });
  const executeNative = jest.fn(async () => {
    order.push("native");
    return result();
  });
  const submitResult = jest.fn<() => Promise<CapabilityResultReceipt>>(async () => {
    order.push("result");
    return { status: "accepted" as const, request_id: REQUEST_ID, grant_id: GRANT_ID };
  });
  const createSession = jest.fn(async () => ({
    origin: "https://control.example",
    authorizeRequest,
    consumeRequest,
    getRequest,
    listRequests: async () => [],
    submitResult,
  }));
  const transport = new IPhoneCapabilityTransport(
    { execute: executeNative },
    { createSession, now: () => NOW },
  );
  return {
    authorizeRequest,
    consumeRequest,
    createSession,
    executeNative,
    getRequest,
    order,
    submitResult,
    transport,
  };
}

describe("iPhone capability transport", () => {
  it("queues duplicate WebSocket notifications in memory without fetching or executing", () => {
    const { authorizeRequest, consumeRequest, executeNative, getRequest, transport } = setup();
    const notification = {
      request_id: REQUEST_ID,
      capability_name: "iphone.mail.compose",
      expires_at: "2026-09-08T12:03:00.000Z",
      preview: { arguments_redacted: true },
    };
    expect(transport.receiveNotification(notification)).toBe(true);
    expect(transport.receiveNotification(notification)).toBe(false);
    expect(() => transport.receiveNotification({
      ...notification,
      capability_name: "iphone.sms.compose",
    })).toThrow("changed its safe preview");
    expect(transport.pendingRequestIds()).toEqual([REQUEST_ID]);
    expect(getRequest).not.toHaveBeenCalled();
    expect(authorizeRequest).not.toHaveBeenCalled();
    expect(consumeRequest).not.toHaveBeenCalled();
    expect(executeNative).not.toHaveBeenCalled();
  });

  it("consumes the exact server grant before touching native APIs", async () => {
    const { executeNative, order, submitResult, transport } = setup();
    await transport.authorize(REQUEST_ID, "approve");
    await expect(transport.execute(REQUEST_ID)).resolves.toEqual(result());
    expect(order).toEqual(["get", "authorize", "consume", "native", "result"]);
    expect(executeNative).toHaveBeenCalledWith(
      { name: "iphone.mail.compose", arguments: requestEnvelope().arguments },
      expect.objectContaining({ request_id: REQUEST_ID, grant_id: GRANT_ID }),
    );
    expect(submitResult).toHaveBeenCalledWith(requestEnvelope(), result());
  });

  it("fails offline before native execution when authoritative consume is unavailable", async () => {
    const state = setup();
    await state.transport.authorize(REQUEST_ID, "approve");
    state.consumeRequest.mockRejectedValueOnce(new Error("offline"));
    await expect(state.transport.execute(REQUEST_ID)).rejects.toThrow("offline");
    expect(state.executeNative).not.toHaveBeenCalled();
    expect(state.submitResult).not.toHaveBeenCalled();
  });

  it("rejects an expired request before asking the server to authorize it", async () => {
    const state = setup();
    state.getRequest.mockResolvedValueOnce({
      ...requestEnvelope(),
      status: "waiting_approval",
      expires_at: "2026-09-08T12:00:29.000Z",
      grant: null,
    });

    await expect(state.transport.authorize(REQUEST_ID, "approve")).rejects.toThrow("expired");
    expect(state.authorizeRequest).not.toHaveBeenCalled();
    expect(state.consumeRequest).not.toHaveBeenCalled();
    expect(state.executeNative).not.toHaveBeenCalled();
  });

  it("rejects a WebSocket preview that disagrees with authoritative detail", async () => {
    const state = setup();
    state.transport.receiveNotification({
      request_id: REQUEST_ID,
      capability_name: "iphone.sms.compose",
      expires_at: "2026-09-08T12:03:00.000Z",
      preview: { arguments_redacted: true },
    });

    await expect(state.transport.authorize(REQUEST_ID, "approve")).rejects.toThrow(
      "does not match the authoritative request",
    );
    expect(state.authorizeRequest).not.toHaveBeenCalled();
    expect(state.executeNative).not.toHaveBeenCalled();
  });

  it("rejects a mismatched consume receipt before native execution", async () => {
    const state = setup();
    await state.transport.authorize(REQUEST_ID, "approve");
    state.consumeRequest.mockResolvedValueOnce({
      ...consumeReceipt(),
      grant_id: `grt_${"e".repeat(64)}`,
    });

    await expect(state.transport.execute(REQUEST_ID)).rejects.toThrow("does not match");
    expect(state.executeNative).not.toHaveBeenCalled();
    expect(state.submitResult).not.toHaveBeenCalled();
  });

  it("coalesces concurrent duplicate delivery and rejects replay without retaining its result", async () => {
    const state = setup();
    await state.transport.authorize(REQUEST_ID, "approve");
    const [first, duplicate] = await Promise.all([
      state.transport.execute(REQUEST_ID),
      state.transport.execute(REQUEST_ID),
    ]);
    await expect(state.transport.execute(REQUEST_ID)).rejects.toThrow("already terminal");
    expect(first).toEqual(result());
    expect(duplicate).toEqual(first);
    expect(state.getRequest).toHaveBeenCalledTimes(1);
    expect(state.consumeRequest).toHaveBeenCalledTimes(1);
    expect(state.executeNative).toHaveBeenCalledTimes(1);
    expect(state.submitResult).toHaveBeenCalledTimes(1);
    expect(state.createSession).toHaveBeenCalledTimes(1);
  });

  it("retries only result delivery after native execution, never the grant or native action", async () => {
    const state = setup();
    await state.transport.authorize(REQUEST_ID, "approve");
    state.submitResult
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValueOnce({ status: "duplicate", request_id: REQUEST_ID, grant_id: GRANT_ID });

    await expect(state.transport.execute(REQUEST_ID)).rejects.toThrow("response lost");
    await expect(state.transport.execute(REQUEST_ID)).resolves.toEqual(result());

    expect(state.getRequest).toHaveBeenCalledTimes(1);
    expect(state.consumeRequest).toHaveBeenCalledTimes(1);
    expect(state.executeNative).toHaveBeenCalledTimes(1);
    expect(state.submitResult).toHaveBeenCalledTimes(2);
  });

  it("never consumes or executes a denied request", async () => {
    const state = setup();
    state.authorizeRequest.mockResolvedValueOnce({
      ...requestEnvelope(),
      status: "denied",
      grant: null,
    });

    await expect(state.transport.authorize(REQUEST_ID, "deny")).resolves.toEqual(
      expect.objectContaining({ status: "denied", grant: null }),
    );
    await expect(state.transport.execute(REQUEST_ID)).rejects.toThrow("already terminal");
    expect(state.consumeRequest).not.toHaveBeenCalled();
    expect(state.executeNative).not.toHaveBeenCalled();
  });

  it("requires an explicit repeat approval to recover an authoritative approved request", async () => {
    const state = setup();
    state.getRequest.mockResolvedValueOnce({
      ...requestEnvelope(),
      status: "approved",
      grant: null,
    });

    await expect(state.transport.authorize(REQUEST_ID, "approve")).resolves.toEqual(
      requestEnvelope(),
    );
    expect(state.authorizeRequest).toHaveBeenCalledWith(
      expect.objectContaining({ status: "approved", grant: null }),
      "approve",
    );
  });

  it("never permits denial after the authoritative request was approved", async () => {
    const state = setup();
    state.getRequest.mockResolvedValueOnce({
      ...requestEnvelope(),
      status: "approved",
      grant: null,
    });

    await expect(state.transport.authorize(REQUEST_ID, "deny")).rejects.toThrow(
      "grant-recovery",
    );
    expect(state.authorizeRequest).not.toHaveBeenCalled();
  });
});
