import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import * as SQLite from "expo-sqlite";

import { createReplicaDatabaseOpener, privateReplicaDirectory } from "@/lib/state/database-location";

const LEGACY = "/container/Documents/SQLite";
const PRIVATE = "/container/Library/Application Support/Swarmer/SQLite";
const NAME = "mongars-replica.db";
const MARKER = "_mongars_storage_location";
type Store = { tables: Record<string, Record<string, unknown>[]>; version: number; integrity: string };
const mockFiles = new Set<string>();
const mockStores = new Map<string, Store>();
const mockHandles: FakeDatabase[] = [];
let mockFailDelete = false;
let mockFailReady = false;
let mockFailMove = false;
let mockBusy = false;

function copy<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T; }
function mockPath(value: string): string { return decodeURIComponent(value.replace(/^file:\/\//, "")); }
function store(): Store { return { tables: {}, version: 0, integrity: "ok" }; }

class FakeDatabase {
  closed = false;
  constructor(readonly databasePath: string) { mockHandles.push(this); }
  get data() { return mockStores.get(this.databasePath) as Store; }
  async execAsync(sql: string) {
    if (sql.startsWith("CREATE TABLE")) this.data.tables[MARKER] ??= [];
  }
  async getAllAsync(sql: string, ...params: unknown[]): Promise<unknown[]> {
    if (sql === "PRAGMA integrity_check") return [{ integrity_check: this.data.integrity }];
    if (sql.startsWith("SELECT type,name")) {
      return Object.keys(this.data.tables).filter((name) => name !== MARKER).sort().map((name) => ({ type: "table", name, tbl_name: name, sql: "CREATE TABLE " + name }));
    }
    if (sql === "SELECT name FROM sqlite_master") return Object.keys(this.data.tables).map((name) => ({ name }));
    if (sql.startsWith("SELECT * FROM")) return copy(this.data.tables[sql.split('"')[1]]);
    if (sql.startsWith("SELECT version,phase")) return copy(this.data.tables[MARKER]);
    throw new Error("Unexpected read: " + sql + " params=" + params.length);
  }
  async getFirstAsync(sql: string, ...params: unknown[]): Promise<unknown> {
    if (sql === "PRAGMA user_version") return { user_version: this.data.version };
    if (sql.startsWith("SELECT name FROM sqlite_master WHERE")) return this.data.tables[String(params[0])] ? { name: params[0] } : null;
    if (sql === "PRAGMA wal_checkpoint(TRUNCATE)") return { busy: mockBusy && this.databasePath.startsWith(LEGACY) ? 1 : 0 };
    if (sql === "PRAGMA journal_mode=DELETE") {
      for (const suffix of ["-wal", "-shm", "-journal"]) mockFiles.delete(this.databasePath + suffix);
      return { journal_mode: "delete" };
    }
    throw new Error("Unexpected first read: " + sql);
  }
  async runAsync(sql: string, ...params: unknown[]) {
    if (sql.startsWith("DELETE FROM")) this.data.tables[MARKER] = [];
    else if (sql.startsWith("INSERT INTO")) {
      if (mockFailReady && params[0] === "ready") throw new Error("Simulated interruption while committing ready");
      this.data.tables[MARKER] = [{ version: 1, phase: params[0] }];
    } else throw new Error("Unexpected write: " + sql);
  }
  async withTransactionAsync(operation: () => Promise<void>) {
    const before = copy(this.data);
    try { await operation(); } catch (cause) { mockStores.set(this.databasePath, before); throw cause; }
  }
  async closeAsync() { this.closed = true; }
}

jest.mock("expo-sqlite", () => ({
  defaultDatabaseDirectory: "/container/Documents/SQLite",
  openDatabaseAsync: jest.fn(), backupDatabaseAsync: jest.fn(), deleteDatabaseAsync: jest.fn(),
}));
jest.mock("expo-file-system", () => ({
  File: class {
    location: string;
    constructor(uri: string) { this.location = mockPath(uri); }
    get exists() { return mockFiles.has(this.location); }
    delete() { mockFiles.delete(this.location); mockStores.delete(this.location); }
    move(target: { location: string }) {
      if (mockFailMove) throw new Error("Simulated interruption before publication");
      if (mockFiles.has(target.location)) throw new Error("Destination already exists");
      mockFiles.add(target.location); mockFiles.delete(this.location);
      mockStores.set(target.location, mockStores.get(this.location) as Store);
      mockStores.delete(this.location); this.location = target.location;
    }
  },
  Directory: class { create() {} },
}));

function seedLegacy() {
  const data: Store = { version: 7, integrity: "ok", tables: {
    tasks: [{ id: "task1", payload: "retained offline task" }],
    sync_meta: [{ key: "cursor", value: "cursor_123" }, { key: "scope", value: "https://paired.example" }],
    mutation_outbox: [{ id: "mutation1", payload_json: "pending draft", idempotency_key: "key1", attempts: 2, completed_at: null }],
  } };
  mockStores.set(LEGACY + "/" + NAME, copy(data));
  mockFiles.add(LEGACY + "/" + NAME);
  // Logical SQLite rows already include writes represented by an existing WAL.
  mockFiles.add(LEGACY + "/" + NAME + "-wal");
  mockFiles.add(LEGACY + "/" + NAME + "-shm");
  return data;
}

describe("private iOS replica relocation", () => {
  beforeEach(() => {
    jest.resetAllMocks(); mockFiles.clear(); mockStores.clear(); mockHandles.length = 0;
    mockFailDelete = false; mockFailReady = false; mockFailMove = false; mockBusy = false;
    jest.mocked(SQLite.openDatabaseAsync).mockImplementation(async (name, _options, directory = LEGACY) => {
      if (directory.includes(" ") && !directory.startsWith("file://")) throw new Error("SQLite custom directory must be a file URI");
      const full = mockPath(directory) + "/" + name;
      if (!mockStores.has(full)) mockStores.set(full, store());
      mockFiles.add(full);
      return new FakeDatabase(full) as unknown as SQLite.SQLiteDatabase;
    });
    jest.mocked(SQLite.backupDatabaseAsync).mockImplementation(async ({ sourceDatabase, destDatabase }) => {
      const source = sourceDatabase as unknown as FakeDatabase;
      const destination = destDatabase as unknown as FakeDatabase;
      mockStores.set(destination.databasePath, copy(source.data));
    });
    jest.mocked(SQLite.deleteDatabaseAsync).mockImplementation(async (name, directory = LEGACY) => {
      const full = mockPath(directory) + "/" + name;
      if (mockHandles.some((handle) => handle.databasePath === full && !handle.closed)) throw new Error("Database still open");
      if (mockFailDelete && mockPath(directory) === LEGACY) throw new Error("Simulated delete failure");
      mockFiles.delete(full); mockStores.delete(full);
    });
  });

  it("derives a private sibling and refuses ambiguous directories", () => {
    expect(privateReplicaDirectory(LEGACY)).toBe(PRIVATE);
    for (const value of [null, "/Documents/../Documents/SQLite", "relative/Documents/SQLite", "/container/Documents", "file:///container/Documents/SQLite"]) {
      expect(() => privateReplicaDirectory(value)).toThrow();
    }
  });

  it.each(["android", "web"])("keeps the existing directory on %s", async (platform) => {
    await createReplicaDatabaseOpener(platform)();
    expect(SQLite.openDatabaseAsync).toHaveBeenCalledWith(NAME);
    expect(SQLite.backupDatabaseAsync).not.toHaveBeenCalled();
  });

  it("creates a new private database without creating anything in Documents", async () => {
    await createReplicaDatabaseOpener("ios")();
    expect([...mockFiles]).toEqual([PRIVATE + "/" + NAME]);
    expect(SQLite.openDatabaseAsync).toHaveBeenCalledWith(NAME, {}, "file:///container/Library/Application%20Support/Swarmer/SQLite");
    expect(SQLite.backupDatabaseAsync).not.toHaveBeenCalled();
  });

  it("uses the same decoded path for filesystem and SQLite when directories contain spaces or Unicode", async () => {
    const replacement = jest.replaceProperty(SQLite, "defaultDatabaseDirectory", "/conteneur été/Documents/SQLite");
    try {
      await createReplicaDatabaseOpener("ios")();
      expect(SQLite.openDatabaseAsync).toHaveBeenCalledWith(NAME, {}, "file:///conteneur%20%C3%A9t%C3%A9/Library/Application%20Support/Swarmer/SQLite");
      expect([...mockFiles]).toEqual(["/conteneur été/Library/Application Support/Swarmer/SQLite/" + NAME]);
    } finally { replacement.restore(); }
  });

  it("backs up every logical row and pending outbox record before removing the old database and sidecars", async () => {
    const before = seedLegacy();
    await createReplicaDatabaseOpener("ios")();
    const after = mockStores.get(PRIVATE + "/" + NAME) as Store;
    expect(after.version).toBe(7);
    expect({ ...after.tables, [MARKER]: undefined }).toEqual({ ...before.tables, [MARKER]: undefined });
    expect(after.tables[MARKER]).toEqual([{ version: 1, phase: "ready" }]);
    expect([...mockFiles].some((file) => file.startsWith(LEGACY))).toBe(false);
    expect(SQLite.backupDatabaseAsync).toHaveBeenCalledTimes(1);
  });

  it("serializes concurrent replica/outbox opens on one migration and one connection", async () => {
    seedLegacy();
    const open = createReplicaDatabaseOpener("ios");
    const first = open(); const second = open();
    expect(first).toBe(second);
    expect(await first).toBe(await second);
    expect(SQLite.backupDatabaseAsync).toHaveBeenCalledTimes(1);
  });

  it("preserves the source after a partial failed backup and safely retries", async () => {
    const before = seedLegacy();
    jest.mocked(SQLite.backupDatabaseAsync).mockRejectedValueOnce(new Error("Disk full"));
    const open = createReplicaDatabaseOpener("ios");
    await expect(open()).rejects.toThrow("Disk full");
    expect(mockStores.get(LEGACY + "/" + NAME)).toEqual(before);
    expect(mockHandles.every((handle) => handle.closed)).toBe(true);
    await open();
    expect(mockFiles.has(LEGACY + "/" + NAME)).toBe(false);
  });

  it("rejects a backup with a missing outbox entry before deleting any original data", async () => {
    const before = seedLegacy();
    jest.mocked(SQLite.backupDatabaseAsync).mockImplementationOnce(async ({ sourceDatabase, destDatabase }) => {
      const copied = copy((sourceDatabase as unknown as FakeDatabase).data);
      copied.tables.mutation_outbox = [];
      mockStores.set((destDatabase as unknown as FakeDatabase).databasePath, copied);
    });
    await expect(createReplicaDatabaseOpener("ios")()).rejects.toThrow("copie privée diffère");
    expect(mockStores.get(LEGACY + "/" + NAME)).toEqual(before);
    expect(SQLite.deleteDatabaseAsync).not.toHaveBeenCalled();
  });

  it("retries publication failure with the original still intact", async () => {
    seedLegacy(); mockFailMove = true;
    const open = createReplicaDatabaseOpener("ios");
    await expect(open()).rejects.toThrow("publication");
    expect(mockFiles.has(LEGACY + "/" + NAME)).toBe(true);
    mockFailMove = false;
    await open();
    expect(mockFiles.has(LEGACY + "/" + NAME)).toBe(false);
  });

  it("resumes cleanup without recopying after deletion failed", async () => {
    seedLegacy(); mockFailDelete = true;
    await expect(createReplicaDatabaseOpener("ios")()).rejects.toThrow("delete failure");
    expect(mockStores.get(PRIVATE + "/" + NAME)?.tables[MARKER]).toEqual([{ version: 1, phase: "cleanup" }]);
    expect(mockFiles.has(LEGACY + "/" + NAME)).toBe(true);
    mockFailDelete = false;
    await createReplicaDatabaseOpener("ios")();
    expect(SQLite.backupDatabaseAsync).toHaveBeenCalledTimes(1);
    expect(mockFiles.has(LEGACY + "/" + NAME)).toBe(false);
  });

  it("resumes from the verified private copy after deletion succeeded but ready commit failed", async () => {
    const before = seedLegacy(); mockFailReady = true;
    await expect(createReplicaDatabaseOpener("ios")()).rejects.toThrow("committing ready");
    expect(mockFiles.has(LEGACY + "/" + NAME)).toBe(false);
    mockFailReady = false;
    await createReplicaDatabaseOpener("ios")();
    expect(mockStores.get(PRIVATE + "/" + NAME)?.tables.mutation_outbox).toEqual(before.tables.mutation_outbox);
    expect(SQLite.backupDatabaseAsync).toHaveBeenCalledTimes(1);
  });

  it("preserves both copies if legacy data changed during an interrupted cleanup", async () => {
    seedLegacy(); mockFailDelete = true;
    await expect(createReplicaDatabaseOpener("ios")()).rejects.toThrow();
    mockStores.get(LEGACY + "/" + NAME)?.tables.mutation_outbox.push({ id: "later" });
    mockFailDelete = false;
    await expect(createReplicaDatabaseOpener("ios")()).rejects.toThrow("ancienne base a changé");
    expect(mockFiles.has(LEGACY + "/" + NAME)).toBe(true);
    expect(mockFiles.has(PRIVATE + "/" + NAME)).toBe(true);
  });

  it("does not delete a database that still has a busy journal", async () => {
    seedLegacy(); mockBusy = true;
    await expect(createReplicaDatabaseOpener("ios")()).rejects.toThrow("encore utilisée");
    expect(mockFiles.has(LEGACY + "/" + NAME)).toBe(true);
    expect(SQLite.deleteDatabaseAsync).not.toHaveBeenCalled();
  });

  it("refuses to overwrite an unrecognized private database or orphaned WAL", async () => {
    seedLegacy(); mockStores.set(PRIVATE + "/" + NAME, { ...store(), tables: { unknown: [{ value: 1 }] } });
    mockFiles.add(PRIVATE + "/" + NAME);
    await expect(createReplicaDatabaseOpener("ios")()).rejects.toThrow("provenance");
    expect(SQLite.backupDatabaseAsync).not.toHaveBeenCalled();
    mockFiles.clear(); mockStores.clear(); mockFiles.add(LEGACY + "/" + NAME + "-wal");
    await expect(createReplicaDatabaseOpener("ios")()).rejects.toThrow("interrompue");
    expect(mockFiles.has(LEGACY + "/" + NAME + "-wal")).toBe(true);
  });
});
