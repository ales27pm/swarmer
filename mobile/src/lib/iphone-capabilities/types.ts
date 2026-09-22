export const IPHONE_CAPABILITY_SCHEMA_VERSION = "0.9" as const;

export type IPhoneCapabilityName =
  | "iphone.location.current"
  | "iphone.contacts.lookup"
  | "iphone.calendar.events"
  | "iphone.calendar.reminders"
  | "iphone.calendar.calendars"
  | "iphone.calendar.event.create"
  | "iphone.calendar.event.update"
  | "iphone.calendar.reminder.create"
  | "iphone.calendar.reminder.update"
  | "iphone.photos.pick"
  | "iphone.mail.compose"
  | "iphone.sms.compose";

export type CapabilityArgumentsByName = {
  "iphone.location.current": Record<string, never>;
  "iphone.contacts.lookup": { query: string };
  "iphone.calendar.events": { start: string; end: string };
  "iphone.calendar.reminders": { calendar_id: string };
  "iphone.calendar.calendars": Record<string, never>;
  "iphone.calendar.event.create": { calendar_id: string; title: string; start: string; end: string };
  "iphone.calendar.event.update": { id: string; title: string; start: string; end: string };
  "iphone.calendar.reminder.create": { calendar_id: string; title: string; due: string | null };
  "iphone.calendar.reminder.update": { id: string; title: string; due: string; completed: boolean };
  "iphone.photos.pick": Record<string, never>;
  "iphone.mail.compose": { recipients?: string[]; subject?: string; body?: string };
  "iphone.sms.compose": { recipients?: string[]; message?: string };
};

export type CapabilityRequest = {
  [Name in IPhoneCapabilityName]: {
    name: Name;
    arguments: CapabilityArgumentsByName[Name];
  };
}[IPhoneCapabilityName];

export type CalendarEventReceipt = { id: string; title: string; start: string; end: string };
export type CalendarReminderReceipt = { id: string; title: string; due: string | null; completed: boolean };

type CapabilityCompletedResult =
  | { name: "iphone.calendar.reminders"; status: "completed"; value: CalendarReminderReceipt[] }
  | { name: "iphone.calendar.calendars"; status: "completed"; value: { id: string; title: string; entityType: "event" | "reminder"; allowsModifications: boolean }[] }
  | { name: "iphone.calendar.event.create" | "iphone.calendar.event.update"; status: "completed"; value: CalendarEventReceipt }
  | { name: "iphone.calendar.reminder.create" | "iphone.calendar.reminder.update"; status: "completed"; value: CalendarReminderReceipt }
  | { name: "iphone.location.current"; status: "completed"; value: { latitude: number; longitude: number; accuracy: number | null } }
  | { name: "iphone.contacts.lookup"; status: "completed"; value: { id: string; name: string; phoneNumbers: string[]; emails: string[] }[] }
  | { name: "iphone.calendar.events"; status: "completed"; value: { id: string; title: string; start: string; end: string }[] }
  | { name: "iphone.photos.pick"; status: "completed"; value: { uri: string; width: number; height: number } }
  | { name: "iphone.mail.compose" | "iphone.sms.compose"; status: "completed"; value: { composed: true } };

type CapabilityCancelledResult = {
  name: "iphone.photos.pick" | "iphone.mail.compose" | "iphone.sms.compose";
  status: "cancelled";
  value: null;
};

type CapabilityDeniedResult = {
  name: IPhoneCapabilityName;
  status: "denied";
  reason: "permission_denied" | "unavailable";
  value: null;
};

type CapabilityFailedResult = {
  name: IPhoneCapabilityName;
  status: "failed";
  reason: "native_error";
  value: null;
};

export type CapabilityResult =
  | CapabilityCompletedResult
  | CapabilityCancelledResult
  | CapabilityDeniedResult
  | CapabilityFailedResult;

export type CapabilityRequestStatus =
  | "waiting_approval"
  | "approved"
  | "denied"
  | "consumed"
  | "completed"
  | "failed"
  | "cancelled"
  | "expired";

export type CapabilityActionDigest = `sha256:${string}`;

export type CapabilityGrant<Name extends IPhoneCapabilityName = IPhoneCapabilityName> = {
  schema_version: typeof IPHONE_CAPABILITY_SCHEMA_VERSION;
  grant_id: string;
  request_id: string;
  task_id: string;
  agent_id: string;
  target_device_id: string;
  approval_id: string;
  audit_id: number;
  capability: Name;
  action_digest: CapabilityActionDigest;
  issued_at: string;
  expires_at: string;
  use: "once";
};

type CapabilityRequestIdentity<Name extends IPhoneCapabilityName> = {
  schema_version: typeof IPHONE_CAPABILITY_SCHEMA_VERSION;
  request_id: string;
  task_id: string;
  agent_id: string;
  target_device_id: string;
  capability: Name;
  status: CapabilityRequestStatus;
  created_at: string;
  expires_at: string;
};

export type CapabilityRequestPreview = {
  [Name in IPhoneCapabilityName]: CapabilityRequestIdentity<Name>;
}[IPhoneCapabilityName];

export type CapabilityRequestDetail = {
  [Name in IPhoneCapabilityName]: CapabilityRequestIdentity<Name> & {
    arguments: CapabilityArgumentsByName[Name];
    action_digest: CapabilityActionDigest;
    grant: CapabilityGrant<Name> | null;
  };
}[IPhoneCapabilityName];

export type CapabilityRequestEnvelope = {
  [Name in IPhoneCapabilityName]: CapabilityRequestIdentity<Name> & {
    status: "approved";
    arguments: CapabilityArgumentsByName[Name];
    action_digest: CapabilityActionDigest;
    grant: CapabilityGrant<Name>;
  };
}[IPhoneCapabilityName];

export type CapabilityAuthorizationDecision = "approve" | "deny";
export type CapabilityAuthorizationResponse =
  | CapabilityRequestEnvelope
  | (CapabilityRequestDetail & { status: "denied"; grant: null });

export type CapabilityConsumeReceipt = {
  status: "consumed";
  request_id: string;
  grant_id: string;
  action_digest: CapabilityActionDigest;
  consumed_at: string;
};

declare const consumedCapabilityGrantBrand: unique symbol;
export type ConsumedCapabilityGrant = CapabilityConsumeReceipt & {
  capability: IPhoneCapabilityName;
  grant_expires_at: string;
  readonly [consumedCapabilityGrantBrand]: true;
};

export type CapabilityResultReceipt = {
  status: "accepted" | "duplicate";
  request_id: string;
  grant_id: string;
};

export type CapabilityNotification = {
  request_id: string;
  capability_name: IPhoneCapabilityName;
  expires_at: string;
  preview: { arguments_redacted: true };
};

export type CapabilityNativeExecutor = {
  execute: (
    request: CapabilityRequest,
    authorization: ConsumedCapabilityGrant,
  ) => Promise<CapabilityResult>;
};

export class CapabilityDeniedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CapabilityDeniedError";
  }
}

export class CapabilityReplayError extends CapabilityDeniedError {
  constructor(message = "This one-use iPhone capability grant was already used.") {
    super(message);
    this.name = "CapabilityReplayError";
  }
}
