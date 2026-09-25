"""
analytics.py
============
Framework-free statistical functions computed over a single health metric's
time series: baselines, anomaly detection, trend, period comparison, and
cross-metric correlation.

Deliberately dependency-free (standard library only, mirroring logic.py) so
these are unit-testable without fastmcp and reusable directly from
server.py's Layer-2/Layer-3 MCP tools. None of this touches the database —
every function takes an already-fetched (date, value) series. That keeps
this module with zero opinions about SQLite, connections, or which metrics
exist; server.py stays responsible for reading rows (via
logic.readonly_connection) and for enforcing HEALTH_PRIVATE_FIELDS before
anything here ever sees a value.
"""

from __future__ import annotations

import statistics
from datetime import date
from typing import NamedTuple


class Point(NamedTuple):
    day: date
    value: float


def _values(series: list[Point]) -> list[float]:
    return [p.value for p in series]


def baseline(series: list[Point]) -> dict[str, float | None]:
    """Mean, median, population stdev, and sample size for a series.

    Returns Nones (n=0) for an empty series rather than raising — "no data
    logged yet for this metric" is an expected, tool-reportable case for a
    Layer-3 caller, not an exceptional one.
    """
    values = _values(series)
    n = len(values)
    if n == 0:
        return {"mean": None, "median": None, "stdev": None, "n": 0}
    return {
        "mean": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "stdev": round(statistics.pstdev(values), 2) if n > 1 else 0.0,
        "n": n,
    }


def detect_anomalies(series: list[Point], threshold: float = 3.5) -> list[dict]:
    """Flag points that deviate sharply from the series' own baseline,
    using a modified z-score built on median + MAD (median absolute
    deviation) rather than mean/stdev.

    MAD is robust to the very outliers it's trying to detect, and to the
    small, noisy samples typical of personal health data (30-90 days),
    where a couple of bad nights would otherwise drag the mean/stdev
    enough to mask themselves. threshold=3.5 is Iglewicz & Hoaglin's
    standard cutoff for "this point doesn't belong."
    """
    if len(series) < 5:
        return []  # not enough history to call anything an "anomaly"
    values = _values(series)
    med = statistics.median(values)
    abs_devs = [abs(v - med) for v in values]
    mad = statistics.median(abs_devs)
    if mad == 0:
        return []  # a flat series has no meaningful spread to deviate from
    anomalies = []
    for point, dev in zip(series, abs_devs, strict=True):
        modified_z = 0.6745 * dev / mad
        if modified_z >= threshold:
            anomalies.append(
                {
                    "date": point.day.isoformat(),
                    "value": point.value,
                    "modified_z_score": round(modified_z, 2),
                    "direction": "above" if point.value > med else "below",
                }
            )
    return anomalies


def calculate_trend(series: list[Point]) -> dict:
    """Direction and slope of an ordinary-least-squares fit against actual
    calendar time (days elapsed since the first observation), not position
    in the list.

    A missing day is a gap, not a step of equal size to a logged one — a
    reading on Jan 1 followed by the next on Jan 20 is 19 days apart, not
    "the next point." Fitting against position instead of calendar time
    would silently compress that 19-day gap to a single step, distorting
    slope_per_day without the caller ever finding out. Points do not need
    to be evenly spaced or gap-free for this to be correct: OLS against
    x = elapsed_days handles irregular spacing natively. Coverage/gap
    reporting (evidence.py) is a separate, complementary signal about how
    much of the window is backed by data — it should never be doing the
    job of fixing a distorted time axis here.
    """
    n = len(series)
    if n < 3:
        return {
            "direction": "insufficient_data",
            "slope_per_day": None,
            "r_squared": None,
            "n": n,
            "span_days": None,
        }
    origin = series[0].day
    xs = [(p.day - origin).days for p in series]
    ys = _values(series)
    span_days = xs[-1] - xs[0]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    ss_xx = sum((x - x_mean) ** 2 for x in xs)
    if ss_xx == 0:
        # Every point falls on the same calendar day (e.g. duplicate-day
        # rows) — there's no time axis to regress against.
        return {"direction": "flat", "slope_per_day": 0.0, "r_squared": 0.0, "n": n, "span_days": span_days}
    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True))
    r_squared = 1.0 if ss_tot == 0 else 1 - ss_res / ss_tot
    direction = "flat" if abs(slope) < 1e-9 else ("increasing" if slope > 0 else "decreasing")
    return {
        "direction": direction,
        "slope_per_day": round(slope, 4),
        "r_squared": round(r_squared, 3),
        "n": n,
        "span_days": span_days,
    }


def compare_periods(series_a: list[Point], series_b: list[Point]) -> dict:
    """Compare two (typically non-overlapping) periods of the same metric.

    series_a is treated as the "current"/later period, series_b as the
    baseline it's measured against (e.g. this week vs. the prior 4 weeks).
    """
    base_a, base_b = baseline(series_a), baseline(series_b)
    if base_a["mean"] is None or base_b["mean"] is None:
        return {"period_a": base_a, "period_b": base_b, "delta": None, "pct_change": None}
    delta = round(base_a["mean"] - base_b["mean"], 2)
    pct_change = round((delta / base_b["mean"]) * 100, 1) if base_b["mean"] != 0 else None
    return {"period_a": base_a, "period_b": base_b, "delta": delta, "pct_change": pct_change}


def _sample_confidence(n: int) -> str:
    """Bucket an overlap/sample count into a coarse adequacy label for
    correlation results — product-level guardrails, not a universal
    statistical threshold. Deliberately silent about the correlation's
    magnitude or significance; this only says how much paired data backed
    it. See find_correlations.
    """
    if n < 4:
        return "insufficient"
    if n < 10:
        return "very_limited"
    if n < 20:
        return "limited"
    if n < 30:
        return "moderate"
    return "strong_sample"


def find_correlations(series_a: list[Point], series_b: list[Point], lag_days: int = 0) -> dict:
    """Pearson correlation between two metrics' series, joined by date.

    lag_days > 0 shifts series_b backward before joining — lag_days=1 tests
    whether series_a on day N predicts series_b on day N+1 (e.g. "does poor
    sleep tonight predict lower steps tomorrow?"). Only dates present in
    both series after the shift are used.

    Every return path includes "sample_confidence" (see _sample_confidence)
    — a bucketed read on the overlap count "n" alone, not on the strength
    of "r" — so a caller can't mistake a high r from a thin sample for a
    settled relationship.
    """
    shifted_b = {p.day.toordinal() - lag_days: p.value for p in series_b}
    pairs = [(p.value, shifted_b[p.day.toordinal()]) for p in series_a if p.day.toordinal() in shifted_b]
    n = len(pairs)
    if n < 4:
        return {
            "r": None,
            "n": n,
            "lag_days": lag_days,
            "note": "Not enough overlapping days to compute a correlation.",
            "sample_confidence": _sample_confidence(n),
        }
    xs, ys = zip(*pairs, strict=True)
    try:
        r = statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return {
            "r": None,
            "n": n,
            "lag_days": lag_days,
            "note": "No variance in one of the two series.",
            "sample_confidence": _sample_confidence(n),
        }
    return {"r": round(r, 3), "n": n, "lag_days": lag_days, "sample_confidence": _sample_confidence(n)}
