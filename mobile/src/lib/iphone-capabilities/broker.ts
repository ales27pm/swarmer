import * as Calendar from "expo-calendar";
import * as Contacts from "expo-contacts";
import * as ImagePicker from "expo-image-picker";
import * as Location from "expo-location";
import * as MailComposer from "expo-mail-composer";
import * as SMS from "expo-sms";

import type { CapabilityGateway, CapabilityRequest, CapabilityResult } from "./types";
import { CapabilityDeniedError } from "./types";

export class IPhoneCapabilityBroker {
  constructor(
    private readonly authorize: CapabilityGateway,
    private readonly overrides: Partial<Record<CapabilityRequest["name"], (arguments_: never) => Promise<CapabilityResult>>> = {},
  ) {}

  async execute(request: CapabilityRequest): Promise<CapabilityResult> {
    const authorization = await this.authorize(request);
    if (!authorization.authorized || !authorization.authorizationId) {
      throw new CapabilityDeniedError(authorization.reason ?? "Server gateway authorization required.");
    }
    const override = this.overrides[request.name];
    if (override) return override(request.arguments as never);
    switch (request.name) {
      case "iphone.location.current": return this.location();
      case "iphone.contacts.lookup": return this.contacts(request.arguments.query);
      case "iphone.calendar.events": return this.events(request.arguments.start, request.arguments.end);
      case "iphone.photos.pick": return this.photo();
      case "iphone.mail.compose": return this.mail(request.arguments);
      case "iphone.sms.compose": return this.sms(request.arguments);
    }
  }

  private async location(): Promise<CapabilityResult> {
    const permission = await Location.requestForegroundPermissionsAsync();
    if (!permission.granted) throw new CapabilityDeniedError("Location permission denied by iOS.");
    const current = await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.Balanced });
    return { name: "iphone.location.current", status: "completed", value: { latitude: current.coords.latitude, longitude: current.coords.longitude, accuracy: current.coords.accuracy } };
  }

  private async contacts(query: string): Promise<CapabilityResult> {
    const permission = await Contacts.requestPermissionsAsync();
    if (!permission.granted) throw new CapabilityDeniedError("Contacts permission denied by iOS.");
    const response = await Contacts.getContactsAsync({ fields: [Contacts.Fields.PhoneNumbers, Contacts.Fields.Emails], name: query, pageSize: 25 });
    return { name: "iphone.contacts.lookup", status: "completed", value: response.data.map((contact) => ({ id: contact.id, name: contact.name, phoneNumbers: contact.phoneNumbers?.flatMap((entry) => entry.number ? [entry.number] : []) ?? [], emails: contact.emails?.flatMap((entry) => entry.email ? [entry.email] : []) ?? [] })) };
  }

  private async events(start: string, end: string): Promise<CapabilityResult> {
    const permission = await Calendar.requestCalendarPermissionsAsync();
    if (!permission.granted) throw new CapabilityDeniedError("Calendar permission denied by iOS.");
    const calendars = await Calendar.getCalendarsAsync(Calendar.EntityTypes.EVENT);
    const events = await Calendar.getEventsAsync(calendars.map((calendar) => calendar.id), new Date(start), new Date(end));
    return { name: "iphone.calendar.events", status: "completed", value: events.slice(0, 100).map((event) => ({ id: event.id, title: event.title, start: String(event.startDate), end: String(event.endDate) })) };
  }

  private async photo(): Promise<CapabilityResult> {
    const permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!permission.granted) throw new CapabilityDeniedError("Photos permission denied by iOS.");
    const response = await ImagePicker.launchImageLibraryAsync({ mediaTypes: ["images"], allowsMultipleSelection: false });
    const asset = response.assets?.[0];
    return asset ? { name: "iphone.photos.pick", status: "completed", value: { uri: asset.uri, width: asset.width, height: asset.height } } : { name: "iphone.photos.pick", status: "cancelled", value: null };
  }

  private async mail(arguments_: { recipients?: string[]; subject?: string; body?: string }): Promise<CapabilityResult> {
    if (!(await MailComposer.isAvailableAsync())) throw new CapabilityDeniedError("Mail composer is unavailable.");
    const response = await MailComposer.composeAsync(arguments_);
    return response.status === MailComposer.MailComposerStatus.CANCELLED
      ? { name: "iphone.mail.compose", status: "cancelled", value: null }
      : { name: "iphone.mail.compose", status: "completed", value: { composed: true } };
  }

  private async sms(arguments_: { recipients?: string[]; message?: string }): Promise<CapabilityResult> {
    if (!(await SMS.isAvailableAsync())) throw new CapabilityDeniedError("SMS composer is unavailable.");
    const response = await SMS.sendSMSAsync(arguments_.recipients ?? [], arguments_.message ?? "");
    return response.result === "cancelled"
      ? { name: "iphone.sms.compose", status: "cancelled", value: null }
      : { name: "iphone.sms.compose", status: "completed", value: { composed: true } };
  }
}
