#!/usr/bin/env node
import { McpServer, ResourceTemplate } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve, join } from "node:path";
import Database from "better-sqlite3";

import {
  MAX_ROWS_RETURNED,
  METRIC_BOUNDS,
  MeasurementInput,
  aggregateMeasurementsToDaily,
  connectWritable,
  defaultDataDir,
  ensureSchema,
  getMetricProvenance as logicGetMetricProvenance,
  insertMeasurement,
  numericStats,
  openReadonly,
  parseDate,
  queryMeasurements,
  resolveRange,
  upsertMetrics,
  validateMetrics,
} from "./logic.js";
import { Point, baseline, calculateTrend, comparePeriods, detectAnomalies, findCorrelations } from "./analytics.js";
import {
  ERR_DATABASE_ERROR,
  ERR_DATABASE_LOCKED,
  ERR_INVALID_DATE,
  ERR_INVALID_FIELD,
  ERR_INVALID_METRIC,
  ERR_INVALID_METRIC_VALUE,
  ERR_INVALID_RANGE,
  ERR_INVALID_TIMESTAMP,
  ERR_MISSING_METRIC,
  ToolError,
  isLockedError,
  toolError,
} from "./errors.js";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// Base directory for the default data/ location: the current working
// directory, not the script's own install location — see
// logic.defaultDataDir's docstring for why.
const BASE_DIR = process.cwd();
const HEALTH_DB_PATH = process.env.HEALTH_DB_PATH
  ? resolve(process.env.HEALTH_DB_PATH.replace(/^~/, process.env.HOME ?? "~"))
  : join(defaultDataDir(BASE_DIR), "health.db");

const METRIC_COLUMNS = [
  "steps",
  "sleep_hours",
  "resting_heart_rate",
  "weight_kg",
  "workout_minutes",
  "mood",
  "water_ml",
  "heart_rate",
  "hrv_ms",
] as const;
type MetricColumn = (typeof METRIC_COLUMNS)[number];

function parsePrivateFields(raw: string): Set<string> {
  const names = new Set(
    raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
  );
  const unknown = [...names].filter((n) => !(METRIC_COLUMNS as readonly string[]).includes(n));
  if (unknown.length) {
    console.error(
      `HEALTH_PRIVATE_FIELDS contains unknown field(s) ${JSON.stringify(unknown.sort())}; ignoring. ` +
        `Valid fields: ${METRIC_COLUMNS.join(", ")}`
    );
  }
  return new Set([...names].filter((n) => (METRIC_COLUMNS as readonly string[]).includes(n)));
}

const PRIVATE_FIELDS = parsePrivateFields(process.env.HEALTH_PRIVATE_FIELDS ?? "");

function redactPrivateFields<T extends Record<string, unknown>>(row: T): T {
  if (PRIVATE_FIELDS.size === 0) return row;
  const copy = { ...row };
  for (const key of Object.keys(copy)) {
    if (PRIVATE_FIELDS.has(key)) (copy as Record<string, unknown>)[key] = null;
  }
  return copy;
}

const CLOUD_MODEL_WARNING =
  "Privacy note: this server and its SQLite file are entirely local, but the data returned by " +
  "this tool becomes part of the conversation sent to whatever model the calling client is " +
  "configured with. If that model runs in the cloud rather than on your machine, treat this the " +
  "same as pasting the data into a chat with that provider.";

// ---------------------------------------------------------------------------
// Database bootstrap
// ---------------------------------------------------------------------------

function ensureDb(dbPath: string): void {
  mkdirSync(dirname(dbPath), { recursive: true });
  const db = connectWritable(dbPath);
  try {
    ensureSchema(db);
  } finally {
    db.close();
  }
}

function readonlyConnection<T>(dbPath: string, fn: (db: Database.Database) => T): T {
  ensureDb(dbPath);
  const db = openReadonly(dbPath);
  try {
    return fn(db);
  } finally {
    db.close();
  }
}

function writableConnection<T>(dbPath: string, fn: (db: Database.Database) => T): T {
  const db = connectWritable(dbPath);
  try {
    ensureSchema(db);
    return fn(db);
  } finally {
    db.close();
  }
}

function dbErrorToToolError(exc: unknown, verb: "read" | "write"): ToolError {
  console.error(`Database error trying to ${verb} ${HEALTH_DB_PATH}:`, exc);
  if (isLockedError(exc)) {
    return toolError(
      ERR_DATABASE_LOCKED,
      `Could not ${verb} the health database — it is locked by another process. Try again in a moment.`
    );
  }
  return toolError(
    ERR_DATABASE_ERROR,
    verb === "read"
      ? "Could not read the health database — it may be missing or corrupt. Try again, or re-run the importer."
      : "Could not write to the health database — it may be locked by another process. Try again in a moment."
  );
}

function fetchMetricSeries(metric: string, start: Date, end: Date): Point[] {
  if (!(METRIC_COLUMNS as readonly string[]).includes(metric)) {
    throw toolError(ERR_INVALID_FIELD, `metric must be one of: ${METRIC_COLUMNS.join(", ")} — got '${metric}'`);
  }
  if (PRIVATE_FIELDS.has(metric)) {
    throw toolError(ERR_INVALID_FIELD, `'${metric}' is configured as private (HEALTH_PRIVATE_FIELDS) and can't be analyzed.`);
  }
  try {
    return readonlyConnection(HEALTH_DB_PATH, (db) => {
      const rows = db
        .prepare(
          `SELECT date, ${metric} FROM daily_metrics WHERE date BETWEEN ? AND ? AND ${metric} IS NOT NULL ORDER BY date`
        )
        .all(isoDate(start), isoDate(end)) as Record<string, unknown>[];
      return rows.map((r) => ({ day: parseDate(r.date as string, "date"), value: Number(r[metric]) }));
    });
  } catch (exc) {
    if (exc instanceof ToolError) throw exc;
    throw dbErrorToToolError(exc, "read");
  }
}

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------------------
// Tool result helper: wraps a JS object as MCP text+structured content, and
// converts thrown errors into MCP tool error results — ToolErrors are
// delivered in full, anything unexpected is reduced to a generic message.
// ---------------------------------------------------------------------------

function ok(payload: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }], structuredContent: payload as Record<string, unknown> };
}

function wrap<A extends unknown[]>(fn: (...args: A) => unknown | Promise<unknown>) {
  return async (...args: A) => {
    try {
      const result = await fn(...args);
      return ok(result);
    } catch (exc) {
      if (exc instanceof ToolError) {
        return { content: [{ type: "text" as const, text: exc.message }], isError: true };
      }
      console.error("Unexpected error:", exc);
      return {
        content: [{ type: "text" as const, text: "An unexpected internal error occurred." }],
        isError: true,
      };
    }
  };
}

// ---------------------------------------------------------------------------
// Server + tools
// ---------------------------------------------------------------------------

const server = new McpServer({ name: "Quantified Self", version: "0.3.0" });

// Deliberately a plain string (not a regex-validated schema): letting
// malformed input reach parseDate/resolveRange means bad dates surface as
// our own coded ToolError (invalid_date/invalid_range), matching the
// Python original, rather than a raw framework-level validation error.
const isoDateSchema = z.string();
const metricArg = z.string().describe(`One of ${METRIC_COLUMNS.join(", ")}.`);

function summaryFor(rows: Record<string, unknown>[]) {
  const summary: Record<string, unknown> = { days_with_data: rows.length };
  for (const metric of METRIC_COLUMNS) {
    summary[metric] = PRIVATE_FIELDS.has(metric)
      ? { avg: null, min: null, max: null }
      : numericStats(rows, metric);
  }
  return summary;
}

// --- read_health_data --------------------------------------------------

server.registerTool(
  "read_health_data",
  {
    title: "Read health data",
    description:
      "Daily steps, sleep hours, resting heart rate, weight (kg), workout minutes, mood, water intake (ml), " +
      "heart rate, and HRV from the local health database, plus averages/min/max summaries.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      start_date: isoDateSchema.optional().describe("First day to include. Defaults to 30 days before end_date."),
      end_date: isoDateSchema.optional().describe("Last day to include. Defaults to today."),
    },
  },
  wrap(async ({ start_date, end_date }: { start_date?: string; end_date?: string }) => {
    let start: Date, end: Date;
    try {
      [start, end] = resolveRange(start_date, end_date, 30);
    } catch (exc) {
      throw toolError(ERR_INVALID_RANGE, (exc as Error).message);
    }
    const rows = readonlyConnection(HEALTH_DB_PATH, (db) => {
      try {
        return db
          .prepare(`SELECT date, ${METRIC_COLUMNS.join(", ")} FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date`)
          .all(isoDate(start), isoDate(end)) as Record<string, unknown>[];
      } catch (exc) {
        throw dbErrorToToolError(exc, "read");
      }
    });
    const truncated = rows.length > MAX_ROWS_RETURNED;
    const returnedRows = truncated ? rows.slice(-MAX_ROWS_RETURNED) : rows;
    return {
      range: { start_date: isoDate(start), end_date: isoDate(end) },
      rows: returnedRows.map(redactPrivateFields),
      truncated,
      summary: summaryFor(rows),
    };
  })
);

// --- export_health_data_csv --------------------------------------------

server.registerTool(
  "export_health_data_csv",
  {
    title: "Export health data to CSV",
    description:
      "Write daily health metrics for a date range to a CSV file on disk, next to the database, instead of " +
      "returning every row through this tool's own result.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      start_date: isoDateSchema.optional().describe("First day to include. Defaults to 30 days before end_date."),
      end_date: isoDateSchema.optional().describe("Last day to include. Defaults to today."),
    },
  },
  wrap(async ({ start_date, end_date }: { start_date?: string; end_date?: string }) => {
    let start: Date, end: Date;
    try {
      [start, end] = resolveRange(start_date, end_date, 30);
    } catch (exc) {
      throw toolError(ERR_INVALID_RANGE, (exc as Error).message);
    }
    const rows = readonlyConnection(HEALTH_DB_PATH, (db) => {
      try {
        return db
          .prepare(`SELECT date, ${METRIC_COLUMNS.join(", ")} FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date`)
          .all(isoDate(start), isoDate(end)) as Record<string, unknown>[];
      } catch (exc) {
        throw dbErrorToToolError(exc, "read");
      }
    });

    const exportDir = join(dirname(HEALTH_DB_PATH), "exports");
    mkdirSync(exportDir, { recursive: true });
    const outPath = join(exportDir, `health_export_${isoDate(start)}_to_${isoDate(end)}.csv`);

    const lines = [["date", ...METRIC_COLUMNS].join(",")];
    for (const row of rows) {
      const r = redactPrivateFields(row);
      lines.push([r.date, ...METRIC_COLUMNS.map((c) => (r[c] === null || r[c] === undefined ? "" : String(r[c])))].join(","));
    }
    writeFileSync(outPath, lines.join("\n") + "\n", "utf-8");

    return {
      path: resolve(outPath),
      rows_exported: rows.length,
      range: { start_date: isoDate(start), end_date: isoDate(end) },
    };
  })
);

// --- log_daily_metric ----------------------------------------------------

server.registerTool(
  "log_daily_metric",
  {
    title: "Log a daily metric",
    description:
      "Record one or more health metrics for a single day, creating that day's row if it doesn't already " +
      "have one. Only the metrics you pass are written — anything left unset is not touched. Use clear_metric " +
      "to undo a value logged by mistake.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      date: isoDateSchema.describe("The day to log."),
      steps: z.number().optional().describe("Step count for the day. 0-200,000."),
      sleep_hours: z.number().optional().describe("Hours of sleep. 0-24."),
      resting_heart_rate: z.number().optional().describe("Resting heart rate in bpm. 20-250."),
      weight_kg: z.number().optional().describe("Body weight in kilograms. 1-500."),
      workout_minutes: z.number().optional().describe("Minutes of exercise. 0-1,440."),
      mood: z.number().optional().describe("Mood rating on a 1-10 scale."),
      water_ml: z.number().optional().describe("Water intake in millilitres. 0-10,000."),
      heart_rate: z.number().optional().describe("Non-resting heart rate reading in bpm. 20-250."),
      hrv_ms: z.number().optional().describe("Heart rate variability in milliseconds. 0-300."),
    },
  },
  wrap(async (args: Record<string, unknown>) => {
    let day: Date;
    try {
      day = parseDate(args.date as string, "date");
    } catch (exc) {
      throw toolError(ERR_INVALID_DATE, (exc as Error).message);
    }
    const provided: Record<string, number> = {};
    for (const m of METRIC_COLUMNS) {
      if (args[m] !== undefined && args[m] !== null) provided[m] = args[m] as number;
    }
    if (Object.keys(provided).length === 0) {
      throw toolError(ERR_MISSING_METRIC, "Provide at least one metric to log alongside the date.");
    }
    try {
      validateMetrics(provided);
    } catch (exc) {
      throw toolError(ERR_INVALID_METRIC_VALUE, (exc as Error).message);
    }

    const row = writableConnection(HEALTH_DB_PATH, (db) => {
      try {
        upsertMetrics(db, [{ date: isoDate(day), ...provided }]);
        return db
          .prepare(`SELECT date, ${METRIC_COLUMNS.join(", ")} FROM daily_metrics WHERE date = ?`)
          .get(isoDate(day)) as Record<string, unknown>;
      } catch (exc) {
        throw dbErrorToToolError(exc, "write");
      }
    });

    return { logged: provided, row: redactPrivateFields(row) };
  })
);

// --- clear_metric --------------------------------------------------------

server.registerTool(
  "clear_metric",
  {
    title: "Clear a single metric",
    description:
      "Blank out (set to null) a single metric for a single day, without touching that day's other metrics. " +
      "The counterpart to log_daily_metric for undoing a bad value.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      date: isoDateSchema.describe("The day to clear a field for."),
      field: metricArg.describe("Which metric to blank out."),
    },
  },
  wrap(async ({ date, field }: { date: string; field: string }) => {
    let day: Date;
    try {
      day = parseDate(date, "date");
    } catch (exc) {
      throw toolError(ERR_INVALID_DATE, (exc as Error).message);
    }
    if (!(METRIC_COLUMNS as readonly string[]).includes(field)) {
      throw toolError(ERR_INVALID_FIELD, `field must be one of: ${METRIC_COLUMNS.join(", ")} — got '${field}'`);
    }

    const row = writableConnection(HEALTH_DB_PATH, (db) => {
      try {
        db.prepare(`UPDATE daily_metrics SET ${field} = NULL WHERE date = ?`).run(isoDate(day));
        return db
          .prepare(`SELECT date, ${METRIC_COLUMNS.join(", ")} FROM daily_metrics WHERE date = ?`)
          .get(isoDate(day)) as Record<string, unknown> | undefined;
      } catch (exc) {
        throw dbErrorToToolError(exc, "write");
      }
    });

    if (!row) {
      return { cleared: field, note: `No row exists for ${isoDate(day)} — nothing to clear.` };
    }
    return { cleared: field, row: redactPrivateFields(row) };
  })
);

// --- log_measurement -------------------------------------------------------

server.registerTool(
  "log_measurement",
  {
    title: "Log a raw measurement",
    description:
      "Record a single raw observation — one metric, one value, one point in time — rather than a whole day's " +
      "summary. Use this instead of log_daily_metric when the source, exact time, or multiple readings that " +
      "day matter.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      timestamp: z.string().describe("YYYY-MM-DD or a full ISO 8601 timestamp."),
      metric: z.string().describe('Free-form metric name, e.g. "resting_heart_rate", "steps".'),
      value: z.number().describe("The numeric reading."),
      unit: z.string().optional().describe('Unit, e.g. "bpm", "kg".'),
      source: z.string().optional().describe('Where this came from, e.g. "Apple Watch", "manual".'),
      source_type: z.string().optional().describe('Category of source, e.g. "wearable", "manual", "app".'),
    },
  },
  wrap(
    async ({
      timestamp,
      metric,
      value,
      unit,
      source,
      source_type,
    }: {
      timestamp: string;
      metric: string;
      value: number;
      unit?: string;
      source?: string;
      source_type?: string;
    }) => {
      try {
        parseDate(timestamp.slice(0, 10), "timestamp");
      } catch (exc) {
        throw toolError(ERR_INVALID_TIMESTAMP, (exc as Error).message);
      }
      if (!metric.trim()) throw toolError(ERR_INVALID_METRIC, "metric must be a non-empty string.");

      const row = writableConnection(HEALTH_DB_PATH, (db) => {
        try {
          const input: MeasurementInput = { timestamp, metric, value, unit, source, source_type };
          const newId = insertMeasurement(db, input);
          return db.prepare("SELECT * FROM measurements WHERE id = ?").get(newId) as Record<string, unknown>;
        } catch (exc) {
          throw dbErrorToToolError(exc, "write");
        }
      });
      return { measurement: row };
    }
  )
);

// --- read_measurements -----------------------------------------------------

server.registerTool(
  "read_measurements",
  {
    title: "Read raw measurements",
    description:
      "Read individual measurement rows (not the daily_metrics aggregate), most recent first.\n\n" + CLOUD_MODEL_WARNING,
    inputSchema: {
      metric: z.string().optional().describe("Only return this metric. Omit for all metrics."),
      start_date: z.string().optional().describe("Only return rows on/after this date (YYYY-MM-DD)."),
      end_date: z.string().optional().describe("Only return rows on/before this date (YYYY-MM-DD)."),
      source: z.string().optional().describe("Only return rows from this source."),
      limit: z.number().optional().describe("Maximum rows to return (default 200)."),
    },
  },
  wrap(
    async ({
      metric,
      start_date,
      end_date,
      source,
      limit,
    }: {
      metric?: string;
      start_date?: string;
      end_date?: string;
      source?: string;
      limit?: number;
    }) => {
      const rows = writableConnection(HEALTH_DB_PATH, (db) => {
        try {
          return queryMeasurements(db, { metric, start: start_date, end: end_date, source, limit: limit ?? 200 });
        } catch (exc) {
          throw dbErrorToToolError(exc, "read");
        }
      });
      return { measurements: rows, count: rows.length };
    }
  )
);

// --- aggregate_measurements --------------------------------------------

server.registerTool(
  "aggregate_measurements",
  {
    title: "Aggregate measurements into a day",
    description:
      "Roll up one day's raw measurements into that day's daily_metrics row, so analytics tools (which all " +
      "read daily_metrics) benefit from data logged via log_measurement.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      date: isoDateSchema.describe("The day to aggregate."),
      source_priority: z
        .array(z.string())
        .optional()
        .describe('Ordered source names, e.g. ["Apple Watch", "Garmin"]; first present wins per metric.'),
    },
  },
  wrap(async ({ date, source_priority }: { date: string; source_priority?: string[] }) => {
    let day: Date;
    try {
      day = parseDate(date, "date");
    } catch (exc) {
      throw toolError(ERR_INVALID_DATE, (exc as Error).message);
    }

    const { row, aggregated } = writableConnection(HEALTH_DB_PATH, (db) => {
      try {
        const agg = aggregateMeasurementsToDaily(db, isoDate(day), source_priority);
        const metricsOnly = Object.fromEntries(Object.entries(agg).filter(([k]) => k !== "date"));
        if (Object.keys(metricsOnly).length > 0) upsertMetrics(db, [agg]);
        const r = db
          .prepare(`SELECT date, ${METRIC_COLUMNS.join(", ")} FROM daily_metrics WHERE date = ?`)
          .get(isoDate(day)) as Record<string, unknown> | undefined;
        return { row: r ?? { date: isoDate(day) }, aggregated: metricsOnly };
      } catch (exc) {
        throw dbErrorToToolError(exc, "write");
      }
    });

    return { date: isoDate(day), aggregated, row: redactPrivateFields(row) };
  })
);

// --- get_metric_provenance -----------------------------------------------

server.registerTool(
  "get_metric_provenance",
  {
    title: "Break a metric down by source",
    description:
      "Show one metric's raw measurements for one day, broken down by which source reported them.\n\n" + CLOUD_MODEL_WARNING,
    inputSchema: {
      metric: z.string().describe('Name of the metric to inspect, e.g. "resting_heart_rate".'),
      date: isoDateSchema.describe("The day to inspect."),
    },
  },
  wrap(async ({ metric, date }: { metric: string; date: string }) => {
    let day: Date;
    try {
      day = parseDate(date, "date");
    } catch (exc) {
      throw toolError(ERR_INVALID_DATE, (exc as Error).message);
    }
    return writableConnection(HEALTH_DB_PATH, (db) => {
      try {
        return logicGetMetricProvenance(db, metric, isoDate(day));
      } catch (exc) {
        throw dbErrorToToolError(exc, "read");
      }
    });
  })
);

// --- Layer 2: analytics ---------------------------------------------------

server.registerTool(
  "get_metric_history",
  {
    title: "Get metric history",
    description:
      "Read one metric's day-by-day values, without the other metrics read_health_data always includes.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      metric: metricArg,
      start_date: isoDateSchema.optional().describe("Defaults to 30 days before end_date."),
      end_date: isoDateSchema.optional().describe("Defaults to today."),
    },
  },
  wrap(async ({ metric, start_date, end_date }: { metric: string; start_date?: string; end_date?: string }) => {
    let start: Date, end: Date;
    try {
      [start, end] = resolveRange(start_date, end_date, 30);
    } catch (exc) {
      throw toolError(ERR_INVALID_RANGE, (exc as Error).message);
    }
    const series = fetchMetricSeries(metric, start, end);
    return {
      metric,
      range: { start_date: isoDate(start), end_date: isoDate(end) },
      points: series.map((p) => ({ date: isoDate(p.day), value: p.value })),
    };
  })
);

server.registerTool(
  "get_baseline",
  {
    title: "Get metric baseline",
    description:
      "Compute 'what's normal' for one metric over a window: mean, median, and standard deviation.\n\n" + CLOUD_MODEL_WARNING,
    inputSchema: {
      metric: metricArg,
      start_date: isoDateSchema.optional().describe("Defaults to 90 days before end_date."),
      end_date: isoDateSchema.optional().describe("Defaults to today."),
    },
  },
  wrap(async ({ metric, start_date, end_date }: { metric: string; start_date?: string; end_date?: string }) => {
    let start: Date, end: Date;
    try {
      [start, end] = resolveRange(start_date, end_date, 90);
    } catch (exc) {
      throw toolError(ERR_INVALID_RANGE, (exc as Error).message);
    }
    const series = fetchMetricSeries(metric, start, end);
    return { metric, range: { start_date: isoDate(start), end_date: isoDate(end) }, baseline: baseline(series) };
  })
);

server.registerTool(
  "detect_metric_anomalies",
  {
    title: "Detect metric anomalies",
    description:
      "Flag days where one metric deviated sharply from its own baseline, using a median/MAD-based modified " +
      "z-score.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      metric: metricArg,
      start_date: isoDateSchema.optional().describe("Defaults to 90 days before end_date."),
      end_date: isoDateSchema.optional().describe("Defaults to today."),
      threshold: z.number().optional().describe("Modified z-score cutoff (default 3.5)."),
    },
  },
  wrap(
    async ({
      metric,
      start_date,
      end_date,
      threshold,
    }: {
      metric: string;
      start_date?: string;
      end_date?: string;
      threshold?: number;
    }) => {
      let start: Date, end: Date;
      try {
        [start, end] = resolveRange(start_date, end_date, 90);
      } catch (exc) {
        throw toolError(ERR_INVALID_RANGE, (exc as Error).message);
      }
      const series = fetchMetricSeries(metric, start, end);
      const t = threshold ?? 3.5;
      const anomalies = detectAnomalies(series, t);
      return { metric, range: { start_date: isoDate(start), end_date: isoDate(end) }, threshold: t, anomalies };
    }
  )
);

server.registerTool(
  "calculate_metric_trend",
  {
    title: "Calculate metric trend",
    description:
      "Fit a simple straight-line trend to one metric over a window: direction, slope per day, and r_squared.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      metric: metricArg,
      start_date: isoDateSchema.optional().describe("Defaults to 30 days before end_date."),
      end_date: isoDateSchema.optional().describe("Defaults to today."),
    },
  },
  wrap(async ({ metric, start_date, end_date }: { metric: string; start_date?: string; end_date?: string }) => {
    let start: Date, end: Date;
    try {
      [start, end] = resolveRange(start_date, end_date, 30);
    } catch (exc) {
      throw toolError(ERR_INVALID_RANGE, (exc as Error).message);
    }
    const series = fetchMetricSeries(metric, start, end);
    return { metric, range: { start_date: isoDate(start), end_date: isoDate(end) }, trend: calculateTrend(series) };
  })
);

server.registerTool(
  "compare_metric_periods",
  {
    title: "Compare two periods",
    description:
      "Compare one metric's average between two date ranges — e.g. 'this month vs. last month'.\n\n" + CLOUD_MODEL_WARNING,
    inputSchema: {
      metric: metricArg,
      period_a_start: isoDateSchema,
      period_a_end: isoDateSchema,
      period_b_start: isoDateSchema,
      period_b_end: isoDateSchema,
    },
  },
  wrap(
    async ({
      metric,
      period_a_start,
      period_a_end,
      period_b_start,
      period_b_end,
    }: {
      metric: string;
      period_a_start: string;
      period_a_end: string;
      period_b_start: string;
      period_b_end: string;
    }) => {
      let startA: Date, endA: Date, startB: Date, endB: Date;
      try {
        [startA, endA] = resolveRange(period_a_start, period_a_end, 0);
        [startB, endB] = resolveRange(period_b_start, period_b_end, 0);
      } catch (exc) {
        throw toolError(ERR_INVALID_RANGE, (exc as Error).message);
      }
      const seriesA = fetchMetricSeries(metric, startA, endA);
      const seriesB = fetchMetricSeries(metric, startB, endB);
      const result = comparePeriods(seriesA, seriesB);
      return {
        metric,
        period_a: { start_date: isoDate(startA), end_date: isoDate(endA) },
        period_b: { start_date: isoDate(startB), end_date: isoDate(endB) },
        period_a_stats: result.period_a,
        period_b_stats: result.period_b,
        delta: result.delta,
        pct_change: result.pct_change,
      };
    }
  )
);

server.registerTool(
  "find_metric_correlation",
  {
    title: "Find correlation between two metrics",
    description:
      "Compute the Pearson correlation between two metrics over the same window, joined by date. Correlation, " +
      "not causation.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      metric_a: metricArg,
      metric_b: metricArg,
      start_date: isoDateSchema.optional().describe("Defaults to 90 days before end_date."),
      end_date: isoDateSchema.optional().describe("Defaults to today."),
      lag_days: z.number().optional().describe("Shift metric_b this many days later before joining (default 0)."),
    },
  },
  wrap(
    async ({
      metric_a,
      metric_b,
      start_date,
      end_date,
      lag_days,
    }: {
      metric_a: string;
      metric_b: string;
      start_date?: string;
      end_date?: string;
      lag_days?: number;
    }) => {
      let start: Date, end: Date;
      try {
        [start, end] = resolveRange(start_date, end_date, 90);
      } catch (exc) {
        throw toolError(ERR_INVALID_RANGE, (exc as Error).message);
      }
      const seriesA = fetchMetricSeries(metric_a, start, end);
      const seriesB = fetchMetricSeries(metric_b, start, end);
      const result = findCorrelations(seriesA, seriesB, lag_days ?? 0);
      return { metric_a, metric_b, ...result };
    }
  )
);

// --- Layer 3: personal intelligence ---------------------------------------

server.registerTool(
  "get_recent_changes",
  {
    title: "Get recent changes",
    description:
      "Scan every (non-private) metric for what's changed lately: a recent period vs. the 4x-as-long period " +
      "before it, anomalies in the recent period, and trend.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      days: z.number().optional().describe("Length of the 'recent' window in days (default 7)."),
    },
  },
  wrap(async ({ days }: { days?: number }) => {
    const d = days ?? 7;
    if (d < 2) throw toolError(ERR_INVALID_RANGE, "days must be at least 2.");
    const end = new Date();
    end.setUTCHours(0, 0, 0, 0);
    const recentStart = new Date(end.getTime() - d * 86_400_000);
    const baselineStart = new Date(recentStart.getTime() - d * 4 * 86_400_000);
    const baselineEnd = new Date(recentStart.getTime() - 86_400_000);

    const changes: { metric: string; kind: string; detail: string }[] = [];
    for (const metric of METRIC_COLUMNS) {
      if (PRIVATE_FIELDS.has(metric)) continue;
      const recentSeries = fetchMetricSeries(metric, recentStart, end);
      const baselineSeries = fetchMetricSeries(metric, baselineStart, baselineEnd);

      const comparison = comparePeriods(recentSeries, baselineSeries);
      if (comparison.pct_change !== null && Math.abs(comparison.pct_change) >= 15) {
        const direction = comparison.pct_change > 0 ? "up" : "down";
        changes.push({
          metric,
          kind: "shift",
          detail:
            `${metric} is ${direction} ${Math.abs(comparison.pct_change)}% over the last ${d} days ` +
            `(avg ${comparison.period_a.mean}) vs. the ${d * 4} days before that (avg ${comparison.period_b.mean}).`,
        });
      }

      for (const anomaly of detectAnomalies(recentSeries)) {
        changes.push({
          metric,
          kind: "anomaly",
          detail: `${metric} on ${anomaly.date} was ${anomaly.value} (${anomaly.direction} the recent median, modified z-score ${anomaly.modified_z_score}).`,
        });
      }

      const trend = calculateTrend(recentSeries);
      if (trend.direction !== "flat" && trend.direction !== "insufficient_data" && (trend.r_squared ?? 0) >= 0.3) {
        changes.push({
          metric,
          kind: "trend",
          detail: `${metric} has been ${trend.direction} over the last ${d} days (${trend.slope_per_day! >= 0 ? "+" : ""}${trend.slope_per_day}/day, r²=${trend.r_squared}).`,
        });
      }
    }

    return {
      recent_range: { start_date: isoDate(recentStart), end_date: isoDate(end) },
      baseline_range: { start_date: isoDate(baselineStart), end_date: isoDate(baselineEnd) },
      changes,
    };
  })
);

server.registerTool(
  "explain_metric_change",
  {
    title: "Explain a metric change",
    description:
      "Build an evidence bundle for 'why did my <metric> look like that on <date>?': that day's value against " +
      "a 90-day baseline, whether it's an anomaly, the trend leading into it, and any correlated metric. " +
      "Returns facts, not a generated explanation.\n\n" +
      CLOUD_MODEL_WARNING,
    inputSchema: {
      metric: metricArg,
      date: isoDateSchema,
    },
  },
  wrap(async ({ metric, date }: { metric: string; date: string }) => {
    let targetDay: Date;
    try {
      targetDay = parseDate(date, "date");
    } catch (exc) {
      throw toolError(ERR_INVALID_DATE, (exc as Error).message);
    }

    const baselineStart = new Date(targetDay.getTime() - 90 * 86_400_000);
    const series = fetchMetricSeries(metric, baselineStart, targetDay);
    const stats = baseline(series);
    const targetPoint = series.find((p) => isoDate(p.day) === isoDate(targetDay));
    const value = targetPoint?.value ?? null;

    const anomalies = detectAnomalies(series);
    const matchingAnomaly = anomalies.find((a) => a.date === isoDate(targetDay)) ?? null;

    const trendStart = new Date(targetDay.getTime() - 30 * 86_400_000);
    const trendSeries = fetchMetricSeries(metric, trendStart, targetDay);
    const trend = calculateTrend(trendSeries);

    const correlated: { metric_a: string; metric_b: string; lag_days: number; r: number | null; n: number; note?: string }[] = [];
    for (const other of METRIC_COLUMNS) {
      if (other === metric || PRIVATE_FIELDS.has(other)) continue;
      const otherSeries = fetchMetricSeries(other, baselineStart, targetDay);
      const result = findCorrelations(series, otherSeries, 0);
      if (result.r !== null && Math.abs(result.r) >= 0.5) {
        correlated.push({ metric_a: metric, metric_b: other, ...result });
      }
    }
    correlated.sort((a, b) => Math.abs(b.r ?? 0) - Math.abs(a.r ?? 0));
    const topCorrelated = correlated.slice(0, 5);

    const facts: string[] = [];
    if (value === null) facts.push(`No ${metric} value is logged for ${date}.`);
    else facts.push(`${metric} on ${date} was ${value}.`);
    if (stats.mean !== null) {
      facts.push(`Over the preceding 90 days, ${metric} averaged ${stats.mean} (median ${stats.median}, n=${stats.n}).`);
    }
    if (matchingAnomaly) {
      facts.push(
        `That value is a statistical anomaly: ${matchingAnomaly.direction} the 90-day median (modified z-score ${matchingAnomaly.modified_z_score}).`
      );
    }
    if (trend.direction !== "flat" && trend.direction !== "insufficient_data") {
      facts.push(`${metric} had been ${trend.direction} over the 30 days leading up to ${date} (r²=${trend.r_squared}).`);
    }
    for (const c of topCorrelated) {
      facts.push(`${c.metric_b} correlates with ${metric} over this window (r=${c.r}, n=${c.n}).`);
    }

    return {
      metric,
      date,
      value,
      baseline_range: { start_date: isoDate(baselineStart), end_date: isoDate(targetDay) },
      baseline: stats,
      is_anomaly: matchingAnomaly !== null,
      modified_z_score: matchingAnomaly?.modified_z_score ?? null,
      trend: calculateTrend(trendSeries),
      correlated_metrics: topCorrelated,
      narrative_facts: facts,
    };
  })
);

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

server.registerResource(
  "Metric schema",
  "health://metrics/schema",
  {
    description:
      "The full set of metrics this server tracks, with each one's valid range and whether it's currently " +
      "configured as private.",
    mimeType: "application/json",
  },
  async (uri) => {
    const defs = Object.entries(METRIC_BOUNDS).map(([name, [low, high, label]]) => ({
      name,
      min: low,
      max: high,
      label,
      private: PRIVATE_FIELDS.has(name),
    }));
    return { contents: [{ uri: uri.href, mimeType: "application/json", text: JSON.stringify(defs, null, 2) }] };
  }
);

server.registerResource(
  "Single day snapshot",
  new ResourceTemplate("health://day/{date}", { list: undefined }),
  {
    description:
      "Read-only snapshot of one day's metrics, addressed directly by date instead of a tool call. Equivalent " +
      "to read_health_data with start_date == end_date == date.",
    mimeType: "application/json",
  },
  async (uri, { date }) => {
    const dateStr = Array.isArray(date) ? date[0] : date;
    let day: Date;
    try {
      day = parseDate(dateStr, "date");
    } catch (exc) {
      throw new Error((exc as Error).message);
    }
    const row = readonlyConnection(HEALTH_DB_PATH, (db) =>
      db
        .prepare(`SELECT date, ${METRIC_COLUMNS.join(", ")} FROM daily_metrics WHERE date = ?`)
        .get(isoDate(day)) as Record<string, unknown> | undefined
    );
    const payload = row ? redactPrivateFields(row) : { date: isoDate(day), ...Object.fromEntries(METRIC_COLUMNS.map((m) => [m, null])) };
    return { contents: [{ uri: uri.href, mimeType: "application/json", text: JSON.stringify(payload, null, 2) }] };
  }
);

// ---------------------------------------------------------------------------

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("Fatal error starting quantified-self MCP server:", err);
  process.exit(1);
});
