"""
source_coverage.py
==================
Longitudinal source-coverage matrix: for every (importer, source, metric) the database actually holds, how
much history there is, how fresh it is, whether the import that produced it is healthy, where it came from,
and whether the analytics tools can use it. Alongside that, a per-metric view of what the analytics tools
really read (the daily_metrics projection) and a per-domain roll-up (sleep, hrv, ...), including domains the
database has no model for at all, so "what can we answer, and what is missing?" has a measured answer.

Read-only: it only SELECTs from measurements, daily_metrics, imports and workout_sessions. It measures the
data in the database, not what a schema or a tool could hold.

Classification is deterministic and uses the policy constants below, which are a starting policy to tune,
not derived values:
  red     no data, or the latest import for the importer failed or never finished, or a daily metric
          covers fewer than RED_MAX_COVERAGE_RATIO of the days in its own span
  yellow  not red, but stale, covers fewer than GREEN_MIN_COVERAGE_RATIO of its span, spans fewer than
          MIN_HISTORY_DAYS, mixes known statistics under one metric name (e.g. HRV as SDNN and as RMSSD),
          or has days where several unranked sources were resolved by the fallback rule
  green   none of the above
Every reason that applies is listed in `limitations`. Episodic metrics (weight, workouts) are not expected
daily, so their coverage ratio does not drive the classification and they are stale after
EPISODIC_STALE_AFTER_DAYS instead of the caller's stale_after_days.

Framework-free apart from logic/metric_registry, mirroring data_health.py: plain dicts out.
`python -m source_coverage` prints the matrix for the local database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from logic import DEFAULT_STALE_AFTER_DAYS, readonly_connection
from metric_registry import METRICS

GREEN, YELLOW, RED = "green", "yellow", "red"
CLASSIFICATIONS = (RED, YELLOW, GREEN)  # weakest first

MIN_HISTORY_DAYS = 28
GREEN_MIN_COVERAGE_RATIO = 0.9
RED_MAX_COVERAGE_RATIO = 0.5
EPISODIC_STALE_AFTER_DAYS = 30
EPISODIC_METRICS = frozenset({"weight_kg", "workout_minutes"})

UNATTRIBUTED = "(unattributed)"

# What an importer's value for a metric statistically *is*, where the adapters document it
# (import_adapters.py). A metric whose rows carry more than one known statistic mixes meanings under one
# name, which analytics cannot see.
KNOWN_STATISTICS: dict[tuple[str, str], str] = {
    ("apple-health", "hrv_ms"): "sdnn",
    ("health-connect", "hrv_ms"): "rmssd",
}

# domain -> metrics read from daily_metrics. A domain is as strong as its weakest metric.
METRIC_DOMAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sleep", ("sleep_hours",)),
    ("hrv", ("hrv_ms",)),
    ("heart_rate", ("heart_rate", "resting_heart_rate")),
    ("activity", ("steps", "workout_minutes")),
    ("body_measurements", ("weight_kg",)),
)

# Domains the database has no table or metric for: reported as red so the gap is explicit.
UNMODELLED_DOMAINS: dict[str, str] = {
    "readiness": "no readiness or recovery score is stored",
    "training": "no training load, volume or plan is stored (workout_sessions holds sessions only)",
    "routes": "no route or location data is stored",
}

# import status, worst first; only the first two make a source red.
_IMPORT_SEVERITY = ("failed", "interrupted", "no_import_record", "succeeded", "manual")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return row is not None


def _in_clause(excluded: list[str]) -> tuple[str, list[str]]:
    if not excluded:
        return "", []
    return " AND metric NOT IN (" + ", ".join("?" for _ in excluded) + ")", excluded


def _gaps(days: list[date]) -> tuple[int, int]:
    """(gap_count, missing_days) inside the span of an ascending list of distinct days."""
    gap_count = missing = 0
    for previous, current in zip(days, days[1:], strict=False):
        hole = (current - previous).days - 1
        if hole > 0:
            gap_count += 1
            missing += hole
    return gap_count, missing


def _span(days: list[date]) -> dict[str, Any]:
    """Coverage facts shared by every level of the matrix. `days` is ascending and distinct."""
    if not days:
        return {
            "available_from": None,
            "latest_available": None,
            "coverage_days": 0,
            "span_days": 0,
            "gap_count": 0,
            "missing_days": 0,
            "coverage_ratio": None,
        }
    span_days = (days[-1] - days[0]).days + 1
    gap_count, missing = _gaps(days)
    return {
        "available_from": days[0].isoformat(),
        "latest_available": days[-1].isoformat(),
        "coverage_days": len(days),
        "span_days": span_days,
        "gap_count": gap_count,
        "missing_days": missing,
        "coverage_ratio": round(len(days) / span_days, 4),
    }


def _freshness(
    latest: str | None, today: date, stale_after_days: int, metric: str | None = None
) -> tuple[int | None, str | None]:
    """(days_behind, CURRENT|STALE); an episodic metric is judged against EPISODIC_STALE_AFTER_DAYS."""
    if latest is None:
        return None, None
    if metric in EPISODIC_METRICS:
        stale_after_days = EPISODIC_STALE_AFTER_DAYS
    days_behind = max(0, (today - date.fromisoformat(latest)).days)
    return days_behind, ("CURRENT" if days_behind <= stale_after_days else "STALE")


def classify(
    facts: dict[str, Any],
    *,
    episodic: bool,
    import_status: str,
    extra_limitations: Iterable[str] = (),
) -> tuple[str, list[str]]:
    """(classification, limitations) for one entry of the matrix; see the module docstring for the rules."""
    if facts["coverage_days"] == 0:
        return RED, ["no_data"]
    red: list[str] = []
    yellow: list[str] = []
    if import_status == "failed":
        red.append("latest_import_failed")
    elif import_status == "interrupted":
        red.append("latest_import_interrupted")
    ratio = facts["coverage_ratio"]
    if not episodic and ratio is not None:
        if ratio < RED_MAX_COVERAGE_RATIO:
            red.append("incomplete_coverage")
        elif ratio < GREEN_MIN_COVERAGE_RATIO:
            yellow.append("gaps_in_coverage")
    if facts["freshness"] == "STALE":
        yellow.append("stale")
    if facts["span_days"] < MIN_HISTORY_DAYS:
        yellow.append("short_history")
    yellow.extend(code for code in extra_limitations if code not in yellow)
    limitations = red + yellow
    return (RED if red else YELLOW if yellow else GREEN), limitations


def _worst_import_status(statuses: Iterable[str]) -> str:
    found = set(statuses)
    for status in _IMPORT_SEVERITY:
        if status in found:
            return status
    return "no_import_record"


def _weakest(classifications: Iterable[str]) -> str:
    found = set(classifications)
    for level in CLASSIFICATIONS:
        if level in found:
            return level
    return RED


def _resolved_order(item: tuple[tuple[str, str | None], int]) -> tuple[bool, str]:
    source = item[0][1]
    return (source is None, source or "")


def _metric_order(metric: str) -> tuple[int, str]:
    keys = list(METRICS)
    return (keys.index(metric) if metric in METRICS else len(keys), metric)


def compute_source_coverage(
    conn: sqlite3.Connection,
    today: date,
    *,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    exclude_metrics: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the coverage matrix. Metrics in `exclude_metrics` (HEALTH_PRIVATE_FIELDS) are left out of every
    level, and a domain whose metrics are all excluded is omitted rather than reported. Read-only."""
    excluded = sorted(set(exclude_metrics))
    clause, params = _in_clause(excluded)

    # --- source level: (importer, source, metric) from the raw measurements -------------------------------
    source_days: dict[tuple[str | None, str | None, str], list[date]] = {}
    for importer, source, metric, day in conn.execute(
        "SELECT importer, source, metric, date(timestamp) AS day FROM measurements "
        f"WHERE date(timestamp) IS NOT NULL{clause} GROUP BY importer, source, metric, day ORDER BY day",
        params,
    ):
        source_days.setdefault((importer, source, metric), []).append(date.fromisoformat(day))

    provenance: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
    for importer, source, metric, unit, source_type, count, imported_at in conn.execute(
        "SELECT importer, source, metric, unit, source_type, COUNT(*), MAX(imported_at) FROM measurements "
        f"WHERE 1 = 1{clause} GROUP BY importer, source, metric, unit, source_type",
        params,
    ):
        entry = provenance.setdefault(
            (importer, source, metric),
            {"measurement_count": 0, "units": set(), "source_types": set(), "last_imported_at": None},
        )
        entry["measurement_count"] += count
        if unit is not None:
            entry["units"].add(unit)
        if source_type is not None:
            entry["source_types"].add(source_type)
        if imported_at is not None and (entry["last_imported_at"] is None or imported_at > entry["last_imported_at"]):
            entry["last_imported_at"] = imported_at

    # A database from before import history existed has no imports table: every importer's
    # status is then "no_import_record" rather than an error.
    latest_import_status: dict[str, str] = {}
    if _table_exists(conn, "imports"):
        for importer, status in conn.execute(
            "SELECT importer, status FROM imports WHERE id IN (SELECT MAX(id) FROM imports GROUP BY importer)"
        ):
            latest_import_status[importer] = (
                "succeeded" if status == "succeeded" else ("failed" if status == "failed" else "interrupted")
            )

    # --- metric level: the daily_metrics projection analytics actually reads --------------------------------
    metric_days: dict[str, list[date]] = {}
    resolution_days: dict[str, dict[str, int]] = {}
    multi_source_days: dict[str, int] = {}
    resolved_days: dict[tuple[str, str | None], int] = {}
    for metric, day, resolved_source, source_count, resolution in conn.execute(
        "SELECT metric, date, resolved_source, source_count, resolution FROM daily_metrics "
        f"WHERE 1 = 1{clause} ORDER BY date",
        params,
    ):
        metric_days.setdefault(metric, []).append(date.fromisoformat(day))
        counts = resolution_days.setdefault(metric, {"single": 0, "priority": 0, "fallback": 0})
        counts[resolution] += 1
        if source_count > 1:
            multi_source_days[metric] = multi_source_days.get(metric, 0) + 1
        resolved_days[(metric, resolved_source)] = resolved_days.get((metric, resolved_source), 0) + 1

    # statistics known per metric, across every importer that wrote it
    statistics_by_metric: dict[str, set[str]] = {}
    for importer, _source, metric in source_days:
        known = KNOWN_STATISTICS.get((importer or "", metric))
        if known:
            statistics_by_metric.setdefault(metric, set()).add(known)
    mixed = {metric for metric, found in statistics_by_metric.items() if len(found) > 1}

    # rows sharing a (source, metric) label cannot be told apart in daily_metrics.resolved_source
    label_counts: dict[tuple[str | None, str], int] = {}
    for _importer, source, metric in source_days:
        label_counts[(source, metric)] = label_counts.get((source, metric), 0) + 1

    rows: list[dict[str, Any]] = []
    metric_import: dict[str, list[str]] = {}
    for (importer, source, metric), days in source_days.items():
        days = sorted(days)
        facts = _span(days)
        facts["days_behind"], facts["freshness"] = _freshness(
            facts["latest_available"], today, stale_after_days, metric
        )
        if importer is None:
            import_status = "manual"
        else:
            import_status = latest_import_status.get(importer, "no_import_record")
        metric_import.setdefault(metric, []).append(import_status)
        extra = ["mixed_statistics"] if metric in mixed else []
        classification, limitations = classify(
            facts, episodic=metric in EPISODIC_METRICS, import_status=import_status, extra_limitations=extra
        )
        info = provenance[(importer, source, metric)]
        rows.append(
            {
                "source": source,
                "importer": importer,
                "metric": metric,
                **facts,
                "import_status": import_status,
                "provenance": {
                    "measurement_count": info["measurement_count"],
                    "units": sorted(info["units"]),
                    "source_types": sorted(info["source_types"]),
                    "last_imported_at": info["last_imported_at"],
                    "statistic": KNOWN_STATISTICS.get((importer or "", metric)),
                    "days_resolved_to_source": (
                        resolved_days.get((metric, source), 0) if label_counts[(source, metric)] == 1 else None
                    ),
                },
                "analytics_supported": metric in METRICS,
                "classification": classification,
                "limitations": limitations,
            }
        )
    rows.sort(key=lambda r: (_metric_order(r["metric"]), r["importer"] or "", r["source"] or ""))

    # --- one entry per metric (registry metrics always appear, so missing data is visible) -------------
    metrics: list[dict[str, Any]] = []
    names = {m for m in METRICS if m not in excluded} | set(metric_days) | {r["metric"] for r in rows}
    for metric in sorted(names, key=_metric_order):
        days = sorted(set(metric_days.get(metric, [])))
        facts = _span(days)
        facts["days_behind"], facts["freshness"] = _freshness(
            facts["latest_available"], today, stale_after_days, metric
        )
        import_status = _worst_import_status(metric_import.get(metric, []))
        counts = resolution_days.get(metric, {"single": 0, "priority": 0, "fallback": 0})
        extra = []
        if metric in mixed:
            extra.append("mixed_statistics")
        if counts["fallback"]:
            extra.append("unranked_source_resolution")
        classification, limitations = classify(
            facts, episodic=metric in EPISODIC_METRICS, import_status=import_status, extra_limitations=extra
        )
        metrics.append(
            {
                "metric": metric,
                **facts,
                "cadence": "episodic" if metric in EPISODIC_METRICS else "daily",
                "import_status": import_status,
                "source_rows": sum(1 for r in rows if r["metric"] == metric),
                "resolution_days": counts,
                "multi_source_days": multi_source_days.get(metric, 0),
                "resolved_sources": {
                    (source if source is not None else UNATTRIBUTED): n
                    for (m, source), n in sorted(resolved_days.items(), key=_resolved_order)
                    if m == metric
                },
                "statistics": sorted(statistics_by_metric.get(metric, set())),
                "analytics_supported": metric in METRICS,
                "classification": classification,
                "limitations": limitations,
            }
        )
    by_metric = {entry["metric"]: entry for entry in metrics}

    # --- domains ---------------------------------------------------------------------------------------
    domains: list[dict[str, Any]] = []
    for domain, members in METRIC_DOMAINS:
        present = [m for m in members if m in by_metric]
        if not present:
            continue
        entries = [by_metric[m] for m in present]
        domains.append(
            {
                "domain": domain,
                "modelled": True,
                "classification": _weakest(e["classification"] for e in entries),
                "metrics": [
                    {"metric": e["metric"], "classification": e["classification"], "coverage_days": e["coverage_days"]}
                    for e in entries
                ],
                "limitations": sorted({code for e in entries for code in e["limitations"]}),
            }
        )
    if "workout_minutes" not in excluded:
        domains.append(_workouts_domain(conn, today))
    for domain, note in UNMODELLED_DOMAINS.items():
        domains.append(
            {
                "domain": domain,
                "modelled": False,
                "classification": RED,
                "metrics": [],
                "limitations": ["not_modelled"],
                "note": note,
            }
        )

    summary = {level: 0 for level in CLASSIFICATIONS}
    for entry in rows:
        summary[entry["classification"]] += 1
    return {
        "as_of": today.isoformat(),
        "stale_after_days": stale_after_days,
        "thresholds": {
            "min_history_days": MIN_HISTORY_DAYS,
            "green_min_coverage_ratio": GREEN_MIN_COVERAGE_RATIO,
            "red_max_coverage_ratio": RED_MAX_COVERAGE_RATIO,
            "episodic_stale_after_days": EPISODIC_STALE_AFTER_DAYS,
            "episodic_metrics": sorted(EPISODIC_METRICS),
        },
        "rows": rows,
        "metrics": metrics,
        "domains": domains,
        "summary": summary,
    }


def _workouts_domain(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workout_sessions'").fetchone()
    days: list[date] = []
    sessions = 0
    if exists:
        for day, count in conn.execute("SELECT date, COUNT(*) FROM workout_sessions GROUP BY date ORDER BY date"):
            days.append(date.fromisoformat(day))
            sessions += count
    facts = _span(days)
    facts["days_behind"], facts["freshness"] = _freshness(
        facts["latest_available"], today, EPISODIC_STALE_AFTER_DAYS, "workout_minutes"
    )
    classification, limitations = classify(facts, episodic=True, import_status="manual")
    return {
        "domain": "workouts",
        "modelled": True,
        "classification": classification,
        "metrics": [],
        "source_table": "workout_sessions",
        "session_count": sessions,
        **facts,
        "limitations": limitations,
    }


def _format_text(report: dict[str, Any]) -> str:
    marks = {GREEN: "GREEN ", YELLOW: "YELLOW", RED: "RED   "}
    lines = [f"Source coverage as of {report['as_of']} (stale after {report['stale_after_days']} days)", ""]
    lines.append("Domains")
    for d in report["domains"]:
        detail = ", ".join(d["limitations"]) or "-"
        lines.append(f"  {marks[d['classification']]} {d['domain']:<18} {detail}")
    lines += ["", "Metrics (what analytics reads)"]
    for m in report["metrics"]:
        span = f"{m['available_from']}..{m['latest_available']}" if m["available_from"] else "no data"
        lines.append(
            f"  {marks[m['classification']]} {m['metric']:<20} {span:<24} {m['coverage_days']:>5} days, "
            f"{m['gap_count']} gaps, import={m['import_status']}"
            + (f"  [{', '.join(m['limitations'])}]" if m["limitations"] else "")
        )
    lines += ["", "Sources (importer / source / metric)"]
    for r in report["rows"]:
        lines.append(
            f"  {marks[r['classification']]} {r['importer'] or 'manual':<14} {r['source'] or UNATTRIBUTED:<22} "
            f"{r['metric']:<20} {r['available_from']}..{r['latest_available']} {r['coverage_days']:>5} days"
            + (f"  [{', '.join(r['limitations'])}]" if r["limitations"] else "")
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m source_coverage",
        description="Print the longitudinal source-coverage matrix for the local health database (read-only).",
    )
    parser.add_argument("--db", type=Path, default=None, help="database path (default: HEALTH_DB_PATH, else usual)")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--stale-after-days", type=int, default=DEFAULT_STALE_AFTER_DAYS)
    args = parser.parse_args(argv)
    if args.stale_after_days < 0:
        parser.error("--stale-after-days must be 0 or more")

    from init_db import DEFAULT_DB_PATH
    from privacy import PRIVATE_FIELDS

    db_path = args.db or DEFAULT_DB_PATH
    if not Path(db_path).exists():
        print(f"No database at {db_path}.", file=sys.stderr)
        return 1
    try:
        with readonly_connection(db_path) as conn:
            report = compute_source_coverage(
                conn, date.today(), stale_after_days=args.stale_after_days, exclude_metrics=PRIVATE_FIELDS
            )
    except sqlite3.Error as exc:
        print(
            f"Could not read {db_path}: {exc}. If this database was created by an older version, start the "
            "server once so it migrates the schema; this report never modifies the database.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, indent=2) if args.json else _format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
