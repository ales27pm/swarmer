export type IPhoneCapabilityName =
  | "iphone.location.current"
  | "iphone.contacts.lookup"
  | "iphone.calendar.events"
  | "iphone.photos.pick"
  | "iphone.mail.compose"
  | "iphone.sms.compose";

export type CapabilityRequest =
  | { name: "iphone.location.current"; arguments: Record<string, never> }
  | { name: "iphone.contacts.lookup"; arguments: { query: string } }
  | { name: "iphone.calendar.events"; arguments: { start: string; end: string } }
  | { name: "iphone.photos.pick"; arguments: Record<string, never> }
  | { name: "iphone.mail.compose"; arguments: { recipients?: string[]; subject?: string; body?: string } }
  | { name: "iphone.sms.compose"; arguments: { recipients?: string[]; message?: string } };

export type CapabilityResult =
  | { name: "iphone.location.current"; status: "completed"; value: { latitude: number; longitude: number; accuracy: number | null } }
  | { name: "iphone.contacts.lookup"; status: "completed"; value: { id: string; name: string; phoneNumbers: string[]; emails: string[] }[] }
  | { name: "iphone.calendar.events"; status: "completed"; value: { id: string; title: string; start: string; end: string }[] }
  | { name: "iphone.photos.pick"; status: "completed" | "cancelled"; value: { uri: string; width: number; height: number } | null }
  | { name: "iphone.mail.compose"; status: "completed" | "cancelled"; value: { composed: true } | null }
  | { name: "iphone.sms.compose"; status: "completed" | "cancelled"; value: { composed: true } | null };

export type GatewayAuthorization = {
  authorized: boolean;
  authorizationId?: string;
  reason?: string;
};

export type CapabilityGateway = (request: CapabilityRequest) => Promise<GatewayAuthorization>;

export class CapabilityDeniedError extends Error {}
