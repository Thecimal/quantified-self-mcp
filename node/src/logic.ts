import Database from "better-sqlite3";
import { mkdirSync, writeFileSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { homedir, platform } from "node:os";

// ---------------------------------------------------------------------------
// Schema + versioned migrations (mirrors logic.py's MIGRATIONS list, applied
// via SQLite's own PRAGMA user_version so no separate migrations table is
// needed). Append new migrations here; never edit or remove an existing one.
// ---------------------------------------------------------------------------

type Migration = [number, string, (db: Database.Database) => void];

function migrateV1CreateTable(db: Database.Database) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS daily_metrics (
      date TEXT PRIMARY KEY,
      steps INTEGER,
      sleep_hours REAL,
      resting_heart_rate INTEGER
    );
  `);
}

const ADDED_COLUMNS_V2: Record<string, string> = {
  weight_kg: "REAL",
  workout_minutes: "INTEGER",
  mood: "INTEGER",
  water_ml: "INTEGER",
};

function migrateV2AddColumns(db: Database.Database) {
  const existing = new Set(
    (db.prepare("PRAGMA table_info(daily_metrics)").all() as { name: string }[]).map((r) => r.name)
  );
  for (const [name, sqltype] of Object.entries(ADDED_COLUMNS_V2)) {
    if (!existing.has(name)) db.exec(`ALTER TABLE daily_metrics ADD COLUMN ${name} ${sqltype}`);
  }
}

function migrateV3CreateMeasurements(db: Database.Database) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS measurements (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp TEXT NOT NULL,
      metric TEXT NOT NULL,
      value REAL NOT NULL,
      unit TEXT,
      source TEXT,
      source_type TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_measurements_metric_timestamp ON measurements (metric, timestamp);
  `);
}

const MEASUREMENT_PROVENANCE_COLUMNS: Record<string, string> = {
  importer: "TEXT",
  imported_at: "TEXT",
};

function migrateV4AddProvenance(db: Database.Database) {
  const existing = new Set(
    (db.prepare("PRAGMA table_info(measurements)").all() as { name: string }[]).map((r) => r.name)
  );
  for (const [name, sqltype] of Object.entries(MEASUREMENT_PROVENANCE_COLUMNS)) {
    if (!existing.has(name)) db.exec(`ALTER TABLE measurements ADD COLUMN ${name} ${sqltype}`);
  }
}

const V5_ADDED_COLUMNS: Record<string, string> = {
  heart_rate: "INTEGER",
  hrv_ms: "REAL",
};

function migrateV5AddHeartRateHrv(db: Database.Database) {
  const existing = new Set(
    (db.prepare("PRAGMA table_info(daily_metrics)").all() as { name: string }[]).map((r) => r.name)
  );
  for (const [name, sqltype] of Object.entries(V5_ADDED_COLUMNS)) {
    if (!existing.has(name)) db.exec(`ALTER TABLE daily_metrics ADD COLUMN ${name} ${sqltype}`);
  }
}

export const MIGRATIONS: Migration[] = [
  [1, "create daily_metrics table", migrateV1CreateTable],
  [2, "add weight_kg, workout_minutes, mood, water_ml columns", migrateV2AddColumns],
  [3, "create measurements table", migrateV3CreateMeasurements],
  [4, "add importer, imported_at provenance columns to measurements", migrateV4AddProvenance],
  [5, "add heart_rate, hrv_ms columns", migrateV5AddHeartRateHrv],
];

export const SCHEMA_VERSION = MIGRATIONS[MIGRATIONS.length - 1][0];

// Guardrails for read_health_data.
export const MAX_RANGE_DAYS = 3660; // ~10 years
export const MAX_ROWS_RETURNED = 400; // ~13 months of daily rows

export const BUSY_TIMEOUT_MS = 5000;

/** Bring db up to SCHEMA_VERSION, running whichever migrations haven't run yet. */
export function ensureSchema(db: Database.Database): void {
  db.pragma("journal_mode = WAL");
  const currentVersion = db.pragma("user_version", { simple: true }) as number;
  if (currentVersion > SCHEMA_VERSION) {
    console.error(
      `Database schema version ${currentVersion} is newer than this version of quantified-self-mcp expects ` +
        `(${SCHEMA_VERSION}); leaving it as-is. You may need to upgrade quantified-self-mcp.`
    );
    return;
  }
  for (const [version, , migrate] of MIGRATIONS) {
    if (version <= currentVersion) continue;
    migrate(db);
    db.pragma(`user_version = ${version}`);
  }
}

// ---------------------------------------------------------------------------
// Default data directory
// ---------------------------------------------------------------------------

function dirIsWritable(dir: string): boolean {
  try {
    mkdirSync(dir, { recursive: true });
    const probe = join(dir, `.qsm-write-check-${process.pid}`);
    writeFileSync(probe, "");
    unlinkSync(probe);
    return true;
  } catch {
    return false;
  }
}

function userDataDir(): string {
  if (platform() === "win32") {
    return process.env.APPDATA ?? join(homedir(), "AppData", "Roaming");
  }
  if (platform() === "darwin") {
    return join(homedir(), "Library", "Application Support");
  }
  return process.env.XDG_DATA_HOME ?? join(homedir(), ".local", "share");
}

/**
 * Pick a sensible default directory for the health database: prefers
 * data/ next to baseDir (the source-checkout convention), falling back
 * to a per-user data directory when that isn't writable (the common case
 * once this is installed via npm globally / npx rather than run from a
 * writable source checkout).
 */
export function defaultDataDir(baseDir: string): string {
  const sourceCheckoutDir = join(baseDir, "data");
  if (dirIsWritable(sourceCheckoutDir)) return sourceCheckoutDir;
  return join(userDataDir(), "quantified-self-mcp");
}

// ---------------------------------------------------------------------------
// Connections
// ---------------------------------------------------------------------------

/**
 * Note on encryption: the Python original optionally opens the database
 * through SQLCipher (via HEALTH_DB_PASSPHRASE) for at-rest encryption.
 * There's no equivalent wired up in this Node port yet — SQLCipher's
 * Node bindings are a separate native dependency decision left for a
 * follow-up. Plain SQLite (this port's only mode) stores health.db as an
 * ordinary, unencrypted file; use OS-level full-disk encryption
 * (FileVault/BitLocker/LUKS) as the baseline, same as the original
 * recommends regardless of SQLCipher.
 */
export function connectWritable(dbPath: string): Database.Database {
  const db = new Database(dbPath);
  db.pragma(`busy_timeout = ${BUSY_TIMEOUT_MS}`);
  return db;
}

export function openReadonly(dbPath: string): Database.Database {
  try {
    const db = new Database(dbPath, { readonly: true, fileMustExist: true });
    db.pragma(`busy_timeout = ${BUSY_TIMEOUT_MS}`);
    return db;
  } catch {
    // Mirrors the Python fallback: if strict read-only open fails (e.g. a
    // pending WAL/journal file), fall back to a normal connection guarded
    // by PRAGMA query_only, which still blocks writes at the SQL level.
    const db = new Database(dbPath, { fileMustExist: true });
    db.pragma("query_only = ON");
    db.pragma(`busy_timeout = ${BUSY_TIMEOUT_MS}`);
    return db;
  }
}

// ---------------------------------------------------------------------------
// Metric bounds + validation
// ---------------------------------------------------------------------------

export const METRIC_BOUNDS: Record<string, [number, number, string]> = {
  steps: [0, 200_000, "steps"],
  sleep_hours: [0, 24, "sleep_hours"],
  resting_heart_rate: [20, 250, "resting_heart_rate (bpm)"],
  weight_kg: [1, 500, "weight_kg"],
  workout_minutes: [0, 1440, "workout_minutes"],
  mood: [1, 10, "mood (expected on a 1-10 scale)"],
  water_ml: [0, 10_000, "water_ml"],
  heart_rate: [20, 250, "heart_rate (bpm)"],
  hrv_ms: [0, 300, "hrv_ms (ms)"],
};

export function validateMetrics(metrics: Record<string, number | null | undefined>): void {
  for (const [name, value] of Object.entries(metrics)) {
    if (value === null || value === undefined || !(name in METRIC_BOUNDS)) continue;
    const [low, high, label] = METRIC_BOUNDS[name];
    if (!(value >= low && value <= high)) {
      throw new Error(`${label} must be between ${low} and ${high}, got ${value}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Date / range helpers
// ---------------------------------------------------------------------------

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Parse a YYYY-MM-DD string, throwing a clear, client-facing error otherwise. */
export function parseDate(value: string, fieldName: string): Date {
  if (!ISO_DATE_RE.test(value)) {
    throw new Error(`${fieldName} must be formatted YYYY-MM-DD, got '${value}'`);
  }
  const d = new Date(value + "T00:00:00Z");
  if (Number.isNaN(d.getTime())) {
    throw new Error(`${fieldName} must be formatted YYYY-MM-DD, got '${value}'`);
  }
  return d;
}

export function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function addDays(d: Date, days: number): Date {
  const copy = new Date(d.getTime());
  copy.setUTCDate(copy.getUTCDate() + days);
  return copy;
}

export function daysBetween(a: Date, b: Date): number {
  return Math.round((b.getTime() - a.getTime()) / 86_400_000);
}

export function today(): Date {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
}

/** Fill in sensible defaults for an open-ended date range and validate it. */
export function resolveRange(
  startDate: string | undefined | null,
  endDate: string | undefined | null,
  defaultDays: number
): [Date, Date] {
  const end = endDate ? parseDate(endDate, "end_date") : today();
  const start = startDate ? parseDate(startDate, "start_date") : addDays(end, -defaultDays);
  if (start.getTime() > end.getTime()) {
    throw new Error(`start_date (${isoDate(start)}) is after end_date (${isoDate(end)})`);
  }
  const span = daysBetween(start, end);
  if (span > MAX_RANGE_DAYS) {
    throw new Error(
      `Requested range is ${span} days, which is over the ${MAX_RANGE_DAYS}-day limit. ` +
        "Narrow start_date/end_date and try again."
    );
  }
  return [start, end];
}

export function numericStats(
  rows: Record<string, unknown>[],
  key: string
): { avg: number | null; min: number | null; max: number | null } {
  const values = rows
    .map((r) => r[key])
    .filter((v): v is number => v !== null && v !== undefined) as number[];
  if (values.length === 0) return { avg: null, min: null, max: null };
  const avg = values.reduce((a, b) => a + b, 0) / values.length;
  return { avg: Math.round(avg * 10) / 10, min: Math.min(...values), max: Math.max(...values) };
}

// ---------------------------------------------------------------------------
// daily_metrics upsert
// ---------------------------------------------------------------------------

/**
 * Upsert one or more daily_metrics rows by date. A column a row doesn't
 * include is left untouched for that date rather than cleared.
 */
export function upsertMetrics(db: Database.Database, rows: Record<string, unknown>[]): void {
  if (rows.length === 0) return;
  const groups = new Map<string, Record<string, unknown>[]>();
  for (const row of rows) {
    if (!("date" in row)) throw new Error("Each row passed to upsertMetrics must include 'date'.");
    const key = Object.keys(row).sort().join(",");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(row);
  }

  const run = db.transaction(() => {
    for (const [columnsKey, groupRows] of groups) {
      const columns = columnsKey.split(",");
      const insertCols = columns.join(", ");
      const placeholders = columns.map((c) => `@${c}`).join(", ");
      const updateClause = columns
        .filter((c) => c !== "date")
        .map((c) => `${c} = excluded.${c}`)
        .join(", ");
      const sql =
        `INSERT INTO daily_metrics (${insertCols}) VALUES (${placeholders}) ` +
        (updateClause ? `ON CONFLICT(date) DO UPDATE SET ${updateClause}` : "ON CONFLICT(date) DO NOTHING");
      const stmt = db.prepare(sql);
      for (const row of groupRows) stmt.run(row as Record<string, unknown>);
    }
  });
  run();
}

// ---------------------------------------------------------------------------
// Raw measurements
// ---------------------------------------------------------------------------

export interface MeasurementInput {
  timestamp: string;
  metric: string;
  value: number;
  unit?: string | null;
  source?: string | null;
  source_type?: string | null;
  importer?: string | null;
  imported_at?: string | null;
}

export function insertMeasurement(db: Database.Database, m: MeasurementInput): number {
  const stmt = db.prepare(`
    INSERT INTO measurements (timestamp, metric, value, unit, source, source_type, importer, imported_at)
    VALUES (@timestamp, @metric, @value, @unit, @source, @source_type, @importer, @imported_at)
  `);
  const info = stmt.run({
    timestamp: m.timestamp,
    metric: m.metric,
    value: m.value,
    unit: m.unit ?? null,
    source: m.source ?? null,
    source_type: m.source_type ?? null,
    importer: m.importer ?? null,
    imported_at: m.imported_at ?? null,
  });
  return Number(info.lastInsertRowid);
}

export interface QueryMeasurementsFilter {
  metric?: string | null;
  start?: string | null;
  end?: string | null;
  source?: string | null;
  limit?: number;
}

export function queryMeasurements(db: Database.Database, filter: QueryMeasurementsFilter): Record<string, unknown>[] {
  const clauses: string[] = [];
  const params: Record<string, unknown> = { limit: filter.limit ?? 1000 };
  if (filter.metric != null) {
    clauses.push("metric = @metric");
    params.metric = filter.metric;
  }
  if (filter.start != null) {
    clauses.push("timestamp >= @start");
    params.start = filter.start;
  }
  if (filter.end != null) {
    clauses.push("timestamp <= @end");
    params.end = filter.end;
  }
  if (filter.source != null) {
    clauses.push("source = @source");
    params.source = filter.source;
  }
  const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
  return db
    .prepare(
      `SELECT id, timestamp, metric, value, unit, source, source_type, importer, imported_at, created_at
       FROM measurements ${where} ORDER BY timestamp DESC LIMIT @limit`
    )
    .all(params) as Record<string, unknown>[];
}

/** Which daily_metrics column each measurement metric rolls up into, and how same-day values combine. */
export const MEASUREMENT_AGGREGATION: Record<string, "sum" | "avg" | "last"> = {
  steps: "sum",
  sleep_hours: "sum",
  resting_heart_rate: "avg",
  weight_kg: "last",
  workout_minutes: "sum",
  mood: "avg",
  water_ml: "sum",
  heart_rate: "avg",
  hrv_ms: "avg",
};

// How far apart two sources' same-day averages for a metric can be before
// this is called a conflict rather than ordinary reading-to-reading noise.
const CONFLICT_TOLERANCE_PCT = 0.05;

export function getMetricProvenance(db: Database.Database, metric: string, day: string) {
  const rows = db
    .prepare(
      `SELECT source, value, timestamp FROM measurements
       WHERE metric = @metric AND timestamp >= @start AND timestamp < @end`
    )
    .all({ metric, start: day, end: day + "T24:00:00" }) as { source: string | null; value: number; timestamp: string }[];

  const bySource = new Map<string | null, { value: number; timestamp: string }[]>();
  for (const row of rows) {
    if (!bySource.has(row.source)) bySource.set(row.source, []);
    bySource.get(row.source)!.push({ value: row.value, timestamp: row.timestamp });
  }

  const sources = [...bySource.entries()]
    .map(([source, readings]) => ({
      source,
      value: Math.round((readings.reduce((a, r) => a + r.value, 0) / readings.length) * 100) / 100,
      n: readings.length,
      latest_timestamp: readings.reduce((a, b) => (a.timestamp > b.timestamp ? a : b)).timestamp,
    }))
    .sort((a, b) => b.n - a.n);

  let conflict = false;
  const distinctValues = sources.filter((s) => s.source !== null).map((s) => s.value);
  if (distinctValues.length > 1) {
    const lo = Math.min(...distinctValues);
    const hi = Math.max(...distinctValues);
    conflict = lo === 0 || (hi - lo) / lo > CONFLICT_TOLERANCE_PCT;
  }

  return { metric, date: day, sources, conflict };
}

/**
 * Given measurement rows for a single metric/day, decide which to keep
 * when more than one source is present. With sourcePriority, keeps only
 * rows from the highest-priority source actually present. Otherwise
 * falls back to whichever source has the most recent imported_at (or
 * created_at). Returns [keptRows, conflict].
 */
export function resolveSourceConflicts(
  rows: Record<string, unknown>[],
  sourcePriority?: string[] | null
): [Record<string, unknown>[], boolean] {
  const sources = new Set(rows.map((r) => (r.source as string | null) ?? null));
  if (sources.size <= 1) return [rows, false];

  if (sourcePriority) {
    for (const preferred of sourcePriority) {
      if (sources.has(preferred)) {
        return [rows.filter((r) => r.source === preferred), true];
      }
    }
  }

  const recencyKey = (row: Record<string, unknown>) =>
    (row.imported_at as string) || (row.created_at as string) || "";
  const newest = rows.reduce((a, b) => (recencyKey(a) >= recencyKey(b) ? a : b));
  const newestSource = (newest.source as string | null) ?? null;
  return [rows.filter((r) => ((r.source as string | null) ?? null) === newestSource), true];
}

export function aggregateMeasurementsToDaily(
  db: Database.Database,
  day: string,
  sourcePriority?: string[] | null
): Record<string, unknown> {
  const rows = db
    .prepare(
      `SELECT metric, value, timestamp, source, imported_at, created_at FROM measurements
       WHERE timestamp >= @start AND timestamp < @end`
    )
    .all({ start: day, end: day + "T24:00:00" }) as Record<string, unknown>[];

  const byMetric = new Map<string, Record<string, unknown>[]>();
  for (const row of rows) {
    const metric = row.metric as string;
    if (!byMetric.has(metric)) byMetric.set(metric, []);
    byMetric.get(metric)!.push(row);
  }

  const result: Record<string, unknown> = { date: day };
  for (const [metric, metricRows] of byMetric) {
    const how = MEASUREMENT_AGGREGATION[metric];
    if (!how) continue;
    const [keptRows] = resolveSourceConflicts(metricRows, sourcePriority);
    const values = keptRows.map((r) => ({ value: r.value as number, timestamp: r.timestamp as string }));
    if (how === "sum") {
      result[metric] = values.reduce((a, v) => a + v.value, 0);
    } else if (how === "avg") {
      result[metric] = Math.round((values.reduce((a, v) => a + v.value, 0) / values.length) * 10) / 10;
    } else if (how === "last") {
      result[metric] = values.reduce((a, b) => (a.timestamp > b.timestamp ? a : b)).value;
    }
  }
  return result;
}
