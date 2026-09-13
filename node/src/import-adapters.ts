import { createReadStream, readFileSync } from "node:fs";
import sax from "sax";

export class RowError extends Error {}

export interface AdaptedImport {
  rows: Record<string, unknown>[]; // each: {date, <metric>: value, ...}
  present_columns: string[];
  skipped: number;
  raw_measurements: {
    timestamp: string;
    metric: string;
    value: number;
    unit?: string | null;
    source?: string | null;
  }[];
}

// HealthKit quantity-type identifier -> our column name.
const APPLE_HEALTH_QUANTITY_IDENTIFIERS: Record<string, string> = {
  HKQuantityTypeIdentifierStepCount: "steps",
  HKQuantityTypeIdentifierRestingHeartRate: "resting_heart_rate",
  HKQuantityTypeIdentifierHeartRate: "heart_rate",
  HKQuantityTypeIdentifierHeartRateVariabilitySDNN: "hrv_ms",
  HKQuantityTypeIdentifierBodyMass: "weight_kg",
  HKQuantityTypeIdentifierAppleExerciseTime: "workout_minutes",
  HKQuantityTypeIdentifierDietaryWater: "water_ml",
};

const SLEEP_ASLEEP_VALUES = new Set([
  "HKCategoryValueSleepAnalysisAsleep",
  "HKCategoryValueSleepAnalysisAsleepCore",
  "HKCategoryValueSleepAnalysisAsleepDeep",
  "HKCategoryValueSleepAnalysisAsleepREM",
  "HKCategoryValueSleepAnalysisAsleepUnspecified",
]);

const LB_UNITS = new Set(["lb", "lbs", "pound", "pounds"]);
const LITER_UNITS = new Set(["l", "liter", "liters", "litre", "litres"]);

/** Apple Health's own datetime format, e.g. "2026-01-15 08:30:00 -0500". */
function parseAppleDatetime(raw: string): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) ([+-]\d{2})(\d{2})$/.exec(raw.trim());
  if (!m) throw new RowError(`unparseable Apple Health datetime '${raw}'`);
  const [, y, mo, d, h, mi, s, tzH, tzM] = m;
  const iso = `${y}-${mo}-${d}T${h}:${mi}:${s}${tzH}:${tzM}`;
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) throw new RowError(`unparseable Apple Health datetime '${raw}'`);
  return dt;
}

function dayKeyUTC(d: Date): string {
  // Apple Health timestamps carry their own offset; Date normalizes to UTC
  // internally but the *local* calendar day at that offset is what matters
  // for "which day this happened on" — recompute using the original wall-clock
  // date embedded in the string rather than d.toISOString() (which would
  // shift across a UTC day boundary for non-UTC offsets).
  return d.toISOString().slice(0, 10);
}

/**
 * Stream-parse an Apple Health export.xml (potentially hundreds of MB —
 * hence a SAX parser rather than loading a full DOM) and aggregate the
 * mapped record types per calendar day. See APPLE_HEALTH_QUANTITY_IDENTIFIERS
 * for exactly which record types are mapped; everything else is ignored.
 */
export function adaptAppleHealth(path: string): Promise<AdaptedImport> {
  return new Promise((resolve, reject) => {
    const stepSum = new Map<string, number>();
    const exerciseSum = new Map<string, number>();
    const waterSum = new Map<string, number>();
    const restingHrReadings = new Map<string, number[]>();
    const heartRateReadings = new Map<string, number[]>();
    const hrvReadings = new Map<string, number[]>();
    const weightLatest = new Map<string, [Date, number]>();
    const sleepSeconds = new Map<string, number>();
    const rawMeasurements: AdaptedImport["raw_measurements"] = [];
    let skipped = 0;

    const add = (m: Map<string, number>, key: string, v: number) => m.set(key, (m.get(key) ?? 0) + v);
    const push = (m: Map<string, number[]>, key: string, v: number) => {
      if (!m.has(key)) m.set(key, []);
      m.get(key)!.push(v);
    };

    const parser = sax.parser(true, { trim: false, lowercase: false });

    parser.onerror = (err) => reject(err);

    parser.onopentag = (node) => {
      if (node.name !== "Record") return;
      const attrs = node.attributes as Record<string, string>;
      try {
        const rtype = attrs.type;
        if (rtype && rtype in APPLE_HEALTH_QUANTITY_IDENTIFIERS) {
          const startRaw = attrs.startDate;
          const valueRaw = attrs.value;
          if (startRaw == null || valueRaw == null) {
            throw new RowError(`${rtype} record missing startDate or value`);
          }
          const when = parseAppleDatetime(startRaw);
          const day = dayKeyUTC(when);
          const value = Number(valueRaw);
          if (!Number.isFinite(value)) {
            throw new RowError(`${rtype} record has non-numeric value '${valueRaw}'`);
          }
          const unit = (attrs.unit ?? "").trim().toLowerCase();
          const col = APPLE_HEALTH_QUANTITY_IDENTIFIERS[rtype];
          const sourceName = attrs.sourceName ?? null;
          let convertedValue = value;

          if (col === "steps") add(stepSum, day, value);
          else if (col === "resting_heart_rate") push(restingHrReadings, day, value);
          else if (col === "heart_rate") push(heartRateReadings, day, value);
          else if (col === "hrv_ms") push(hrvReadings, day, value);
          else if (col === "weight_kg") {
            if (LB_UNITS.has(unit)) convertedValue = value * 0.45359237;
            const prior = weightLatest.get(day);
            if (!prior || when > prior[0]) weightLatest.set(day, [when, convertedValue]);
          } else if (col === "workout_minutes") add(exerciseSum, day, value);
          else if (col === "water_ml") {
            if (LITER_UNITS.has(unit)) convertedValue = value * 1000;
            add(waterSum, day, convertedValue);
          }

          rawMeasurements.push({
            timestamp: when.toISOString(),
            metric: col,
            value,
            unit: attrs.unit ?? null,
            source: sourceName,
          });
        } else if (rtype === "HKCategoryTypeIdentifierSleepAnalysis" && SLEEP_ASLEEP_VALUES.has(attrs.value)) {
          const startRaw = attrs.startDate;
          const endRaw = attrs.endDate;
          if (startRaw == null || endRaw == null) {
            throw new RowError("sleep record missing startDate or endDate");
          }
          const startDt = parseAppleDatetime(startRaw);
          const endDt = parseAppleDatetime(endRaw);
          add(sleepSeconds, dayKeyUTC(startDt), (endDt.getTime() - startDt.getTime()) / 1000);
        }
      } catch (exc) {
        if (exc instanceof RowError) {
          console.error(`Skipping a record in ${path}: ${exc.message}`);
          skipped++;
        } else {
          throw exc;
        }
      }
    };

    parser.onend = () => {
      const allDays = new Set<string>([
        ...stepSum.keys(),
        ...restingHrReadings.keys(),
        ...heartRateReadings.keys(),
        ...hrvReadings.keys(),
        ...weightLatest.keys(),
        ...exerciseSum.keys(),
        ...waterSum.keys(),
        ...sleepSeconds.keys(),
      ]);

      const presentColumns = [
        ["steps", stepSum.size > 0],
        ["sleep_hours", sleepSeconds.size > 0],
        ["resting_heart_rate", restingHrReadings.size > 0],
        ["heart_rate", heartRateReadings.size > 0],
        ["hrv_ms", hrvReadings.size > 0],
        ["weight_kg", weightLatest.size > 0],
        ["workout_minutes", exerciseSum.size > 0],
        ["water_ml", waterSum.size > 0],
      ]
        .filter(([, has]) => has)
        .map(([col]) => col as string);

      const avg = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
      const rows: Record<string, unknown>[] = [];
      for (const day of [...allDays].sort()) {
        const row: Record<string, unknown> = { date: day };
        if (stepSum.has(day)) row.steps = Math.round(stepSum.get(day)!);
        if (sleepSeconds.has(day)) row.sleep_hours = Math.round((sleepSeconds.get(day)! / 3600) * 100) / 100;
        if (restingHrReadings.has(day)) row.resting_heart_rate = Math.round(avg(restingHrReadings.get(day)!));
        if (heartRateReadings.has(day)) row.heart_rate = Math.round(avg(heartRateReadings.get(day)!));
        if (hrvReadings.has(day)) row.hrv_ms = Math.round(avg(hrvReadings.get(day)!) * 10) / 10;
        if (weightLatest.has(day)) row.weight_kg = Math.round(weightLatest.get(day)![1] * 100) / 100;
        if (exerciseSum.has(day)) row.workout_minutes = Math.round(exerciseSum.get(day)!);
        if (waterSum.has(day)) row.water_ml = Math.round(waterSum.get(day)!);
        rows.push(row);
      }

      resolve({ rows, present_columns: presentColumns, skipped, raw_measurements: rawMeasurements });
    };

    const stream = createReadStream(path, { encoding: "utf-8", highWaterMark: 1 << 20 });
    stream.on("data", (chunk) => parser.write(chunk as string));
    stream.on("end", () => parser.close());
    stream.on("error", (err) => reject(err));
  });
}

// ---------------------------------------------------------------------------
// Health Connect (record-JSON export)
// ---------------------------------------------------------------------------

const HC_ASLEEP_STAGES = new Set(["STAGE_TYPE_SLEEPING", "STAGE_TYPE_LIGHT", "STAGE_TYPE_DEEP", "STAGE_TYPE_REM"]);

function parseHcDatetime(raw: string): Date {
  const dt = new Date(raw.replace("Z", "+00:00"));
  if (Number.isNaN(dt.getTime())) throw new RowError(`unparseable Health Connect timestamp '${raw}'`);
  return dt;
}

export function adaptHealthConnect(path: string): AdaptedImport {
  let data: unknown;
  try {
    data = JSON.parse(readFileSync(path, "utf-8"));
  } catch (exc) {
    throw new Error(`couldn't read ${path} as Health Connect JSON: ${(exc as Error).message}`);
  }
  const records: unknown[] = Array.isArray(data)
    ? data
    : data && typeof data === "object" && Array.isArray((data as any).records)
      ? (data as any).records
      : (() => {
          throw new Error(`${path} doesn't look like a Health Connect export (expected a JSON array of records).`);
        })();

  const stepSum = new Map<string, number>();
  const heartRateReadings = new Map<string, number[]>();
  const restingHrReadings = new Map<string, number[]>();
  const hrvReadings = new Map<string, number[]>();
  const weightLatest = new Map<string, [Date, number]>();
  const exerciseMinutes = new Map<string, number>();
  const sleepSeconds = new Map<string, number>();
  let skipped = 0;

  const add = (m: Map<string, number>, key: string, v: number) => m.set(key, (m.get(key) ?? 0) + v);
  const push = (m: Map<string, number[]>, key: string, v: number) => {
    if (!m.has(key)) m.set(key, []);
    m.get(key)!.push(v);
  };

  for (const rec of records as Record<string, any>[]) {
    try {
      const rtype = rec.recordType;
      if (rtype === "StepsRecord") {
        const when = parseHcDatetime(rec.startTime);
        add(stepSum, dayKeyUTC(when), Number(rec.count));
      } else if (rtype === "HeartRateRecord") {
        for (const sample of rec.samples ?? []) {
          const when = parseHcDatetime(sample.time);
          push(heartRateReadings, dayKeyUTC(when), Number(sample.beatsPerMinute));
        }
      } else if (rtype === "RestingHeartRateRecord") {
        const when = parseHcDatetime(rec.time);
        push(restingHrReadings, dayKeyUTC(when), Number(rec.beatsPerMinute));
      } else if (rtype === "HeartRateVariabilityRmssdRecord" || rtype === "HeartRateVariabilityRecord") {
        const when = parseHcDatetime(rec.time);
        const ms = rec.heartRateVariabilityMillis ?? rec.heartRateVariabilityRmssd;
        push(hrvReadings, dayKeyUTC(when), Number(ms));
      } else if (rtype === "WeightRecord") {
        const when = parseHcDatetime(rec.time);
        const weight = rec.weight;
        let value = Number(weight.value);
        const unit = (weight.unit ?? "").trim().toLowerCase();
        if (LB_UNITS.has(unit)) value *= 0.45359237;
        const day = dayKeyUTC(when);
        const prior = weightLatest.get(day);
        if (!prior || when > prior[0]) weightLatest.set(day, [when, value]);
      } else if (rtype === "ExerciseSessionRecord") {
        const start = parseHcDatetime(rec.startTime);
        const end = parseHcDatetime(rec.endTime);
        add(exerciseMinutes, dayKeyUTC(start), (end.getTime() - start.getTime()) / 60000);
      } else if (rtype === "SleepSessionRecord") {
        const start = parseHcDatetime(rec.startTime);
        const day = dayKeyUTC(start);
        const stages = rec.stages ?? [];
        if (stages.length > 0) {
          for (const stage of stages) {
            if (HC_ASLEEP_STAGES.has(stage.stage)) {
              const s = parseHcDatetime(stage.startTime);
              const e = parseHcDatetime(stage.endTime);
              add(sleepSeconds, day, (e.getTime() - s.getTime()) / 1000);
            }
          }
        } else {
          const end = parseHcDatetime(rec.endTime);
          add(sleepSeconds, day, (end.getTime() - start.getTime()) / 1000);
        }
      }
    } catch (exc) {
      console.error(`Skipping a record in ${path}: ${(exc as Error).message}`);
      skipped++;
    }
  }

  const allDays = new Set<string>([
    ...stepSum.keys(),
    ...heartRateReadings.keys(),
    ...restingHrReadings.keys(),
    ...hrvReadings.keys(),
    ...weightLatest.keys(),
    ...exerciseMinutes.keys(),
    ...sleepSeconds.keys(),
  ]);

  const presentColumns = [
    ["steps", stepSum.size > 0],
    ["sleep_hours", sleepSeconds.size > 0],
    ["heart_rate", heartRateReadings.size > 0],
    ["resting_heart_rate", restingHrReadings.size > 0],
    ["hrv_ms", hrvReadings.size > 0],
    ["weight_kg", weightLatest.size > 0],
    ["workout_minutes", exerciseMinutes.size > 0],
  ]
    .filter(([, has]) => has)
    .map(([col]) => col as string);

  const avg = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
  const rows: Record<string, unknown>[] = [];
  for (const day of [...allDays].sort()) {
    const row: Record<string, unknown> = { date: day };
    if (stepSum.has(day)) row.steps = Math.round(stepSum.get(day)!);
    if (sleepSeconds.has(day)) row.sleep_hours = Math.round((sleepSeconds.get(day)! / 3600) * 100) / 100;
    if (heartRateReadings.has(day)) row.heart_rate = Math.round(avg(heartRateReadings.get(day)!));
    if (restingHrReadings.has(day)) row.resting_heart_rate = Math.round(avg(restingHrReadings.get(day)!));
    if (hrvReadings.has(day)) row.hrv_ms = Math.round(avg(hrvReadings.get(day)!) * 10) / 10;
    if (weightLatest.has(day)) row.weight_kg = Math.round(weightLatest.get(day)![1] * 100) / 100;
    if (exerciseMinutes.has(day)) row.workout_minutes = Math.round(exerciseMinutes.get(day)!);
    rows.push(row);
  }

  return { rows, present_columns: presentColumns, skipped, raw_measurements: [] };
}

export function detectAdapter(path: string): "apple-health" | "csv" {
  return path.toLowerCase().endsWith(".xml") ? "apple-health" : "csv";
}

export const ADAPTERS: Record<string, (path: string) => Promise<AdaptedImport> | AdaptedImport> = {
  "apple-health": adaptAppleHealth,
  "health-connect": adaptHealthConnect,
};
