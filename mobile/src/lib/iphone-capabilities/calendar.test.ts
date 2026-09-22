import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import * as Calendar from "expo-calendar";
import { executeAgenda } from "./calendar";
import { capabilityActionDigest, parseCapabilityResult } from "./grant";

jest.mock("expo-calendar", () => ({
  EntityTypes: { EVENT: "event", REMINDER: "reminder" },
  requestCalendarPermissionsAsync: jest.fn(), requestRemindersPermissionsAsync: jest.fn(),
  getCalendarsAsync: jest.fn(), createEventAsync: jest.fn(), updateEventAsync: jest.fn(), getEventAsync: jest.fn(),
  createReminderAsync: jest.fn(), updateReminderAsync: jest.fn(), getReminderAsync: jest.fn(), getRemindersAsync: jest.fn(),
}));
const eventArgs = { calendar_id: "local", title: "Rendez-vous", start: "2026-10-01T15:00:00Z", end: "2026-10-01T16:00:00Z" };
const savedEvent = { id: "event", calendarId: "local", title: eventArgs.title, startDate: eventArgs.start, endDate: eventArgs.end };
const reminderArgs = { calendar_id: "local", title: "Préparer le dossier", due: "2026-10-01T14:30:00Z" };
beforeEach(() => {
  jest.resetAllMocks();
  jest.mocked(Calendar.requestCalendarPermissionsAsync).mockResolvedValue({ granted: true } as never);
  jest.mocked(Calendar.requestRemindersPermissionsAsync).mockResolvedValue({ granted: true } as never);
  jest.mocked(Calendar.getCalendarsAsync).mockResolvedValue([{ id: "local", title: "Personnel", allowsModifications: true }] as never);
});
describe("approved agenda native operations", () => {
  it("creates an event and only reports completed after matching native read-back", async () => {
    jest.mocked(Calendar.createEventAsync).mockResolvedValue("event");
    jest.mocked(Calendar.getEventAsync).mockResolvedValue(savedEvent as never);
    const result = await executeAgenda({ name: "iphone.calendar.event.create", arguments: eventArgs });
    expect(result).toEqual({ name: "iphone.calendar.event.create", status: "completed", value: { id: "event", title: eventArgs.title, start: "2026-10-01T15:00:00.000Z", end: "2026-10-01T16:00:00.000Z" } });
    expect(parseCapabilityResult(result, result.name)).toEqual(result);
    expect(Calendar.getEventAsync).toHaveBeenCalledWith("event");
  });
  it("does not claim success or retry when saved event cannot be verified", async () => {
    jest.mocked(Calendar.createEventAsync).mockResolvedValue("event");
    jest.mocked(Calendar.getEventAsync).mockResolvedValue({ ...savedEvent, title: "Other" } as never);
    await expect(executeAgenda({ name: "iphone.calendar.event.create", arguments: eventArgs })).rejects.toThrow("read-back");
    expect(Calendar.createEventAsync).toHaveBeenCalledTimes(1);
  });
  it("does not write when permission is denied", async () => {
    jest.mocked(Calendar.requestCalendarPermissionsAsync).mockResolvedValue({ granted: false } as never);
    await expect(executeAgenda({ name: "iphone.calendar.event.create", arguments: eventArgs })).resolves.toMatchObject({ status: "denied", reason: "permission_denied" });
    expect(Calendar.createEventAsync).not.toHaveBeenCalled();
  });
  it("does not write to an absent or read-only calendar", async () => {
    jest.mocked(Calendar.getCalendarsAsync).mockResolvedValue([{ id: "local", allowsModifications: false }] as never);
    await expect(executeAgenda({ name: "iphone.calendar.event.create", arguments: eventArgs })).rejects.toThrow("read-only");
    expect(Calendar.createEventAsync).not.toHaveBeenCalled();
  });
  it("updates a single non-recurring event with read-back", async () => {
    jest.mocked(Calendar.getEventAsync).mockResolvedValue(savedEvent as never);
    jest.mocked(Calendar.updateEventAsync).mockResolvedValue("event");
    const { calendar_id, ...fields } = eventArgs;
    await expect(executeAgenda({ name: "iphone.calendar.event.update", arguments: { id: "event", ...fields } })).resolves.toMatchObject({ status: "completed" });
    expect(Calendar.updateEventAsync).toHaveBeenCalledWith("event", expect.objectContaining({ title: eventArgs.title }), { futureEvents: false });
    expect(Calendar.getEventAsync).toHaveBeenCalledTimes(2);
  });
  it("rejects recurring edits before any mutation", async () => {
    jest.mocked(Calendar.getEventAsync).mockResolvedValue({ ...savedEvent, recurrenceRule: { frequency: "daily" } } as never);
    await expect(executeAgenda({ name: "iphone.calendar.event.update", arguments: { id: "event", title: eventArgs.title, start: eventArgs.start, end: eventArgs.end } })).rejects.toThrow("scope");
    expect(Calendar.updateEventAsync).not.toHaveBeenCalled();
  });
  it("creates a reminder and verifies its due date and completion", async () => {
    jest.mocked(Calendar.createReminderAsync).mockResolvedValue("reminder");
    jest.mocked(Calendar.getReminderAsync).mockResolvedValue({ id: "reminder", calendarId: "local", title: reminderArgs.title, dueDate: reminderArgs.due, completed: false });
    const result = await executeAgenda({ name: "iphone.calendar.reminder.create", arguments: reminderArgs });
    expect(result).toMatchObject({ status: "completed", value: { id: "reminder", completed: false, due: "2026-10-01T14:30:00.000Z" } });
    expect(parseCapabilityResult(result, result.name)).toEqual(result);
  });
  it("updates reminder completion and checks the saved record", async () => {
    jest.mocked(Calendar.getReminderAsync).mockResolvedValueOnce({ id: "reminder", calendarId: "local" }).mockResolvedValueOnce({ id: "reminder", calendarId: "local", title: reminderArgs.title, dueDate: reminderArgs.due, completed: true });
    jest.mocked(Calendar.updateReminderAsync).mockResolvedValue("reminder");
    await expect(executeAgenda({ name: "iphone.calendar.reminder.update", arguments: { id: "reminder", title: reminderArgs.title, due: reminderArgs.due, completed: true } })).resolves.toMatchObject({ status: "completed", value: { completed: true } });
  });
  it("lists event and reminder calendar identities without selecting one implicitly", async () => {
    const result = await executeAgenda({ name: "iphone.calendar.calendars", arguments: {} });
    expect(result).toMatchObject({ status: "completed", value: [{ id: "local", entityType: "event" }, { id: "local", entityType: "reminder" }] });
    expect(parseCapabilityResult(result, result.name)).toEqual(result);
  });
  it("reads reminders from only the approved list", async () => {
    jest.mocked(Calendar.getRemindersAsync).mockResolvedValue([{ id: "r", title: "Read", completed: false }]);
    await expect(executeAgenda({ name: "iphone.calendar.reminders", arguments: { calendar_id: "local" } })).resolves.toMatchObject({ status: "completed", value: [{ id: "r", due: null, completed: false }] });
    expect(Calendar.getRemindersAsync).toHaveBeenCalledWith(["local"], null, null, null);
  });
  it("binds all mutation fields to the approval digest and rejects unsupported fields", () => {
    const id = "iphreq_test";
    const first = capabilityActionDigest(id, "iphone.calendar.event.create", eventArgs);
    expect(capabilityActionDigest(id, "iphone.calendar.event.create", { ...eventArgs, title: "Changed" })).not.toEqual(first);
    expect(() => capabilityActionDigest(id, "iphone.calendar.event.create", { ...eventArgs, attendees: ["x"] } as never)).toThrow();
    expect(() => capabilityActionDigest(id, "iphone.calendar.reminder.create", { ...reminderArgs, due: "2026-10-01T14:30:00.123Z" })).toThrow("whole-second precision");
    expect(() => capabilityActionDigest(id, "iphone.calendar.reminder.update", { id: "x", title: "x", due: null, completed: false } as never)).toThrow();
  });
});
