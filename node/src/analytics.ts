export interface Point {
  day: Date; // UTC midnight
  value: number;
}

function values(series: Point[]): number[] {
  return series.map((p) => p.value);
}

function mean(xs: number[]): number {
  return xs.reduce((a, b) => a + b, 0) / xs.length;
}

function median(xs: number[]): number {
  const sorted = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 !== 0 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function pstdev(xs: number[]): number {
  const m = mean(xs);
  return Math.sqrt(mean(xs.map((x) => (x - m) ** 2)));
}

/** Population Pearson correlation, matching Python's statistics.correlation. */
function correlation(xs: number[], ys: number[]): number {
  const n = xs.length;
  const mx = mean(xs);
  const my = mean(ys);
  let num = 0,
    dx2 = 0,
    dy2 = 0;
  for (let i = 0; i < n; i++) {
    const dx = xs[i] - mx;
    const dy = ys[i] - my;
    num += dx * dy;
    dx2 += dx * dx;
    dy2 += dy * dy;
  }
  if (dx2 === 0 || dy2 === 0) throw new Error("No variance in one of the two series.");
  return num / Math.sqrt(dx2 * dy2);
}

export interface BaselineStats {
  mean: number | null;
  median: number | null;
  stdev: number | null;
  n: number;
}

/** Mean, median, population stdev, and sample size for a series. */
export function baseline(series: Point[]): BaselineStats {
  const vals = values(series);
  const n = vals.length;
  if (n === 0) return { mean: null, median: null, stdev: null, n: 0 };
  return {
    mean: Math.round(mean(vals) * 100) / 100,
    median: Math.round(median(vals) * 100) / 100,
    stdev: n > 1 ? Math.round(pstdev(vals) * 100) / 100 : 0.0,
    n,
  };
}

export interface Anomaly {
  date: string;
  value: number;
  modified_z_score: number;
  direction: "above" | "below";
}

/**
 * Flag points that deviate sharply from the series' own baseline using a
 * modified z-score built on median + MAD (median absolute deviation).
 * threshold=3.5 is Iglewicz & Hoaglin's standard cutoff.
 */
export function detectAnomalies(series: Point[], threshold = 3.5): Anomaly[] {
  if (series.length < 5) return [];
  const vals = values(series);
  const med = median(vals);
  const absDevs = vals.map((v) => Math.abs(v - med));
  const mad = median(absDevs);
  if (mad === 0) return [];
  const anomalies: Anomaly[] = [];
  series.forEach((point, i) => {
    const modifiedZ = (0.6745 * absDevs[i]) / mad;
    if (modifiedZ >= threshold) {
      anomalies.push({
        date: point.day.toISOString().slice(0, 10),
        value: point.value,
        modified_z_score: Math.round(modifiedZ * 100) / 100,
        direction: point.value > med ? "above" : "below",
      });
    }
  });
  return anomalies;
}

export interface TrendStats {
  direction: "insufficient_data" | "flat" | "increasing" | "decreasing";
  slope_per_day: number | null;
  r_squared: number | null;
  n: number;
}

/** Direction and slope of an OLS fit against day index (0, 1, 2, ...). */
export function calculateTrend(series: Point[]): TrendStats {
  const n = series.length;
  if (n < 3) return { direction: "insufficient_data", slope_per_day: null, r_squared: null, n };
  const xs = Array.from({ length: n }, (_, i) => i);
  const ys = values(series);
  const xMean = mean(xs);
  const yMean = mean(ys);
  let ssXy = 0,
    ssXx = 0;
  for (let i = 0; i < n; i++) {
    ssXy += (xs[i] - xMean) * (ys[i] - yMean);
    ssXx += (xs[i] - xMean) ** 2;
  }
  if (ssXx === 0) return { direction: "flat", slope_per_day: 0.0, r_squared: 0.0, n };
  const slope = ssXy / ssXx;
  const intercept = yMean - slope * xMean;
  const ssTot = ys.reduce((a, y) => a + (y - yMean) ** 2, 0);
  const ssRes = xs.reduce((a, x, i) => a + (ys[i] - (slope * x + intercept)) ** 2, 0);
  const rSquared = ssTot === 0 ? 1.0 : 1 - ssRes / ssTot;
  const direction = Math.abs(slope) < 1e-9 ? "flat" : slope > 0 ? "increasing" : "decreasing";
  return {
    direction,
    slope_per_day: Math.round(slope * 10000) / 10000,
    r_squared: Math.round(rSquared * 1000) / 1000,
    n,
  };
}

export interface ComparePeriodsResult {
  period_a: BaselineStats;
  period_b: BaselineStats;
  delta: number | null;
  pct_change: number | null;
}

/** Compare two (typically non-overlapping) periods of the same metric. */
export function comparePeriods(seriesA: Point[], seriesB: Point[]): ComparePeriodsResult {
  const baseA = baseline(seriesA);
  const baseB = baseline(seriesB);
  if (baseA.mean === null || baseB.mean === null) {
    return { period_a: baseA, period_b: baseB, delta: null, pct_change: null };
  }
  const delta = Math.round((baseA.mean - baseB.mean) * 100) / 100;
  const pctChange = baseB.mean !== 0 ? Math.round((delta / baseB.mean) * 100 * 10) / 10 : null;
  return { period_a: baseA, period_b: baseB, delta, pct_change: pctChange };
}

export interface CorrelationResult {
  r: number | null;
  n: number;
  lag_days: number;
  note?: string;
}

/**
 * Pearson correlation between two metrics' series, joined by date.
 * lag_days > 0 shifts series_b backward before joining.
 */
export function findCorrelations(seriesA: Point[], seriesB: Point[], lagDays = 0): CorrelationResult {
  const shiftedB = new Map<number, number>();
  for (const p of seriesB) {
    shiftedB.set(Math.floor(p.day.getTime() / 86_400_000) - lagDays, p.value);
  }
  const pairs: [number, number][] = [];
  for (const p of seriesA) {
    const ord = Math.floor(p.day.getTime() / 86_400_000);
    if (shiftedB.has(ord)) pairs.push([p.value, shiftedB.get(ord)!]);
  }
  const n = pairs.length;
  if (n < 4) {
    return { r: null, n, lag_days: lagDays, note: "Not enough overlapping days to compute a correlation." };
  }
  const xs = pairs.map((p) => p[0]);
  const ys = pairs.map((p) => p[1]);
  try {
    const r = correlation(xs, ys);
    return { r: Math.round(r * 1000) / 1000, n, lag_days: lagDays };
  } catch {
    return { r: null, n, lag_days: lagDays, note: "No variance in one of the two series." };
  }
}
