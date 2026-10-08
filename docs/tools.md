# MCP Tool Reference

Quantified Self MCP exposes **20 tools** and **2 resources** over stdio.

All tools enforce strict input validation, privacy protections (`HEALTH_PRIVATE_FIELDS`), and return structured evidence with provenance ratings.

---

## 1. Daily Health Data

* **`read_health_data(start_date, end_date, metrics=None)`**
  Read day-level health summaries (steps, sleep, resting heart rate, weight, workout minutes, mood, water intake) across a date range.
* **`log_daily_metric(date, steps=None, sleep_hours=None, ...)`**
  Record daily summary metrics for a given day. Upserts measurements and updates the `daily_metrics` projection.
* **`clear_metric(date, field)`**
  Reset a single metric for a given day back to `null`.
* **`export_health_data_csv(start_date, end_date, filename=None)`**
  Export daily metrics across a date range to a CSV file on disk.

## 2. Granular Observations & Measurements

* **`log_measurement(timestamp, metric, value, unit=None, source=None)`**
  Record an individual timestamped observation with provenance and unit validation.
* **`read_measurements(start_date=None, end_date=None, metric=None, source=None, limit=100)`**
  Retrieve raw individual measurement rows before daily projection.
* **`aggregate_measurements(date, source_priority=None)`**
  Preview daily aggregation results using custom source-priority conflict resolution.
* **`get_metric_provenance(date, metric)`**
  Inspect readings for a specific date broken down by device/source to identify discrepancies.

## 3. Workouts & Activity Sessions

* **`log_workout_session(start_time, end_time=None, duration_minutes=None, activity_type=None, ...)`**
  Record a structured exercise session (activity type, duration, heart rate, calories, distance).
* **`read_workout_sessions(start_date=None, end_date=None, activity_type=None, limit=50)`**
  Query individual workout sessions with activity and date filtering.

## 4. Analytics & Longitudinal Intelligence

* **`get_baseline(metric, window_days=30, end_date=None)`**
  Compute summary statistics (mean, median, standard deviation, IQR, normal range) for a metric.
* **`get_metric_history(metric, start_date=None, end_date=None)`**
  Retrieve a single metric's time series over a date range.
* **`calculate_metric_trend(metric, window_days=30, end_date=None)`**
  Fit linear regression to detect directional trends over time, annotated with claim strength.
* **`detect_metric_anomalies(metric, window_days=30, end_date=None, threshold_std=2.0)`**
  Identify dates where metric values deviate significantly from baseline.
* **`compare_metric_periods(metric, period_a_start, period_a_end, period_b_start, period_b_end)`**
  Compare metrics between two time spans (e.g., this week vs. last week).
* **`find_metric_correlation(metric_a, metric_b, window_days=60, end_date=None)`**
  Compute Pearson correlation coefficient between two metrics over matching days.
* **`get_recent_changes(window_days=14, baseline_days=30, end_date=None)`**
  Scan all active metrics to find meaningful deviations from normal baselines.
* **`explain_metric_change(metric, target_date, baseline_days=30)`**
  Assemble a comprehensive causal evidence bundle for why a metric fluctuated on a specific date.

## 5. System Status & Freshness

* **`get_data_status(stale_after_days=7)`**
  Evaluate database freshness, latest recorded dates, total days logged, and metric coverage.
* **`get_import_status(limit=10)`**
  Review recent data imports: source file, importer type, duration, records added, and status.

---

## Resources

* `health://metrics/schema`: JSON schema specifying metric definitions, valid ranges, units, and privacy classifications.
* `health://day/{date}`: Full daily metric snapshot for a given `YYYY-MM-DD`.
