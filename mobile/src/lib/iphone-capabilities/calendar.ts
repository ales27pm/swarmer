import * as Calendar from "expo-calendar";
import type { CapabilityRequest, CapabilityResult, CalendarEventReceipt, CalendarReminderReceipt } from "./types";

type AgendaRequest = Extract<CapabilityRequest, { name:
  | "iphone.calendar.reminders" | "iphone.calendar.calendars" | "iphone.calendar.event.create" | "iphone.calendar.event.update"
  | "iphone.calendar.reminder.create" | "iphone.calendar.reminder.update" }>;

function eventReceipt(event: Calendar.Event): CalendarEventReceipt {
  return { id: event.id, title: event.title, start: new Date(event.startDate).toISOString(), end: new Date(event.endDate).toISOString() };
}
function reminderReceipt(reminder: Calendar.Reminder): CalendarReminderReceipt {
  if (!reminder.id) throw new Error("Reminder read-back has no identity.");
  return { id: reminder.id, title: reminder.title ?? "", due: reminder.dueDate ? new Date(reminder.dueDate).toISOString() : null, completed: reminder.completed === true };
}
async function writableCalendar(id: string, type: Calendar.EntityTypes) {
  const calendars = await Calendar.getCalendarsAsync(type);
  if (!calendars.some((calendar) => calendar.id === id && calendar.allowsModifications)) {
    throw new Error("The selected calendar is unavailable or read-only.");
  }
}

/** Execute only after server approval/consumption. A saved identifier alone is not proof of the requested result. */
export async function executeAgenda(request: AgendaRequest): Promise<CapabilityResult> {
  const name = request.name;
  const reminder = name.includes("reminder");
  const permission = reminder ? await Calendar.requestRemindersPermissionsAsync() : await Calendar.requestCalendarPermissionsAsync();
  if (!permission.granted) return { name, status: "denied", reason: "permission_denied", value: null };

  if (request.name === "iphone.calendar.reminders") {
    const calendars = await Calendar.getCalendarsAsync(Calendar.EntityTypes.REMINDER);
    if (!calendars.some((calendar) => calendar.id === request.arguments.calendar_id)) throw new Error("Reminder list is unavailable.");
    const reminders = await Calendar.getRemindersAsync([request.arguments.calendar_id], null, null, null);
    return { name: request.name, status: "completed", value: reminders.slice(0, 100).map(reminderReceipt) };
  }
  if (request.name === "iphone.calendar.calendars") {
    const reminderPermission = await Calendar.requestRemindersPermissionsAsync();
    if (!reminderPermission.granted) return { name, status: "denied", reason: "permission_denied", value: null };
    const events = await Calendar.getCalendarsAsync(Calendar.EntityTypes.EVENT);
    const reminders = await Calendar.getCalendarsAsync(Calendar.EntityTypes.REMINDER);
    return { name: request.name, status: "completed", value: [...events.map((item) => ({ ...item, entityType: "event" as const })),
      ...reminders.map((item) => ({ ...item, entityType: "reminder" as const }))].slice(0, 100).map((item) => ({
        id: item.id, title: item.title, entityType: item.entityType, allowsModifications: item.allowsModifications,
      })) };
  }
  if (request.name === "iphone.calendar.event.create" || request.name === "iphone.calendar.event.update") {
    const args = request.arguments;
    const details = { title: args.title, startDate: new Date(args.start), endDate: new Date(args.end), allDay: false };
    let id: string;
    let calendarId: string;
    if (request.name === "iphone.calendar.event.create") {
      calendarId = request.arguments.calendar_id;
      await writableCalendar(calendarId, Calendar.EntityTypes.EVENT);
      id = await Calendar.createEventAsync(request.arguments.calendar_id, details);
    } else {
      const existing = await Calendar.getEventAsync(request.arguments.id);
      // Recurring edits need explicit occurrence/series scope, which this v1 contract does not expose.
      if (existing.recurrenceRule || existing.isDetached) throw new Error("Recurring events need explicit occurrence scope.");
      calendarId = existing.calendarId;
      await writableCalendar(calendarId, Calendar.EntityTypes.EVENT);
      id = await Calendar.updateEventAsync(request.arguments.id, details, { futureEvents: false });
    }
    const saved = await Calendar.getEventAsync(id);
    if (saved.calendarId !== calendarId) throw new Error("Event read-back returned a different calendar.");
    const value = eventReceipt(saved);
    if (value.id !== id || value.title !== args.title || Date.parse(value.start) !== Date.parse(args.start) || Date.parse(value.end) !== Date.parse(args.end)) {
      throw new Error("Event read-back did not confirm the requested edit. Do not automatically retry.");
    }
    return { name: request.name, status: "completed", value };
  }
  const args = request.arguments;
  // EventKit uses calendar date components for reminders: fractional seconds are not preserved.
  if (args.due !== null && Date.parse(args.due) % 1_000 !== 0) throw new Error("Reminder due times require whole-second precision.");
  const details: Calendar.Reminder = { title: args.title, allDay: false, ...(args.due === null ? {} : { dueDate: new Date(args.due) }) };
  let id: string;
  let calendarId: string;
  let expectedCompleted = false;
  if (request.name === "iphone.calendar.reminder.create") {
    calendarId = request.arguments.calendar_id;
    await writableCalendar(calendarId, Calendar.EntityTypes.REMINDER);
    id = await Calendar.createReminderAsync(request.arguments.calendar_id, { ...details, completed: false });
  } else {
    const existing = await Calendar.getReminderAsync(request.arguments.id);
    if (existing.recurrenceRule) throw new Error("Recurring reminders need explicit occurrence scope.");
    if (!existing.calendarId) throw new Error("Reminder has no calendar.");
    calendarId = existing.calendarId;
    await writableCalendar(calendarId, Calendar.EntityTypes.REMINDER);
    expectedCompleted = request.arguments.completed;
    id = await Calendar.updateReminderAsync(request.arguments.id, { ...details, completed: expectedCompleted });
  }
  const saved = await Calendar.getReminderAsync(id);
  if (saved.calendarId !== calendarId) throw new Error("Reminder read-back returned a different list.");
  const value = reminderReceipt(saved);
  if (value.id !== id || value.title !== args.title || value.completed !== expectedCompleted ||
    (args.due === null ? value.due !== null : value.due === null || Date.parse(value.due) !== Date.parse(args.due))) {
    throw new Error("Reminder read-back did not confirm the requested edit. Do not automatically retry.");
  }
  return { name: request.name, status: "completed", value };
}
