import { Platform } from "react-native";
import * as SQLite from "expo-sqlite";

const NAME = "mongars-replica.db";
const STAGING = "mongars-replica-migration.db";
const MARKER = "_mongars_storage_location";
const SIDECARS = ["-wal", "-shm", "-journal"];
type FileSystem = typeof import("expo-file-system");
type SchemaRow = { type: string; name: string; tbl_name: string; sql: string | null };

/** Expo SQLite iOS returns an absolute Documents/SQLite path, not a storage URL. */
export function privateReplicaDirectory(defaultDirectory: unknown): string {
  if (typeof defaultDirectory !== "string" || !defaultDirectory.startsWith("/")
      || defaultDirectory.includes("\0") || defaultDirectory.includes("\\")
      || defaultDirectory.split("/").some((part) => part === "." || part === "..")
      || !defaultDirectory.endsWith("/Documents/SQLite")) {
    throw new Error("Le chemin privé de la base iPhone n’a pas pu être vérifié.");
  }
  return defaultDirectory.slice(0, -"Documents/SQLite".length) + "Library/Application Support/Swarmer/SQLite";
}

function uri(path: string): string {
  return "file://" + path.split("/").map(encodeURIComponent).join("/");
}

function databaseFile(fs: FileSystem, directory: string, name = NAME) {
  return new fs.File(uri(directory + "/" + name));
}

function sidecarsExist(fs: FileSystem, directory: string, name = NAME): boolean {
  return SIDECARS.some((suffix) => databaseFile(fs, directory, name + suffix).exists);
}

function removeSidecars(fs: FileSystem, directory: string, name = NAME) {
  for (const suffix of SIDECARS) {
    const file = databaseFile(fs, directory, name + suffix);
    if (file.exists) file.delete();
  }
}

async function integrity(database: SQLite.SQLiteDatabase) {
  const rows = await database.getAllAsync<Record<string, unknown>>("PRAGMA integrity_check");
  if (rows.length !== 1 || Object.values(rows[0])[0] !== "ok") {
    throw new Error("La base locale n’a pas passé la vérification d’intégrité. Les données sont conservées.");
  }
}

function stable(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => item instanceof Uint8Array ? [...item] : item);
}

/** Compare every table, including pending outbox entries, without logging its contents. */
async function contents(database: SQLite.SQLiteDatabase): Promise<string> {
  const schema = await database.getAllAsync<SchemaRow>(
    "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name<>? AND tbl_name<>? ORDER BY type,name",
    MARKER, MARKER,
  );
  const tables: Record<string, string[]> = {};
  for (const row of schema.filter((row) => row.type === "table")) {
    const quoted = '"' + row.name.replaceAll('"', '""') + '"';
    const data = await database.getAllAsync<Record<string, unknown>>("SELECT * FROM " + quoted);
    tables[row.name] = data.map((record) => stable(Object.entries(record).sort(([a], [b]) => a.localeCompare(b)))).sort();
  }
  return stable({ schema, tables, version: await database.getFirstAsync("PRAGMA user_version") });
}

async function marker(database: SQLite.SQLiteDatabase): Promise<string | null> {
  const exists = await database.getFirstAsync("SELECT name FROM sqlite_master WHERE type='table' AND name=?", MARKER);
  if (!exists) return null;
  const rows = await database.getAllAsync<{ version: number; phase: string }>(`SELECT version,phase FROM ${MARKER}`);
  if (rows.length !== 1 || rows[0].version !== 1 || !["cleanup", "ready"].includes(rows[0].phase)) {
    throw new Error("L’état de migration de la base privée est invalide. Les copies sont conservées.");
  }
  return rows[0].phase;
}

async function setMarker(database: SQLite.SQLiteDatabase, phase: "cleanup" | "ready") {
  await database.withTransactionAsync(async () => {
    await database.execAsync(`CREATE TABLE IF NOT EXISTS ${MARKER} (version INTEGER NOT NULL, phase TEXT NOT NULL)`);
    await database.runAsync(`DELETE FROM ${MARKER}`);
    await database.runAsync(`INSERT INTO ${MARKER}(version,phase) VALUES(1,?)`, phase);
  });
}

async function standalone(database: SQLite.SQLiteDatabase) {
  const checkpoint = await database.getFirstAsync<{ busy: number }>("PRAGMA wal_checkpoint(TRUNCATE)");
  if (checkpoint?.busy !== 0) throw new Error("La base locale est encore utilisée. La migration est reportée.");
  const mode = await database.getFirstAsync<{ journal_mode: string }>("PRAGMA journal_mode=DELETE");
  if (mode?.journal_mode !== "delete") throw new Error("Le journal de la base locale n’a pas été consolidé.");
}

async function migrateCopy(fs: FileSystem, legacy: string, target: string) {
  const source = await SQLite.openDatabaseAsync(NAME, { useNewConnection: true }, uri(legacy));
  let destination: SQLite.SQLiteDatabase | null = null;
  try {
    await source.execAsync("BEGIN");
    await integrity(source);
    const expected = await contents(source);
    // A previous incomplete staging file is disposable only while the source is intact.
    const staging = databaseFile(fs, target, STAGING);
    if (staging.exists) await SQLite.deleteDatabaseAsync(STAGING, uri(target));
    removeSidecars(fs, target, STAGING);
    destination = await SQLite.openDatabaseAsync(STAGING, { useNewConnection: true }, uri(target));
    await SQLite.backupDatabaseAsync({ sourceDatabase: source, destDatabase: destination });
    await integrity(destination);
    if (await contents(destination) !== expected) throw new Error("La copie privée diffère de la base existante. L’original est conservé.");
    await setMarker(destination, "cleanup");
    await standalone(destination);
    await destination.closeAsync();
    destination = null;
    await source.execAsync("ROLLBACK");
    // Publishing never replaces an existing private database.
    if (databaseFile(fs, target).exists || sidecarsExist(fs, target, STAGING)) {
      throw new Error("La publication de la base privée est bloquée; les copies sont conservées.");
    }
    staging.move(databaseFile(fs, target));
  } finally {
    try {
      if (destination) await destination.closeAsync();
    } finally {
      await source.closeAsync();
    }
  }
}

async function cleanupLegacy(fs: FileSystem, legacy: string, database: SQLite.SQLiteDatabase) {
  await integrity(database);
  if (databaseFile(fs, legacy).exists) {
    const source = await SQLite.openDatabaseAsync(NAME, { useNewConnection: true }, uri(legacy));
    try {
      await integrity(source);
      if (await contents(source) !== await contents(database)) {
        throw new Error("L’ancienne base a changé. Aucune copie n’est supprimée.");
      }
      await standalone(source);
    } finally {
      await source.closeAsync();
    }
    await SQLite.deleteDatabaseAsync(NAME, uri(legacy));
  }
  // Only these known sidecars can remain after a crash between deletion and the ready marker.
  removeSidecars(fs, legacy);
  if (databaseFile(fs, legacy).exists || sidecarsExist(fs, legacy)) {
    throw new Error("Le nettoyage de Documents est incomplet. La base privée est conservée.");
  }
  await setMarker(database, "ready");
}

async function openIOSDatabase(): Promise<SQLite.SQLiteDatabase> {
  const legacy = SQLite.defaultDatabaseDirectory as unknown;
  const target = privateReplicaDirectory(legacy);
  const directory = legacy as string;
  // Keep the native filesystem module out of the unchanged Android/web open path.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const fs = require("expo-file-system") as FileSystem;
  new fs.Directory(uri(target)).create({ intermediates: true, idempotent: true });
  if (!databaseFile(fs, target).exists) {
    if (sidecarsExist(fs, target)) throw new Error("Des journaux privés isolés nécessitent une vérification; les fichiers sont conservés.");
    if (databaseFile(fs, directory).exists) await migrateCopy(fs, directory, target);
    else if (sidecarsExist(fs, directory) || databaseFile(fs, target, STAGING).exists || sidecarsExist(fs, target, STAGING)) {
      throw new Error("Une migration interrompue nécessite une vérification; les fichiers sont conservés.");
    }
  }
  const database = await SQLite.openDatabaseAsync(NAME, {}, uri(target));
  try {
    const phase = await marker(database);
    if (phase === "cleanup") await cleanupLegacy(fs, directory, database);
    else if (phase === "ready") {
      if (databaseFile(fs, directory).exists || sidecarsExist(fs, directory)) {
        throw new Error("Une ancienne base est réapparue dans Documents; aucune donnée n’est remplacée.");
      }
    } else {
      const schema = await database.getAllAsync("SELECT name FROM sqlite_master");
      if (schema.length || databaseFile(fs, directory).exists || sidecarsExist(fs, directory)) {
        throw new Error("La provenance de la base privée n’est pas confirmée; les copies sont conservées.");
      }
      await setMarker(database, "ready");
    }
    return database;
  } catch (cause) {
    await database.closeAsync();
    throw cause;
  }
}

/** Both the replica and mutation outbox must wait for this same migration promise. */
export function createReplicaDatabaseOpener(platform: string = Platform.OS) {
  let opening: Promise<SQLite.SQLiteDatabase> | null = null;
  return (): Promise<SQLite.SQLiteDatabase> => {
    if (!opening) {
      opening = (platform === "ios" ? openIOSDatabase() : SQLite.openDatabaseAsync(NAME))
        .catch((cause) => { opening = null; throw cause; });
    }
    return opening;
  };
}

export const openReplicaDatabase = createReplicaDatabaseOpener();
