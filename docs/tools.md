# MCP Tool Reference

Quantified Self MCP exposes **20 tools** and **2 resources** over stdio.

All tools enforce strict input validation and privacy protections (`HEALTH_PRIVATE_FIELDS`). The analytics tools
return structured evidence (sample size, coverage, `data_health`) and, where they draw a conclusion, a `claim`
whose decision limits how strongly it may be stated.

---

## 1. Daily Health Data

* **`read_health_data(start_date=None, end_date=None)`**
  Read day-level summaries of all nine metrics (steps, sleep, resting heart rate, weight, workout minutes, mood, water
  intake, heart rate, HRV) across a date range (default: the 30 days up to today). A metric listed in
  `HEALTH_PRIVATE_FIELDS` is always `null`.
* **`log_daily_metric(date, steps=None, sleep_hours=None, resting_heart_rate=None, weight_kg=None, workout_minutes=None, mood=None, water_ml=None, heart_rate=None, hrv_ms=None)`**
  Record daily summary metrics for a given day. Upserts measurements and updates the `daily_metrics` projection.
* **`clear_metric(date, field)`**
  Reset a single metric for a given day back to `null`.
* **`export_health_data_csv(start_date=None, end_date=None)`**
  Export daily metrics across a date range to a CSV file on disk (the filename is derived from the range). The tool
  returns the file's path and a row count, not the data.

## 2. Granular Observations & Measurements

* **`log_measurement(timestamp, metric, value, unit=None, source=None, source_type=None)`**
  Record an individual timestamped observation with provenance and unit validation.
* **`read_measurements(metric=None, start_date=None, end_date=None, source=None, limit=200)`**
  Retrieve raw individual measurement rows before daily projection.
* **`aggregate_measurements(date, source_priority=None)`**
  Preview daily aggregation results using custom source-priority conflict resolution.
* **`get_metric_provenance(metric, date)`**
  Inspect readings for a specific date broken down by device/source to identify discrepancies.

## 3. Workouts & Activity Sessions

* **`log_workout_session(date, activity_type, duration_minutes, start_time=None, intensity=None, avg_heart_rate=None, max_heart_rate=None, source=None, notes=None)`**
  Record a structured exercise session (activity type, duration, timing, intensity, average and peak heart rate, notes).
* **`read_workout_sessions(start_date=None, end_date=None, activity_type=None, limit=200)`**
  Query individual workout sessions with activity and date filtering.

## 4. Analytics & Longitudinal Intelligence

* **`get_baseline(metric, start_date=None, end_date=None)`**
  Compute the mean, median, standard deviation and sample size for a metric over a window (default: the 90 days up
  to `end_date`), annotated with claim strength.
* **`get_metric_history(metric, start_date=None, end_date=None)`**
  Retrieve a single metric's time series over a date range.
* **`calculate_metric_trend(metric, start_date=None, end_date=None)`**
  Fit a straight-line trend over a window (default: the 30 days up to `end_date`) and report its direction, slope per
  day and r², annotated with claim strength.
* **`detect_metric_anomalies(metric, start_date=None, end_date=None, threshold=3.5)`**
  Flag days whose modified z-score (median and MAD) exceeds `threshold`, over a window (default: the 90 days up to
  `end_date`).
* **`compare_metric_periods(metric, period_a_start, period_a_end, period_b_start, period_b_end)`**
  Compare metrics between two time spans (e.g., this week vs. last week).
* **`find_metric_correlation(metric_a, metric_b, start_date=None, end_date=None, lag_days=0)`**
  Pearson correlation between two metrics' daily values over matching days (default window: the 90 days up to
  `end_date`; `lag_days=1` pairs `metric_a` today with `metric_b` tomorrow). It is an association only, never
  evidence that one metric affects the other. Read `claim.decision` (the tier and `must_state`) before reporting `r`.
* **`get_recent_changes(days=7)`**
  Scan every non-private metric for what changed lately: the last `days` days against the 4x-as-long period before
  them, with any anomalies and the trend inside the recent window.
* **`explain_metric_change(metric, date)`**
  Assemble the facts around a metric's value on one day: it against its 90-day baseline, whether it is an anomaly,
  the trend leading into it, and strongly correlated metrics. It returns facts for the caller to interpret, not a
  causal explanation.

## 5. System Status & Freshness

* **`get_data_status(stale_after_days=2)`**
  Evaluate database freshness, latest recorded dates, total days logged, and metric coverage.
* **`get_import_status(limit=5)`**
  Review recent data imports: source file, importer type, duration, records added, and status.

---

## Resources

* `health://metrics/schema`: each metric's name, valid range and label, and whether it is configured as private.
* `health://day/{date}`: Full daily metric snapshot for a given `YYYY-MM-DD`.
