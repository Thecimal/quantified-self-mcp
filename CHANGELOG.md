# Changelog

All notable changes to this project are documented here.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions correspond to the [PyPI release history](https://pypi.org/project/quantified-self-mcp/#history).

## [Unreleased]

### Added

- **Incremental imports with an audit trail.** Re-importing a source no longer deletes and rewrites
  everything: each (metric, day) is compared with what the importer already stored, unchanged days are left
  exactly as they were (ids and `imported_at` keep recording when they were first imported), and only new
  or changed days are written. Each `imports` row now records `records_seen`, `records_added`,
  `records_updated`, `records_unchanged` and `records_removed` (days dropped by `--replace`), plus the
  dataset coverage before and after, and `get_import_status` returns them. `quantified-self-init-db` prints
  the same counts. Schema migration 9 adds the columns to databases that already have the table.
- **Import history and data freshness.** Every `quantified-self-init-db` run now
  records an `imports` row (importer, source file name and SHA-256, status
  `running` / `succeeded` / `failed`, rows loaded and skipped, measurements
  written, error). Two read-only MCP tools expose it: `get_data_status` reports
  whether the database is `CURRENT`, `STALE`, `INCOMPLETE`, `NO_DATA` or
  `IMPORT_FAILED` (latest data date, coverage, gaps, last successful import,
  suggested action), and `get_import_status` lists recent import runs.
  Metrics in `HEALTH_PRIVATE_FIELDS` are ignored when computing coverage.
- **`data_health` on every analytics tool.** A single
  data-quality state for the window behind the answer: `VALID`,
  `VALID_WITH_GAPS`, `INSUFFICIENT_DATA`, `STALE` or `IMPORT_INCOMPLETE`, with
  every applicable reason, the latest observation and its age, the covered
  period, gaps, observation count and the last import. It combines the window's
  existing evidence with the import history and dataset status, and is
  separate from `claim` (which is unchanged). Results that rest on several
  windows (period comparisons, correlations, recent changes, explanations)
  report the weakest window and list every window's reasons.
- **Agent-loop eval (`eval/agent`, Phase 1).** A real model, the real MCP
  server and real tool results in a multi-turn loop, scored per layer (routing,
  arguments, execution) with replayable traces, deterministic fixtures defined
  relative to today, and a tool coverage matrix built from the live tool list
  (`python -m eval.agent --coverage --strict`). The harness itself is covered
  by offline tests that need no API key. Interpretation and final-answer
  scoring are not built yet and are reported as not scored.

### Changed

- **The evidence registry now describes what is actually evaluated** — `trend`,
  `window_comparison`, `correlation` and `anomaly` list only the dimensions that
  have evaluators (sample and temporal, plus missingness for the first three),
  like `baseline`. Previously they also listed robustness, practical,
  provenance and measurement_validity, which no evaluator produced, so every
  claim resolved those as `not_assessed` and could never exceed `suggestive`.
  Nothing is tolerated to make that go away: those four analyses now carry an
  explicit `max_tier: suggestive` ceiling whose factor, `practical_not_evaluated`,
  is added to `claim.decision.must_state` (a statistically detectable but trivial
  effect cannot be called `supported` while practical significance is unchecked).
  `claim.profile.dimensions` for those analyses no longer contains the
  `not_assessed` robustness/practical/provenance entries, and
  `robustness_not_assessed`, `practical_not_assessed` and
  `provenance_not_assessed` no longer appear in `must_state`. A parity test now
  fails if a registry dimension has no evaluator or an evaluator's output is not
  in the registry.

### Fixed

- **Apple Health raw observations are now stored in the metric's canonical unit
  and validated per reading.** `daily_metrics` is projected from `measurements`,
  but the Apple Health adapter wrote raw readings in the device's own unit under
  the canonical metric name: a 154 lb weight became `weight_kg = 154` and
  `water_ml` was summed in litres. It also never bounds-checked raw readings, and
  a day reported as "Skipping date ..." for an out-of-bounds total was imported
  anyway. Pound and litre readings are now converted before storage (the stored
  `unit` is `kg` / `ml`), readings in other weight or volume units (stones, fluid
  ounces) are skipped and counted instead of being read as kg / ml, out-of-bounds
  readings are skipped, and a skipped day imports none of its raw rows.
- **NaN / infinity no longer crash an import or reach an aggregate.** Apple
  Health, Health Connect and CSV imports now skip and report non-finite values
  like any other bad record (previously `int(round(nan))` / `int(inf)` aborted
  the whole import with a traceback). A rejected Health Connect reading also no
  longer leaves an empty per-day bucket behind.

## [0.4.0] - 2026-09-29

### Added

- **Baseline evidence claim** — `get_baseline` now runs through the same
  pipeline as the other analyses (registry policy -> `assess_baseline` ->
  `EvidenceProfile` -> `ClaimDecision`) and returns it in a new canonical
  `claim` envelope (`evidence` / `profile` / `decision`). The `baseline`
  registry entry lists only dimensions that have evaluators (sample,
  temporal, missingness) and reuses the anomaly baseline-length policy
  (`min_baseline_days: 28`). The top-level `evidence` field is deprecated in
  favor of `claim.evidence` and stays identical for one release.
- **Multi-metric `coverage` on `read_health_data`** — per-metric
  coverage percentages plus an overall `coverage_percent`/`confidence`
  for the whole date range, computed by the new
  `evidence.build_coverage_summary`, so a broad "what's my health
  data look like" read carries the same evidence signal the
  single-metric analytics tools already did.
- **`evidence` on every `get_recent_changes` change note** — each
  shift/anomaly/trend entry now carries its own coverage over the
  window it was computed from, closing the one analytics tool that
  previously reported findings with no coverage signal at all.
- Tool docstrings for `get_baseline`, `detect_metric_anomalies`,
  `calculate_metric_trend`, `compare_metric_periods`,
  `find_metric_correlation`, `get_recent_changes`, and
  `explain_metric_change` now explicitly instruct the calling model to
  fold `confidence`/`coverage_ratio` into how it phrases a result,
  rather than just returning the numbers alongside it.
- **`workout_sessions` table (schema v6)** — one row per workout instead
  of a single daily `workout_minutes` total: `activity_type`,
  `start_time`, `duration_minutes`, `intensity` (low/moderate/high),
  `avg_heart_rate`, `max_heart_rate`, `source`, `notes`. New
  `log_workout_session` / `read_workout_sessions` tools, and
  `explain_metric_change` now attaches the day's sessions (and a
  narrative fact per session) when explaining `workout_minutes`.

### Changed

- **`daily_metrics` no longer blends devices** (schema v8). The projection used
  to aggregate every measurement for a (metric, day), so two devices recording
  the same walk summed to 19,500 steps from 10,000 + 9,500, two sleep trackers
  to 14.5 h, and a manual `log_daily_metric` total or a CSV day total stacked
  on top of an Apple Health import. It now aggregates one source's observations
  per (metric, day): the highest-ranked present source in the new
  `source_priority` table (manual `daily-log` entries rank first by default);
  with none ranked, the source that observed the most distinct hours of the
  day, then the one with the latest observation, then source name. New
  `daily_metrics` columns `resolved_source`, `source_count` and `resolution`
  (`single` / `priority` / `fallback`) record how each value was chosen.
  Existing databases are migrated and rebuilt on next start, so historical
  multi-source days change to the single-source value. `db.invariant
  .set_source_priority()` edits the lists and re-projects in the same
  transaction; `verify()` flags a projection left stale by a direct edit.
  The `aggregate_measurements` preview's default fallback now matches the
  projection instead of using `imported_at`.
- An expression index on `measurements (metric, date(timestamp), source)`
  makes the projection's per-day lookups indexed: repairing 120k measurements
  drops from ~22 s to ~0.1 s.

### Removed

- **BREAKING: deprecated flat claim fields removed from the output schemas** —
  `evidence_profile` and `claim_decision` are gone from `detect_metric_anomalies`,
  `calculate_metric_trend`, `compare_metric_periods`, `find_metric_correlation`,
  every `get_recent_changes` note and every `explain_metric_change` correlated
  metric, and `explain_metric_change` also drops `trend_evidence_profile` and
  `trend_claim_decision`. They were exact copies of `claim.profile` /
  `claim.decision` (`headline_claim.*` / `trend_claim.*` on
  `explain_metric_change`), so nothing is lost: read the canonical `claim`
  envelope instead. Clients that read the flat fields must switch before
  upgrading. The separate deprecations of `evidence`, `evidence_a` /
  `evidence_b` and `period_a_evidence` / `period_b_evidence` are unchanged.

### Fixed

- **Claims now reflect the limits of the statistic actually reported** —
  anomaly detection returns `[]` when MAD is zero (a flat series with one
  extreme value, or a majority of identical readings), which previously read
  as "nothing unusual happened"; the anomaly sample dimension now blocks with
  `baseline_mad_zero`. A correlation over a constant series (r undefined) now
  blocks with `zero_variance`. `calculate_trend` on points that all share one
  calendar day reports `insufficient_data` instead of `flat`.
  `explain_metric_change` now assesses the 90-day baseline it reports
  (`baseline_claim`) and includes it in `overall_decision`.
- **Apple Health import day-shift bug** — `adapt_apple_health`'s
  `raw_measurements` kept each record's original UTC offset (e.g.
  `"...T23:30:00-08:00"`), but `db/aggregation.py` buckets `daily_metrics`
  with plain SQLite `date(timestamp)`, which normalizes an offset-aware
  timestamp to UTC before taking the date. A record from the evening or
  night in any non-UTC timezone was silently stored under the *next* UTC
  calendar day — disagreeing with both the adapter's own `rows`/`--report`
  output and with what the source device showed, and feeding every
  downstream analytics/evidence tool the wrong day. `raw_measurements`
  timestamps are now stored as naive local wall-clock time (tzinfo
  stripped), matching the convention every other timestamp in
  `measurements` already follows.
- **Explicit `destructiveHint` on every tool** — ten tools omitted it, so
  MCP clients fell back to the spec default (`true`). All 18 tools now
  set all four annotation hints explicitly.
- **`export_health_data_csv` annotations** — it writes a CSV to disk, so
  it is now `readOnlyHint=False` (was `True`) and `idempotentHint=True`
  (was `False`; the filename is deterministic per date range, so repeat
  calls rewrite the same file rather than adding new ones). It also
  overwrites an existing export for the same range, so it is now
  `destructiveHint=True`.
- **`log_daily_metric` is `destructiveHint=True`** — it replaces the
  previously logged value for that metric and day (the old daily-log
  measurement row is deleted and a new one inserted), so it is an
  overwrite, not an additive write. Still idempotent.
- **Exact annotation regression test** — `test_tool_annotations_complete`
  compares the serialized `list_tools()` annotations of all 18 tools
  against an explicit expected matrix, so a missing hint, a wrong value,
  or an added/removed tool fails CI.

## [0.3.0] - 2026-09-13

### Added
- **Provenance columns on `measurements` (schema v4)** — `importer` (which
  import path wrote the row, e.g. `"apple-health"`) and `imported_at`
  (when that import ran), alongside the existing `source`/`source_type`.
- **Apple Health import now records raw, source-tagged measurements** —
  each `sourceName` (e.g. "Ben's Apple Watch") is preserved per
  observation in `measurements`, not just blended into the daily
  aggregate. CSV import doesn't have a per-record source, so it still
  only writes `daily_metrics` as before.
- **`get_metric_provenance` tool** — breaks a metric's readings for a
  day down by source and flags a `conflict` when two sources disagree
  by more than a small tolerance, instead of silently averaging
  different devices together.
- **`resolve_source_conflicts` / `source_priority`** — `aggregate_measurements`
  now takes an optional ordered source list so a multi-source day
  aggregates from one chosen device rather than blending readings from
  different sources; without a priority, falls back to whichever source
  was imported most recently.
- **`measurements` table (schema v3)** — a raw, source-level layer under
  `daily_metrics`: one row per observation (`timestamp`, `metric`,
  `value`, `unit`, `source`, `source_type`), instead of one row per day.
  Lets a day hold several readings of the same metric (multiple
  workouts, repeated wearable samples) with enough context to explain a
  value, not just report it.
- **`log_measurement` / `read_measurements` / `aggregate_measurements`
  tools** — record a raw observation, read raw rows back with
  metric/date/source filters, and roll a day's raw measurements into its
  `daily_metrics` row (`logic.MEASUREMENT_AGGREGATION` decides sum vs.
  average vs. latest per metric) so existing analytics tools pick them
  up unchanged.

### Fixed
- **Every tool's "Privacy note" cloud-model warning was silently never
  reaching any real MCP client.** FastMCP derives a tool's client-facing
  `description` from its docstring via `inspect.getdoc()` + Griffe, which
  (a) dedents the docstring before parsing, so the warning's own 4-space
  indentation (copied from the source file's indentation) meant it could
  never substring-match a plain, undedented comparison string, and (b)
  only keeps the *leading* text block before `Args:` — a paragraph placed
  after `Returns:`, as every tool here had it, is parsed but then
  dropped, never making it into `description` at all. The existing test
  (`test_every_tool_carries_the_cloud_model_warning`) didn't catch this
  because it checked each function's raw Python `__doc__` rather than
  what `list_tools()` actually returns over the protocol — those two
  things are not the same, and diverged silently. Fixed by moving the
  warning to right after each tool's opening summary (before `Args:`)
  and rewriting `CLOUD_MODEL_WARNING` without the indentation that
  never survives dedenting. The test itself now asserts against
  `mcp.list_tools()`'s real `description` field, and discovers tools
  automatically instead of checking a hand-maintained tuple that had
  already silently missed several tools added after it was written.
  Verified against an actual `fastmcp.Client` round-trip, not just the
  server-side object, before and after.

### Added
- New `export_health_data_csv` tool: writes a date range of health
  metrics to a CSV file on disk (next to the database) and returns only
  the file's path and a row count, rather than the row values
  themselves — so exporting a long history doesn't have to pass through
  a cloud LLM's context. Respects `HEALTH_PRIVATE_FIELDS` the same way
  `read_health_data` does.
- Layer 2/3 analytics tools: `get_metric_history`, `get_baseline`,
  `detect_metric_anomalies`, `calculate_metric_trend`,
  `compare_metric_periods`, `find_metric_correlation`, `get_recent_changes`,
  and `explain_metric_change` — see README.md's "MCP Tools" section. The
  underlying statistics (baseline, MAD-based anomaly detection, linear
  trend, period comparison, lagged Pearson correlation) live in the new,
  framework-free `analytics.py`, mirroring how logic.py separates
  MCP-independent logic from server.py's tool wiring. Every new tool
  refuses a metric listed in `HEALTH_PRIVATE_FIELDS` outright, the same
  guardrail `read_health_data` already applies, rather than merely
  redacting a value after computing something from it. `analytics.py` is
  now listed in `[tool.hatch.build.targets.wheel]`'s `include` so a
  `pip install` gets it too (the same class of bug the CI wheel-import
  check below already exists to catch).
- `tests/test_analytics.py`: unit tests for every function in
  `analytics.py`.
- `tests/test_mcp_client_integration.py`: integration tests for the new
  tools through the actual MCP client, plus a private-metric rejection
  test.
- `tests/test_logic.py`: real multi-threaded concurrency tests that hold
  a write lock on one connection while a second writes, and that fire
  several concurrent upserts at once — exercising WAL mode and
  `busy_timeout` under actual contention instead of only checking their
  pragma values.

### Changed
- CI's "Verify wheel contains all required modules" step now imports
  `analytics` explicitly alongside the existing modules.

## [0.2.0] - 2026-09-06

### Fixed
- `server.py` and `init_db.py` no longer default to storing `health.db`
  inside the installed package's own directory on a system-wide `pip
  install` (usually not writable). Both now detect whether `data/` next
  to the source is writable and fall back automatically to a per-user
  data directory (e.g. `~/.local/share/quantified-self-mcp` on Linux) —
  see `logic.default_data_dir`. `HEALTH_DB_PATH` and `--db-path` still
  override this either way.
- `init_db.py` now only requires a `date` column in a CSV's header — it
  previously also required `steps`, `sleep_hours`, and `resting_heart_rate`
  to be present, which broke the documented "add one column later"
  incremental-update workflow (e.g. a follow-up CSV with just
  `date,weight_kg`) with a `SystemExit`.
- CI now builds the actual package (`python -m build`) and imports it from
  the built wheel, so a release missing a required module — as has
  happened twice before with `logic.py` — fails CI instead of reaching
  PyPI.

### Added
- `init_db.py` now supports a `--db-path` flag and reads the
  `HEALTH_DB_PATH` environment variable (matching `server.py`), so a
  `pip install`-ed copy can be pointed at a writable location instead of
  defaulting to somewhere inside the installed package.
- `tests/test_init_db.py`: unit tests for CSV parsing, row-skip handling,
  `--replace`, and the CLI — previously only exercised end-to-end by one
  happy-path CSV in CI, with no pytest coverage.

### Documentation
- README now documents installing from PyPI (`pip install
  quantified-self-mcp`) and configuring Claude Desktop for a pip install,
  alongside the existing source-checkout instructions.

## [0.1.0] - 2026-09-02

First published release ([PyPI](https://pypi.org/project/quantified-self-mcp/0.1.0/)).

- MCP server exposing local health data (steps, sleep, resting heart
  rate, weight, workout minutes, mood, water intake) to an LLM via
  FastMCP, backed by a local SQLite database.
- `read_health_data` and `log_daily_metric` / `clear_metric` tools, with
  range validation on logged values.
- `init_db.py` CSV importer with upsert-by-date semantics.
- WAL mode and a busy timeout on all SQLite connections, to reduce
  "database is locked" errors during concurrent access.
- `fastmcp.json` for one-command installs into Claude Desktop and other
  MCP clients.
- Automatic schema migration, so upgrading never requires deleting an
  existing database.
- Project narrowed from an original health-and-finance scope to
  health-only, to keep the initial release focused (finance tracking
  remains in git history for later).
