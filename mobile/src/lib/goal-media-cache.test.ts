import { describe, expect, it, jest } from "@jest/globals";
import { createGoalMediaCache } from "./goal-media-cache";
import { mediaFixture } from "@/testing/goal-media-fixtures";

const mockEntries = new Map<string, string | Uint8Array>();
const mockDeleted: string[] = [];
jest.mock("expo-file-system", () => {
  class Directory {
    uri: string;
    constructor(...parts: (string | { uri: string })[]) { this.uri = parts.map((part) => typeof part === "string" ? part : part.uri).join("/"); }
    get exists() { return mockEntries.has(this.uri); }
    create() { mockEntries.set(this.uri, "directory"); }
    delete() { mockDeleted.push(this.uri); for (const path of mockEntries.keys()) if (path === this.uri || path.startsWith(`${this.uri}/`)) mockEntries.delete(path); }
  }
  class File extends Directory { write(bytes: Uint8Array) { mockEntries.set(this.uri, bytes); } }
  return { Directory, File, Paths: { cache: "file:///private/Library/Caches" } };
});
describe("private media cache", () => {
  it("cleans old process files once, isolates active previews and deletes only their own files", () => {
    const root = "file:///private/Library/Caches/goal-media-v1";
    mockEntries.set(root, "directory"); mockEntries.set(`${root}/abandoned/audio.wav`, "old");
    const first = createGoalMediaCache(); const second = createGoalMediaCache(); const fixture = mediaFixture();
    const one = first.put(fixture.bytes, fixture.artifact), two = second.put(fixture.bytes, fixture.artifact);
    expect(one).toMatch(/^file:\/\/\/private\/Library\/Caches\/goal-media-v1\//);
    expect(one).not.toEqual(two); expect(mockEntries.has(`${root}/abandoned/audio.wav`)).toBe(false);
    expect(mockDeleted.filter((path) => path === root)).toHaveLength(1);
    first.dispose(); expect(mockEntries.has(one)).toBe(false); expect(mockEntries.has(two)).toBe(true);
    first.dispose(); second.dispose(); expect(mockEntries.has(two)).toBe(false);
    expect(() => first.put(fixture.bytes, fixture.artifact)).toThrow();
  });
});
