import * as Calendar from "expo-calendar/legacy";
import * as Contacts from "expo-contacts/legacy";
import * as ImagePicker from "expo-image-picker";
import * as Location from "expo-location";
import * as MailComposer from "expo-mail-composer";
import * as SMS from "expo-sms";

import {
  capabilityActionDigest,
  capabilityGrantFingerprint,
  parseCapabilityResult,
} from "./grant";
import type {
  CapabilityRequest,
  CapabilityResult,
  ConsumedCapabilityGrant,
} from "./types";
import { CapabilityDeniedError, CapabilityReplayError } from "./types";

type CapabilityOverride = (
  arguments_: never,
  authorization: ConsumedCapabilityGrant,
) => Promise<CapabilityResult>;

export class IPhoneCapabilityBroker {
  private readonly usedGrantIds = new Set<string>();

  constructor(
    private readonly overrides: Partial<Record<CapabilityRequest["name"], CapabilityOverride>> = {},
    private readonly now: () => number = Date.now,
  ) {}

  async execute(
    request: CapabilityRequest,
    authorization: ConsumedCapabilityGrant,
  ): Promise<CapabilityResult> {
    const grantExpiresAt = Date.parse(authorization.grant_expires_at);
    if (
      authorization.status !== "consumed" ||
      authorization.capability !== request.name ||
      authorization.action_digest !== capabilityActionDigest(
        authorization.request_id,
        request.name,
        request.arguments,
      ) ||
      !Number.isFinite(grantExpiresAt) ||
      grantExpiresAt <= this.now()
    ) {
      throw new CapabilityDeniedError("A matching consumed server grant is required.");
    }
    const grantFingerprint = capabilityGrantFingerprint(authorization.grant_id);
    if (this.usedGrantIds.has(grantFingerprint)) {
      throw new CapabilityReplayError();
    }
    this.usedGrantIds.add(grantFingerprint);

    const override = this.overrides[request.name];
    if (override) {
      return parseCapabilityResult(
        await override(request.arguments as never, authorization),
        request.name,
      );
    }
    let result: CapabilityResult;
    switch (request.name) {
      case "iphone.location.current": result = await this.location(); break;
      case "iphone.contacts.lookup": result = await this.contacts(request.arguments.query); break;
      case "iphone.calendar.events": result = await this.events(request.arguments.start, request.arguments.end); break;
      case "iphone.photos.pick": result = await this.photo(); break;
      case "iphone.mail.compose": result = await this.mail(request.arguments); break;
      case "iphone.sms.compose": result = await this.sms(request.arguments); break;
    }
    return parseCapabilityResult(result, request.name);
  }

  private async location(): Promise<CapabilityResult> {
    const permission = await Location.requestForegroundPermissionsAsync();
    if (!permission.granted) {
      return { name: "iphone.location.current", status: "denied", reason: "permission_denied", value: null };
    }
    const current = await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.Balanced });
    return { name: "iphone.location.current", status: "completed", value: { latitude: current.coords.latitude, longitude: current.coords.longitude, accuracy: current.coords.accuracy } };
  }

  private async contacts(query: string): Promise<CapabilityResult> {
    const permission = await Contacts.requestPermissionsAsync();
    if (!permission.granted) {
      return { name: "iphone.contacts.lookup", status: "denied", reason: "permission_denied", value: null };
    }
    const response = await Contacts.getContactsAsync({ fields: [Contacts.Fields.PhoneNumbers, Contacts.Fields.Emails], name: query, pageSize: 25 });
    return { name: "iphone.contacts.lookup", status: "completed", value: response.data.map((contact) => ({ id: contact.id, name: contact.name, phoneNumbers: contact.phoneNumbers?.flatMap((entry) => entry.number ? [entry.number] : []) ?? [], emails: contact.emails?.flatMap((entry) => entry.email ? [entry.email] : []) ?? [] })) };
  }

  private async events(start: string, end: string): Promise<CapabilityResult> {
    const permission = await Calendar.requestCalendarPermissionsAsync();
    if (!permission.granted) {
      return { name: "iphone.calendar.events", status: "denied", reason: "permission_denied", value: null };
    }
    const calendars = await Calendar.getCalendarsAsync(Calendar.EntityTypes.EVENT);
    const events = await Calendar.getEventsAsync(calendars.map((calendar) => calendar.id), new Date(start), new Date(end));
    return { name: "iphone.calendar.events", status: "completed", value: events.slice(0, 100).map((event) => ({ id: event.id, title: event.title, start: new Date(event.startDate).toISOString(), end: new Date(event.endDate).toISOString() })) };
  }

  private async photo(): Promise<CapabilityResult> {
    const permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!permission.granted) {
      return { name: "iphone.photos.pick", status: "denied", reason: "permission_denied", value: null };
    }
    const response = await ImagePicker.launchImageLibraryAsync({ mediaTypes: ["images"], allowsMultipleSelection: false });
    const asset = response.assets?.[0];
    return asset ? { name: "iphone.photos.pick", status: "completed", value: { uri: asset.uri, width: asset.width, height: asset.height } } : { name: "iphone.photos.pick", status: "cancelled", value: null };
  }

  private async mail(arguments_: { recipients?: string[]; subject?: string; body?: string }): Promise<CapabilityResult> {
    if (!(await MailComposer.isAvailableAsync())) {
      return { name: "iphone.mail.compose", status: "denied", reason: "unavailable", value: null };
    }
    const response = await MailComposer.composeAsync(arguments_);
    return response.status === MailComposer.MailComposerStatus.SENT
      ? { name: "iphone.mail.compose", status: "completed", value: { composed: true } }
      : { name: "iphone.mail.compose", status: "cancelled", value: null };
  }

  private async sms(arguments_: { recipients?: string[]; message?: string }): Promise<CapabilityResult> {
    if (!(await SMS.isAvailableAsync())) {
      return { name: "iphone.sms.compose", status: "denied", reason: "unavailable", value: null };
    }
    const response = await SMS.sendSMSAsync(arguments_.recipients ?? [], arguments_.message ?? "");
    return response.result === "sent"
      ? { name: "iphone.sms.compose", status: "completed", value: { composed: true } }
      : { name: "iphone.sms.compose", status: "cancelled", value: null };
  }
}
