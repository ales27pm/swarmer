import {
  assertCapabilityRequestFresh,
  assertCapabilityRequestId,
  capabilityGrantFingerprint,
  CapabilityProtocolError,
  parseCapabilityAuthorizationResponse,
  parseCapabilityConsumeReceipt,
  parseCapabilityNotification,
  parseCapabilityRequestEnvelope,
  parseCapabilityRequestList,
  parseCapabilityRequestLookup,
  parseCapabilityResult,
  parseCapabilityResultReceipt,
} from "./grant";
import type {
  CapabilityAuthorizationDecision,
  CapabilityAuthorizationResponse,
  CapabilityNativeExecutor,
  CapabilityNotification,
  CapabilityRequest,
  CapabilityRequestDetail,
  CapabilityRequestEnvelope,
  CapabilityRequestPreview,
  CapabilityResult,
} from "./types";

export type CapabilityTransportSession = {
  origin: string;
  authorizeRequest: (
    request: CapabilityRequestDetail,
    decision: CapabilityAuthorizationDecision,
  ) => Promise<unknown>;
  consumeRequest: (request: CapabilityRequestEnvelope) => Promise<unknown>;
  getRequest: (requestId: string) => Promise<unknown>;
  listRequests: () => Promise<unknown>;
  submitResult: (
    request: CapabilityRequestEnvelope,
    result: CapabilityResult,
  ) => Promise<unknown>;
};

export type CapabilityTransportDependencies = {
  createSession: () => Promise<CapabilityTransportSession>;
  now?: () => number;
};

type AuthorizationInFlight = {
  decision: CapabilityAuthorizationDecision;
  promise: Promise<CapabilityAuthorizationResponse>;
};

type ExecutionOutcome = {
  envelope: CapabilityRequestEnvelope;
  reported: boolean;
  result: CapabilityResult;
};

const MAX_TERMINAL_MARKERS = 256;
const TERMINAL_MARKER_TTL_MS = 10 * 60 * 1_000;

function requestFingerprint(request: CapabilityRequestDetail | CapabilityRequestPreview): string {
  return [
    request.schema_version,
    request.request_id,
    request.task_id,
    request.agent_id,
    request.target_device_id,
    request.capability,
    request.created_at,
    request.expires_at,
  ].join("|");
}

function nativeRequest(envelope: CapabilityRequestEnvelope): CapabilityRequest {
  return {
    name: envelope.capability,
    arguments: envelope.arguments,
  } as CapabilityRequest;
}

export class IPhoneCapabilityTransport {
  private readonly createSession: CapabilityTransportDependencies["createSession"];
  private readonly now: () => number;
  private readonly notifications = new Map<string, CapabilityNotification>();
  private readonly previews = new Map<string, CapabilityRequestPreview>();
  private readonly details = new Map<string, CapabilityRequestDetail>();
  private readonly fingerprints = new Map<string, string>();
  private readonly detailDigests = new Map<string, string>();
  private readonly authorizations = new Map<string, CapabilityAuthorizationResponse>();
  private readonly authorizationInFlight = new Map<string, AuthorizationInFlight>();
  private readonly executionInFlight = new Map<string, Promise<CapabilityResult>>();
  private readonly outcomes = new Map<string, ExecutionOutcome>();
  private readonly terminalRequests = new Map<string, number>();
  private readonly grantOwners = new Map<string, string>();
  private readonly usedGrantIds = new Map<string, string>();
  private session: CapabilityTransportSession | null = null;
  private sessionInFlight: Promise<CapabilityTransportSession> | null = null;
  private epoch = 0;

  constructor(
    private readonly native: CapabilityNativeExecutor,
    dependencies: CapabilityTransportDependencies,
  ) {
    this.createSession = dependencies.createSession;
    this.now = dependencies.now ?? Date.now;
  }

  receiveNotification(value: unknown): boolean {
    this.pruneExpiredState();
    const notification = parseCapabilityNotification(value, this.now());
    if (this.terminalRequests.has(notification.request_id)) return false;
    const existing = this.notifications.get(notification.request_id);
    if (existing && JSON.stringify(existing) !== JSON.stringify(notification)) {
      throw new CapabilityProtocolError("Duplicate capability notification changed its safe preview.");
    }
    const isNew = existing === undefined;
    this.notifications.set(notification.request_id, notification);
    return isNew;
  }

  pendingRequestIds(): string[] {
    this.pruneExpiredState();
    const now = this.now();
    for (const [requestId, notification] of this.notifications) {
      if (Date.parse(notification.expires_at) <= now) this.notifications.delete(requestId);
    }
    return [...this.notifications.keys()];
  }

  async refresh(): Promise<CapabilityRequestPreview[]> {
    this.pruneExpiredState();
    const epoch = this.epoch;
    const session = await this.boundSession(epoch);
    const previews = parseCapabilityRequestList(await session.listRequests());
    this.assertEpoch(epoch);
    for (const preview of previews) {
      this.assertNotificationMatches(preview);
      this.remember(preview);
    }
    return previews;
  }

  async load(requestId: string): Promise<CapabilityRequestDetail> {
    this.pruneExpiredState();
    const epoch = this.epoch;
    assertCapabilityRequestId(requestId);
    const session = await this.boundSession(epoch);
    const detail = parseCapabilityRequestLookup(await session.getRequest(requestId), this.now());
    this.assertEpoch(epoch);
    if (detail.request_id !== requestId) {
      throw new CapabilityProtocolError("Capability detail does not match the requested ID.");
    }
    this.assertNotificationMatches(detail);
    this.remember(detail);
    this.details.set(requestId, detail);
    return detail;
  }

  authorize(
    requestId: string,
    decision: CapabilityAuthorizationDecision,
  ): Promise<CapabilityAuthorizationResponse> {
    this.pruneExpiredState();
    if (decision !== "approve" && decision !== "deny") {
      return Promise.reject(new CapabilityProtocolError("Capability authorization decision is invalid."));
    }
    const existing = this.authorizations.get(requestId);
    if (existing) {
      const matches = decision === "approve"
        ? existing.status === "approved" && existing.grant !== null
        : existing.status === "denied";
      if (!matches) {
        return Promise.reject(new CapabilityProtocolError("Capability request already has a different decision."));
      }
      return Promise.resolve(existing);
    }
    const inFlight = this.authorizationInFlight.get(requestId);
    if (inFlight) {
      if (inFlight.decision !== decision) {
        return Promise.reject(new CapabilityProtocolError("A different capability decision is already in flight."));
      }
      return inFlight.promise;
    }
    const promise = this.authorizeOnce(requestId, decision, this.epoch).finally(() => {
      if (this.authorizationInFlight.get(requestId)?.promise === promise) {
        this.authorizationInFlight.delete(requestId);
      }
    });
    this.authorizationInFlight.set(requestId, { decision, promise });
    return promise;
  }

  execute(requestId: string): Promise<CapabilityResult> {
    this.pruneExpiredState();
    if (this.terminalRequests.has(requestId)) {
      this.notifications.delete(requestId);
      return Promise.reject(new CapabilityProtocolError("Capability request is already terminal in this app session."));
    }
    const inFlight = this.executionInFlight.get(requestId);
    if (inFlight) return inFlight;
    const outcome = this.outcomes.get(requestId);
    const promise = (outcome
      ? this.deliverOutcome(outcome, this.epoch)
      : this.executeOnce(requestId, this.epoch)).finally(() => {
      if (this.executionInFlight.get(requestId) === promise) {
        this.executionInFlight.delete(requestId);
      }
    });
    this.executionInFlight.set(requestId, promise);
    return promise;
  }

  clear(): void {
    this.epoch += 1;
    this.notifications.clear();
    this.previews.clear();
    this.details.clear();
    this.fingerprints.clear();
    this.detailDigests.clear();
    this.authorizations.clear();
    this.authorizationInFlight.clear();
    this.executionInFlight.clear();
    this.outcomes.clear();
    this.terminalRequests.clear();
    this.grantOwners.clear();
    this.usedGrantIds.clear();
    this.session = null;
    this.sessionInFlight = null;
  }

  private remember(request: CapabilityRequestDetail | CapabilityRequestPreview): void {
    const fingerprint = requestFingerprint(request);
    const existing = this.fingerprints.get(request.request_id);
    if (existing && existing !== fingerprint) {
      throw new CapabilityProtocolError("Duplicate capability delivery changed a bound request.");
    }
    this.fingerprints.set(request.request_id, fingerprint);
    if ("arguments" in request) {
      const existingDigest = this.detailDigests.get(request.request_id);
      if (existingDigest && existingDigest !== request.action_digest) {
        throw new CapabilityProtocolError("Duplicate capability delivery changed its arguments.");
      }
      this.detailDigests.set(request.request_id, request.action_digest);
    }
    if (!("arguments" in request)) this.previews.set(request.request_id, request);
  }

  private async authorizeOnce(
    requestId: string,
    decision: CapabilityAuthorizationDecision,
    epoch: number,
  ): Promise<CapabilityAuthorizationResponse> {
    const detail = this.details.get(requestId) ?? await this.load(requestId);
    this.assertEpoch(epoch);
    assertCapabilityRequestFresh(detail, this.now());
    this.assertAuthorizationAllowed(detail, decision);
    const response = await this.requestAuthorization(detail, decision, epoch);
    this.rememberAuthorization(requestId, response);
    return response;
  }

  private assertAuthorizationAllowed(
    detail: CapabilityRequestDetail,
    decision: CapabilityAuthorizationDecision,
  ): void {
    const canDecide = detail.status === "waiting_approval";
    const canRecover = decision === "approve" && detail.status === "approved" && detail.grant === null;
    if (!canDecide && !canRecover) {
      throw new CapabilityProtocolError(
        "Only a waiting request or an approved grant-recovery request may be authorized.",
      );
    }
  }

  private async requestAuthorization(
    detail: CapabilityRequestDetail,
    decision: CapabilityAuthorizationDecision,
    epoch: number,
  ): Promise<CapabilityAuthorizationResponse> {
    const session = await this.boundSession(epoch);
    this.assertEpoch(epoch);
    const response = parseCapabilityAuthorizationResponse(
      await session.authorizeRequest(detail, decision),
      decision,
      detail,
      this.now(),
    );
    this.assertEpoch(epoch);
    return response;
  }

  private rememberAuthorization(
    requestId: string,
    response: CapabilityAuthorizationResponse,
  ): void {
    this.remember(response);
    if (response.status === "approved" && response.grant !== null) {
      this.rememberGrantOwner(response);
      this.authorizations.set(requestId, response);
    } else {
      this.forgetRequest(requestId);
      this.rememberTerminal(requestId);
    }
  }

  private rememberGrantOwner(response: CapabilityRequestEnvelope): void {
    const grantFingerprint = capabilityGrantFingerprint(response.grant.grant_id);
    const previousRequest = this.grantOwners.get(grantFingerprint);
    if (previousRequest && previousRequest !== response.request_id) {
      throw new CapabilityProtocolError("A capability grant was duplicated across requests.");
    }
    this.grantOwners.set(grantFingerprint, response.request_id);
  }

  private async executeOnce(requestId: string, epoch: number): Promise<CapabilityResult> {
    const authorized = this.authorizations.get(requestId);
    if (!authorized || authorized.status !== "approved" || authorized.grant === null) {
      throw new CapabilityProtocolError("Capability request must be approved in this app session before execution.");
    }
    const envelope = parseCapabilityRequestEnvelope(authorized, this.now());
    const grantFingerprint = capabilityGrantFingerprint(envelope.grant.grant_id);
    const previousRequest = this.usedGrantIds.get(grantFingerprint);
    if (previousRequest) {
      throw new CapabilityProtocolError("Capability grant replay was blocked.");
    }

    const session = await this.boundSession(epoch);
    this.assertEpoch(epoch);
    const rawReceipt = await session.consumeRequest(envelope);
    this.assertEpoch(epoch);
    if (rawReceipt === null || typeof rawReceipt !== "object") {
      throw new CapabilityProtocolError("Capability consume receipt is invalid.");
    }
    const receipt = rawReceipt as Record<string, unknown>;
    const authorization = parseCapabilityConsumeReceipt({
      status: receipt.status,
      request_id: receipt.request_id,
      grant_id: receipt.grant_id,
      action_digest: receipt.action_digest,
      consumed_at: receipt.consumed_at,
    }, envelope, this.now());
    this.usedGrantIds.set(grantFingerprint, requestId);

    let result: CapabilityResult;
    try {
      result = parseCapabilityResult(
        await this.native.execute(nativeRequest(envelope), authorization),
        envelope.capability,
      );
    } catch {
      result = {
        name: envelope.capability,
        status: "failed",
        reason: "native_error",
        value: null,
      };
    }
    this.assertEpoch(epoch);
    const outcome = { envelope, reported: false, result } satisfies ExecutionOutcome;
    this.outcomes.set(requestId, outcome);
    this.notifications.delete(requestId);
    return this.deliverOutcome(outcome, epoch);
  }

  private async deliverOutcome(outcome: ExecutionOutcome, epoch: number): Promise<CapabilityResult> {
    if (outcome.reported) return outcome.result;
    const session = await this.boundSession(epoch);
    this.assertEpoch(epoch);
    const receipt = await session.submitResult(outcome.envelope, outcome.result);
    this.assertEpoch(epoch);
    parseCapabilityResultReceipt(receipt, outcome.envelope);
    outcome.reported = true;
    const requestId = outcome.envelope.request_id;
    this.discardOutcome(outcome);
    this.rememberTerminal(requestId);
    return outcome.result;
  }

  private async boundSession(expectedEpoch: number): Promise<CapabilityTransportSession> {
    if (this.session) return this.session;
    const existing = this.sessionInFlight;
    if (existing) {
      const session = await existing;
      this.assertEpoch(expectedEpoch);
      return session;
    }
    const pending = this.createSession();
    this.sessionInFlight = pending;
    try {
      const session = await pending;
      this.assertEpoch(expectedEpoch);
      if (!session.origin) {
        throw new CapabilityProtocolError("Capability transport session has no bound origin.");
      }
      if (this.sessionInFlight === pending) this.session = session;
      return session;
    } finally {
      if (this.sessionInFlight === pending) this.sessionInFlight = null;
    }
  }

  private forgetRequest(requestId: string): void {
    this.notifications.delete(requestId);
    this.previews.delete(requestId);
    this.details.delete(requestId);
    this.fingerprints.delete(requestId);
    this.detailDigests.delete(requestId);
    this.authorizations.delete(requestId);
  }

  private rememberTerminal(requestId: string): void {
    this.terminalRequests.delete(requestId);
    this.terminalRequests.set(requestId, this.now() + TERMINAL_MARKER_TTL_MS);
    while (this.terminalRequests.size > MAX_TERMINAL_MARKERS) {
      const oldest = this.terminalRequests.keys().next().value as string | undefined;
      if (!oldest) break;
      this.terminalRequests.delete(oldest);
    }
  }

  private pruneExpiredState(): void {
    const now = this.now();
    this.pruneExpiredNotifications(now);
    this.pruneExpiredDetails(now);
    this.pruneExpiredTerminalRequests(now);
    this.pruneExpiredAuthorizations(now);
    this.pruneExpiredOutcomes(now);
  }

  private pruneExpiredNotifications(now: number): void {
    for (const [requestId, notification] of this.notifications) {
      if (Date.parse(notification.expires_at) <= now) this.notifications.delete(requestId);
    }
  }

  private pruneExpiredDetails(now: number): void {
    for (const [requestId, detail] of this.details) {
      if (Date.parse(detail.expires_at) <= now) this.forgetRequest(requestId);
    }
  }

  private pruneExpiredTerminalRequests(now: number): void {
    for (const [requestId, expiresAt] of this.terminalRequests) {
      if (expiresAt <= now) this.terminalRequests.delete(requestId);
    }
  }

  private pruneExpiredAuthorizations(now: number): void {
    for (const [requestId, authorization] of this.authorizations) {
      if (authorization.grant && Date.parse(authorization.grant.expires_at) <= now) {
        this.releaseGrant(authorization.grant.grant_id);
        this.forgetRequest(requestId);
      }
    }
  }

  private pruneExpiredOutcomes(now: number): void {
    for (const outcome of this.outcomes.values()) {
      if (Date.parse(outcome.envelope.expires_at) <= now) {
        this.discardOutcome(outcome);
      }
    }
  }

  private discardOutcome(outcome: ExecutionOutcome): void {
    this.outcomes.delete(outcome.envelope.request_id);
    this.releaseGrant(outcome.envelope.grant.grant_id);
    this.forgetRequest(outcome.envelope.request_id);
  }

  private releaseGrant(grantId: string): void {
    const grantFingerprint = capabilityGrantFingerprint(grantId);
    this.grantOwners.delete(grantFingerprint);
    this.usedGrantIds.delete(grantFingerprint);
  }

  private assertEpoch(expected: number): void {
    if (expected !== this.epoch) {
      throw new CapabilityProtocolError("The paired control plane changed during capability execution.");
    }
  }

  private assertNotificationMatches(
    request: CapabilityRequestDetail | CapabilityRequestPreview,
  ): void {
    const notification = this.notifications.get(request.request_id);
    if (
      notification &&
      (
        notification.capability_name !== request.capability ||
        notification.expires_at !== request.expires_at
      )
    ) {
      throw new CapabilityProtocolError("Capability notification does not match the authoritative request.");
    }
  }
}
