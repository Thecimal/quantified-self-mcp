#!/usr/bin/env node
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import {
  connectWritable,
  defaultDataDir,
  ensureSchema,
  insertMeasurement,
  upsertMetrics,
  validateMetrics,
} from "./logic.js";
import { parseCsvFile, resolveHeader } from "./csv.js";
import { ADAPTERS, RowError, detectAdapter } from "./import-adapters.js";

// Base directory for the default data/ location: the current working
// directory the command is run from, not the script's own install
// location (which would resolve inside node_modules for an npm install —
// see logic.defaultDataDir's docstring for the full rationale).
const BASE_DIR = process.cwd();
const DATA_DIR = process.env.HEALTH_DB_PATH ? dirname(process.env.HEALTH_DB_PATH) : defaultDataDir(BASE_DIR);
const DEFAULT_DB_PATH = process.env.HEALTH_DB_PATH ?? resolve(DATA_DIR, "health.db");

const CORE_METRIC_COLUMNS = ["steps", "sleep_hours", "resting_heart_rate"];
const OPTIONAL_COLUMNS = ["weight_kg", "workout_minutes", "mood", "water_ml", "heart_rate", "hrv_ms"];
const ALL_METRIC_COLUMNS = [...CORE_METRIC_COLUMNS, ...OPTIONAL_COLUMNS];

function die(message: string): never {
  console.error(`Error: ${message}`);
  process.exit(1);
}

function normalizeDate(raw: string): string {
  const s = raw.trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    if (Number.isNaN(new Date(s + "T00:00:00Z").getTime())) {
      throw new RowError(`unrecognized date '${raw}' (use YYYY-MM-DD or MM/DD/YYYY)`);
    }
    return s;
  }
  const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(s);
  if (m) {
    const [, mm, dd, yyyy] = m;
    const iso = `${yyyy}-${mm.padStart(2, "0")}-${dd.padStart(2, "0")}`;
    if (Number.isNaN(new Date(iso + "T00:00:00Z").getTime())) {
      throw new RowError(`unrecognized date '${raw}' (use YYYY-MM-DD or MM/DD/YYYY)`);
    }
    return iso;
  }
  throw new RowError(`unrecognized date '${raw}' (use YYYY-MM-DD or MM/DD/YYYY)`);
}

function cleanNumber(raw: string): string {
  return raw.trim().replace(/[$,]/g, "");
}

function toInt(raw: string): number | null {
  const cleaned = cleanNumber(raw);
  if (!cleaned) return null;
  const n = Number(cleaned);
  if (!Number.isFinite(n)) throw new RowError(`expected a number, got '${raw}'`);
  return Math.trunc(n);
}

function toFloat(raw: string): number | null {
  const cleaned = cleanNumber(raw);
  if (!cleaned) return null;
  const n = Number(cleaned);
  if (!Number.isFinite(n)) throw new RowError(`expected a number, got '${raw}'`);
  return n;
}

const METRIC_PARSERS: Record<string, (raw: string) => number | null> = {
  steps: toInt,
  sleep_hours: toFloat,
  resting_heart_rate: toInt,
  weight_kg: toFloat,
  workout_minutes: toInt,
  mood: toInt,
  water_ml: toInt,
  heart_rate: toInt,
  hrv_ms: toFloat,
};

function loadCsvRows(
  csvPath: string,
  columnMap: Record<string, string>
): { parsedRows: Record<string, unknown>[]; presentColumns: string[]; skipped: number } {
  const text = readFileSync(csvPath, "utf-8").replace(/^\uFEFF/, "");
  const { header, rows } = parseCsvFile(text);
  if (header.length === 0) die(`${csvPath} appears to be empty.`);

  const dateCol = resolveHeader(header, "date", columnMap);
  if (!dateCol) {
    if (columnMap.date) {
      die(`--map date=${columnMap.date} but ${csvPath} has no such column. Found columns: ${header.join(", ")}`);
    }
    die(`${csvPath} is missing required column: date (use --map date=<your column name> if it's named differently). Found columns: ${header.join(", ")}`);
  }
  for (const [canonical, mapped] of Object.entries(columnMap)) {
    if (canonical !== "date" && !header.includes(mapped)) {
      die(`--map ${canonical}=${mapped} but ${csvPath} has no such column. Found columns: ${header.join(", ")}`);
    }
  }

  const presentColumns = ALL_METRIC_COLUMNS.filter((c) => resolveHeader(header, c, columnMap) !== null);
  const colMap: Record<string, string> = { date: dateCol };
  for (const c of presentColumns) colMap[c] = resolveHeader(header, c, columnMap)!;

  const parsedRows: Record<string, unknown>[] = [];
  let skipped = 0;
  rows.forEach((raw, i) => {
    const lineNo = i + 2; // +2: header is line 1
    try {
      const dateVal = (raw[colMap.date] ?? "").trim();
      if (!dateVal) throw new RowError("missing date");
      const parsed: Record<string, unknown> = { date: normalizeDate(dateVal) };
      for (const col of presentColumns) {
        parsed[col] = METRIC_PARSERS[col]((raw[colMap[col]] ?? "").trim());
      }
      try {
        validateMetrics(Object.fromEntries(Object.entries(parsed).filter(([k]) => k !== "date")) as Record<string, number | null>);
      } catch (exc) {
        throw new RowError((exc as Error).message);
      }
      parsedRows.push(parsed);
    } catch (exc) {
      if (exc instanceof RowError) {
        console.error(`Skipping ${csvPath} line ${lineNo}: ${exc.message}`);
        skipped++;
      } else {
        throw exc;
      }
    }
  });

  return { parsedRows, presentColumns, skipped };
}

async function initHealthDb(
  sourcePath: string,
  dbPath: string,
  replace: boolean,
  source: string,
  columnMap: Record<string, string>
) {
  const adapterName = source === "auto" ? detectAdapter(sourcePath) : source;
  let parsedRows: Record<string, unknown>[] = [];
  let presentColumns: string[] = [];
  let skipped = 0;
  let rawMeasurements: { timestamp: string; metric: string; value: number; unit?: string | null; source?: string | null }[] = [];

  if (adapterName === "csv") {
    const result = loadCsvRows(sourcePath, columnMap);
    parsedRows = result.parsedRows;
    presentColumns = result.presentColumns;
    skipped = result.skipped;
  } else {
    if (!(adapterName in ADAPTERS)) {
      die(`unknown import source '${adapterName}'. Available: csv, ${Object.keys(ADAPTERS).join(", ")}`);
    }
    const adapted = await ADAPTERS[adapterName](sourcePath);
    skipped = adapted.skipped;
    for (const row of adapted.rows) {
      try {
        validateMetrics(Object.fromEntries(Object.entries(row).filter(([k]) => k !== "date")) as Record<string, number | null>);
        parsedRows.push(row);
      } catch (exc) {
        console.error(`Skipping ${sourcePath} date ${row.date}: ${(exc as Error).message}`);
        skipped++;
      }
    }
    presentColumns = adapted.present_columns;
    rawMeasurements = adapted.raw_measurements;
  }

  const db = connectWritable(dbPath);
  try {
    ensureSchema(db);
    if (replace) db.exec("DELETE FROM daily_metrics");
    upsertMetrics(db, parsedRows);
    if (rawMeasurements.length > 0) {
      const importedAt = new Date().toISOString().slice(0, 19);
      for (const m of rawMeasurements) {
        insertMeasurement(db, {
          timestamp: m.timestamp,
          metric: m.metric,
          value: m.value,
          unit: m.unit,
          source: m.source,
          source_type: "wearable",
          importer: adapterName,
          imported_at: importedAt,
        });
      }
    }
  } finally {
    db.close();
  }

  if (presentColumns.length) console.log(`Loaded columns: ${presentColumns.join(", ")}`);
  if (rawMeasurements.length) console.log(`Loaded ${rawMeasurements.length} raw measurement(s) with source provenance.`);
  console.log(`Health DB ready at ${dbPath}: ${parsedRows.length} row(s) loaded, ${skipped} skipped. (source: ${adapterName})`);
}

function parseArgs(argv: string[]) {
  const args = { sourcePath: "", source: "auto", replace: false, dbPath: DEFAULT_DB_PATH, columnMap: {} as Record<string, string> };
  const positional: string[] = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--source") args.source = argv[++i];
    else if (a === "--replace") args.replace = true;
    else if (a === "--db-path") args.dbPath = argv[++i];
    else if (a === "--map") {
      const item = argv[++i];
      const eq = item.indexOf("=");
      if (eq === -1) die(`--map expects COLUMN=HEADER, got '${item}'`);
      const canonical = item.slice(0, eq).trim().toLowerCase();
      const header = item.slice(eq + 1).trim();
      if (canonical !== "date" && !ALL_METRIC_COLUMNS.includes(canonical)) {
        die(`--map column '${canonical}' is not recognized. Valid: date, ${ALL_METRIC_COLUMNS.join(", ")}`);
      }
      if (!header) die(`--map '${item}' has an empty header`);
      args.columnMap[canonical] = header;
    } else if (a === "--help" || a === "-h") {
      printUsage();
      process.exit(0);
    } else positional.push(a);
  }
  if (positional.length === 0) {
    printUsage();
    process.exit(1);
  }
  args.sourcePath = positional[0];
  return args;
}

function printUsage() {
  console.error(
    "Usage: quantified-self-init-db <source-file> [--source auto|csv|apple-health|health-connect] " +
      "[--map COLUMN=HEADER]... [--replace] [--db-path PATH]"
  );
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!existsSync(args.sourcePath)) die(`source file not found at ${args.sourcePath}`);
  mkdirSync(dirname(args.dbPath), { recursive: true });
  await initHealthDb(args.sourcePath, args.dbPath, args.replace, args.source, args.columnMap);
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
